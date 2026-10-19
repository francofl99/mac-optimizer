#!/usr/bin/env python3
"""mac-optimizer dashboard. Usage: server.py [--port N] | --metrics"""
import json
import os
import re
import secrets
import signal
import threading
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import schedule

BASE = Path(os.environ.get("MAC_OPTIMIZER_HOME", Path.home() / ".mac-optimizer"))
REPO = Path(__file__).resolve().parent.parent
REPORTS = BASE / "reports"
STATE = BASE / "state.json"
DASHBOARD = Path(__file__).with_name("dashboard.html")
EXEC_LOG = BASE / "exec.log"
GB = 1024 ** 3


def sh(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def memory():
    total = int(sh(["sysctl", "-n", "hw.memsize"]) or 0)
    page = int(sh(["pagesize"]) or 16384)
    vm = {}
    for line in sh(["vm_stat"]).splitlines():
        m = re.match(r'"?(.+?)"?:\s+(\d+)', line)
        if m:
            vm[m.group(1)] = int(m.group(2)) * page
    swap = re.findall(r"(\w+) = ([\d.]+)M", sh(["sysctl", "-n", "vm.swapusage"]))
    swap = {k: float(v) * 1024 ** 2 for k, v in swap}
    free_pct = re.search(r"(\d+)%", sh(["memory_pressure", "-Q"]))
    return {
        "total_gb": total / GB,
        "wired_gb": vm.get("Pages wired down", 0) / GB,
        "active_gb": vm.get("Pages active", 0) / GB,
        "inactive_gb": vm.get("Pages inactive", 0) / GB,
        "compressed_gb": vm.get("Pages occupied by compressor", 0) / GB,
        "free_gb": vm.get("Pages free", 0) / GB,
        "free_pct": int(free_pct.group(1)) if free_pct else None,
        "swap_used_gb": swap.get("used", 0) / GB,
        "swap_total_gb": swap.get("total", 0) / GB,
    }


def top_apps(limit=12):
    apps = {}
    for line in sh(["ps", "-Axo", "rss=,pcpu=,comm="]).splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        rss, cpu, comm = int(parts[0]), float(parts[1]), parts[2]
        m = re.search(r"/([^/]+)\.app/", comm)
        name = m.group(1) if m else comm.rsplit("/", 1)[-1]
        a = apps.setdefault(name, {"name": name, "mb": 0, "cpu": 0.0, "procs": 0})
        a["mb"] += rss / 1024
        a["cpu"] += cpu
        a["procs"] += 1
    rows = list(apps.values())
    return {
        "by_ram": sorted(rows, key=lambda r: -r["mb"])[:limit],
        "by_cpu": sorted(rows, key=lambda r: -r["cpu"])[:limit],
        "process_count": sum(r["procs"] for r in rows),
    }


_battery_cache = {"t": 0, "v": {}}


def battery():
    out = sh(["pmset", "-g", "batt"])
    pct = re.search(r"(\d+)%", out)
    v = {
        "percent": int(pct.group(1)) if pct else None,
        "source": "AC" if "AC Power" in out else "Battery",
        "state": (re.search(r"%; ([^;]+);", out) or [None, None])[1],
    }
    if time.time() - _battery_cache["t"] > 600:
        prof = sh(["system_profiler", "SPPowerDataType"], timeout=30)
        cyc = re.search(r"Cycle Count: (\d+)", prof)
        cap = re.search(r"Maximum Capacity: (\d+)", prof)
        cond = re.search(r"Condition: (\w+)", prof)
        _battery_cache.update(t=time.time(), v={
            "cycles": int(cyc.group(1)) if cyc else None,
            "max_capacity": int(cap.group(1)) if cap else None,
            "condition": cond.group(1) if cond else None,
        })
    v.update(_battery_cache["v"])
    blockers = re.search(r"sleep prevented by ([^)]+)", sh(["pmset", "-g"]))
    v["sleep_blockers"] = sorted(set(s.strip() for s in blockers.group(1).split(","))) if blockers else []
    return v


ORBSTACK_IMG = Path.home() / "Library/Group Containers/HUAQ24HBR6.dev.orbstack/data/data.img.raw"


def docker():
    # `docker system df` takes minutes with hundreds of volumes; live view only uses cheap calls.
    names = sh(["docker", "ps", "--format", "{{.Names}}"], timeout=5).split()
    vm_disk = ORBSTACK_IMG.stat().st_blocks * 512 / GB if ORBSTACK_IMG.exists() else None
    return {"running": len(names), "containers": names, "vm_disk_gb": vm_disk}


def metrics():
    du = shutil.disk_usage("/System/Volumes/Data")
    load = os.getloadavg()
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cpus": os.cpu_count(),
        "load": [round(x, 2) for x in load],
        "memory": memory(),
        "disk": {"total_gb": du.total / GB, "used_gb": du.used / GB, "free_gb": du.free / GB},
        "battery": battery(),
        "apps": top_apps(),
        "docker": docker(),
    }


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def set_status(finding_id, status, note=""):
    state = load_state()
    state.setdefault("findings", {})[finding_id] = {
        "status": status, "note": note, "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    return state


def list_reports():
    out = []
    for p in sorted(REPORTS.glob("*.json"), reverse=True):
        try:
            r = json.loads(p.read_text())
            out.append({"id": p.stem, "date": r.get("date"), "summary": r.get("summary"),
                        "metrics": r.get("metrics", {})})
        except ValueError:
            continue
    return out


def runnable(finding):
    cmd = (finding.get("command") or "").strip()
    if not cmd:
        return False, "Sin comando"
    if re.search(r"\bsudo\b", cmd):
        return False, "Requiere sudo: ejecútalo en tu terminal"
    return True, ""


JOBS, JOBS_LOCK, RUN_ENV = {}, threading.Lock(), {}
MAX_OUTPUT = 200_000
# stdin is /dev/null, so a CLI asking for confirmation reads EOF, aborts, and often still exits 0.
PROMPT_RE = re.compile(r"\[y/N\]|\(y/n\)|\[Y/n\]|Are you sure|Do you want to continue|Proceed\?", re.I)


def run_job(job):
    start = time.time()
    proc = subprocess.Popen(["/bin/bash", "-c", job["command"]], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                            cwd=str(Path.home()), env=RUN_ENV, start_new_session=True)
    job["pid"] = proc.pid
    for line in proc.stdout:
        if len(job["output"]) < MAX_OUTPUT:
            job["output"] += line
    proc.wait()
    prompted = bool(PROMPT_RE.search(job["output"][-2000:]))
    status = ("cancelled" if job.get("cancelled") else "needs_input" if prompted
              else "ok" if proc.returncode == 0 else "error")
    job.update(code=proc.returncode, duration=round(time.time() - start, 1), status=status)
    if job["status"] == "ok" and job["report"]:
        set_status(job["finding"], "done", f"ejecutado desde el dashboard ({job['duration']}s)")
    with EXEC_LOG.open("a") as f:
        f.write(json.dumps({k: job[k] for k in ("id", "report", "finding", "command", "status", "code", "duration", "started")},
                           ensure_ascii=False) + "\n")


def start_job(report_id, finding_id):
    path = REPORTS / f"{report_id}.json"
    if not re.fullmatch(r"[\w-]+", report_id or "") or not path.exists():
        return 404, {"error": "reporte no encontrado"}
    # The command always comes from the report on disk, never from the request body.
    finding = next((f for f in json.loads(path.read_text()).get("findings", []) if f.get("id") == finding_id), None)
    if not finding:
        return 404, {"error": "hallazgo no encontrado"}
    ok, why = runnable(finding)
    if not ok:
        return 400, {"error": why}
    return launch(report_id, finding_id, finding.get("title"), finding["command"])


def launch(report_id, finding_id, title, command):
    with JOBS_LOCK:
        if any(j["status"] == "running" and j["finding"] == finding_id for j in JOBS.values()):
            return 409, {"error": "Esta acción ya se está ejecutando"}
        job = {"id": secrets.token_hex(6), "report": report_id, "finding": finding_id, "title": title,
               "command": command, "status": "running", "output": "", "code": None,
               "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        JOBS[job["id"]] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return 200, job


class Handler(BaseHTTPRequestHandler):
    token = ""
    allowed_hosts = set()

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def host_ok(self):
        # Blocks DNS-rebinding: a foreign site resolving to 127.0.0.1 still sends its own Host header.
        return self.headers.get("Host") in self.allowed_hosts

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {"error": "host no permitido"})
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            html = DASHBOARD.read_text().replace("__CSRF_TOKEN__", self.token)
            return self.send(200, html.encode(), "text/html; charset=utf-8")
        if path == "/api/live":
            return self.send(200, metrics())
        if path == "/api/reports":
            return self.send(200, list_reports())
        if path == "/api/state":
            return self.send(200, load_state())
        if path == "/api/schedule":
            return self.send(200, schedule.show())
        if path == "/api/jobs":
            jobs = sorted(JOBS.values(), key=lambda j: j["started"], reverse=True)[:20]
            return self.send(200, [{k: v for k, v in j.items() if k != "output"} for j in jobs])
        m = re.fullmatch(r"/api/jobs/(\w+)", path)
        if m and m.group(1) in JOBS:
            return self.send(200, JOBS[m.group(1)])
        m = re.fullmatch(r"/api/reports/([\w-]+)", path)
        if m and (REPORTS / f"{m.group(1)}.json").exists():
            return self.send(200, (REPORTS / f"{m.group(1)}.json").read_bytes())
        self.send(404, {"error": "not found"})

    def do_POST(self):
        # Token is only embedded in the served page, so other sites in the browser can't trigger commands.
        if not self.host_ok() or not secrets.compare_digest(self.headers.get("X-Token", ""), self.token):
            return self.send(403, {"error": "token inválido"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        m = re.fullmatch(r"/api/findings/([\w.-]+)", self.path)
        if m:
            return self.send(200, set_status(m.group(1), body.get("status", "pending"), body.get("note", "")))
        if self.path == "/api/run":
            return self.send(*start_job(body.get("report"), body.get("finding")))
        if self.path == "/api/schedule":
            try:
                return self.send(200, schedule.apply(body.get("mode"), body.get("weekday"), body.get("hour", 10),
                                                     body.get("minute", 0), self.server.server_port))
            except (ValueError, TypeError, RuntimeError) as e:
                return self.send(400, {"error": str(e)})
        if self.path == "/api/analyze":
            return self.send(*launch(None, "__analysis__", "Nuevo repaso (Claude headless)", f'bash "{REPO}/bin/analyze.sh"'))
        m = re.fullmatch(r"/api/jobs/(\w+)/cancel", self.path)
        if m and m.group(1) in JOBS and JOBS[m.group(1)]["status"] == "running":
            job = JOBS[m.group(1)]
            job["cancelled"] = True
            os.killpg(job["pid"], signal.SIGTERM)
            return self.send(200, job)
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    if "--metrics" in sys.argv:
        print(json.dumps(metrics(), indent=2))
        sys.exit()
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8765
    REPORTS.mkdir(parents=True, exist_ok=True)
    RUN_ENV.update(os.environ, PATH=schedule.user_path(), HOMEBREW_NO_AUTO_UPDATE="1")
    Handler.token = secrets.token_urlsafe(24)
    Handler.allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    print(f"Repaso dashboard: http://localhost:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
