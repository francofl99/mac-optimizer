#!/usr/bin/env python3
"""mac-optimizer dashboard. Usage: server.py [--port N] | --metrics"""
import collections
import ipaddress
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
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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


def battery_now():
    out = sh(["pmset", "-g", "batt"])
    pct = re.search(r"(\d+)%", out)
    return {
        "percent": int(pct.group(1)) if pct else None,
        "source": "AC" if "AC Power" in out else "Battery",
        "state": (re.search(r"%; ([^;]+);", out) or [None, None])[1],
    }


def battery():
    v = battery_now()
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


_machine = {}


def machine():
    if not _machine:
        hw = sh(["system_profiler", "SPHardwareDataType"], timeout=30)
        field = lambda k: (re.search(rf"{k}: (.+)", hw) or [None, None])[1]
        _machine.update(model=field("Model Name"), chip=field("Chip") or field("Processor Name"),
                        macos=sh(["sw_vers", "-productVersion"]).strip() or None)
    return _machine


def disk():
    du = shutil.disk_usage("/System/Volumes/Data")
    return {"total_gb": du.total / GB, "used_gb": du.used / GB, "free_gb": du.free / GB}


PING_HOST = "1.1.1.1"
VPN_IFACES = ("utun", "ipsec", "ppp")
NET_POOL = ThreadPoolExecutor(3)
_ports = {"t": 0, "v": []}
_wifi_cache = {"t": 0, "v": None}
_net = {"v": None}


def service_order():
    if time.time() - _ports["t"] > 300:
        out = sh(["networksetup", "-listnetworkserviceorder"])
        _ports.update(t=time.time(), v=re.findall(r"Hardware Port: ([^,]+), Device: (\w+)\)", out))
    return _ports["v"]


def uplink():
    # With a VPN the default route is a utun; the scoped route shows the physical port and router underneath.
    for port, dev in service_order():
        gw = re.search(r"gateway: (\S+)", sh(["route", "-n", "get", "-ifscope", dev, "default"], timeout=3))
        if gw:
            return {"port": port, "device": dev, "gateway": gw.group(1)}
    return {}


def ping(host, count=5):
    out = sh(["ping", "-q", "-c", str(count), "-i", "0.2", "-W", "1000", host], timeout=count + 3)
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"= [\d.]+/([\d.]+)/[\d.]+/([\d.]+) ms", out)
    return {"host": host, "ms": round(float(rtt.group(1)), 1) if rtt else None,
            "jitter_ms": round(float(rtt.group(2)), 1) if rtt else None,
            "loss_pct": float(loss.group(1)) if loss else None}


def dns_ms():
    m = re.search(r"Query time: (\d+) msec", sh(["dig", "+tries=1", "+time=2", "apple.com"], timeout=4))
    return int(m.group(1)) if m else None


def iface_bytes(dev):
    for line in sh(["netstat", "-ibn", "-I", dev]).splitlines():
        if "<Link#" in line:
            cols = line.split()
            return int(cols[-5]), int(cols[-2])
    return None


def wifi():
    if time.time() - _wifi_cache["t"] > 60:
        cur = sh(["system_profiler", "SPAirPortDataType"], timeout=30).partition("Current Network Information:")[2]
        cur = cur.partition("Other Local Wi-Fi Networks:")[0]
        field = lambda k: (re.search(rf"{k}: (.+)", cur) or [None, None])[1]
        sn = re.search(r"Signal / Noise: (-?\d+) dBm / (-?\d+) dBm", cur)
        rate = field("Transmit Rate")
        _wifi_cache.update(t=time.time(), v={
            "rssi": int(sn.group(1)) if sn else None, "noise": int(sn.group(2)) if sn else None,
            "channel": field("Channel"), "phy": field("PHY Mode"),
            "tx_rate_mbps": int(rate) if rate and rate.isdigit() else None,
        } if cur else None)
    return _wifi_cache["v"]


TRUSTED_FILE = BASE / "trusted-networks.txt"
norm_mac = lambda m: ":".join(p.zfill(2) for p in m.lower().split(":"))


def gateway_mac(ip):
    m = re.search(r" at ([0-9a-f:]{11,17}) ", sh(["arp", "-n", ip], timeout=3))
    return norm_mac(m.group(1)) if m else None


def trusted_macs():
    try:
        lines = TRUSTED_FILE.read_text().splitlines()
    except OSError:
        return set()
    return {norm_mac(l.split("#")[0].strip()) for l in lines if l.split("#")[0].strip()}


def network():
    up = uplink()
    via = (re.search(r"interface: (\S+)", sh(["route", "-n", "get", "default"], timeout=3)) or [None, None])[1]
    gw = NET_POOL.submit(ping, up["gateway"]) if up else None
    inet, dns = NET_POOL.submit(ping, PING_HOST), NET_POOL.submit(dns_ms)
    gateway = gw.result() if gw else None
    # The router's MAC identifies the home network: the SSID is redacted without Location permission.
    mac = gateway_mac(up["gateway"]) if up else None
    return {
        "t": time.time(), "uplink": up, "route_via": via,
        "vpn": via if via and via.startswith(VPN_IFACES) else None,
        "gateway": gateway, "internet": inet.result(), "dns_ms": dns.result(),
        "gateway_mac": mac, "trusted": bool(mac) and mac in trusted_macs(),
        "wifi": wifi() if up.get("port") == "Wi-Fi" else None,
        "bytes": iface_bytes(up["device"]) if up else None,
    }


def throughput(prev, cur):
    if not (prev and prev["bytes"] and cur["bytes"]) or prev["uplink"] != cur["uplink"]:
        return None, None
    dt = cur["t"] - prev["t"]
    return tuple(round(max(0, c - p) * 8 / dt) for p, c in zip(prev["bytes"], cur["bytes"]))


SPEEDTEST = {"status": "idle", "result": None}
SPEEDTEST_LOCK = threading.Lock()
SPEEDTEST_FILE = BASE / "speedtest.json"


def run_speedtest():
    try:
        r = json.loads(sh(["networkQuality", "-c"], timeout=120))
        result = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "down_bps": r.get("dl_throughput"),
                  "up_bps": r.get("ul_throughput"), "rpm": r.get("responsiveness"),
                  "base_rtt_ms": r.get("base_rtt"), "interface": r.get("interface_name"),
                  "server": r.get("test_endpoint")}
        SPEEDTEST_FILE.write_text(json.dumps(result))
        SPEEDTEST.update(status="ok", result=result)
    except (ValueError, OSError):
        SPEEDTEST.update(status="error")


def start_speedtest():
    with SPEEDTEST_LOCK:
        if SPEEDTEST["status"] == "running":
            return 409, {"error": "El test ya está corriendo"}
        SPEEDTEST["status"] = "running"
    threading.Thread(target=run_speedtest, daemon=True).start()
    return 200, SPEEDTEST


def set_trusted(trusted):
    up = uplink()
    mac = gateway_mac(up["gateway"]) if up else None
    if not mac:
        return 409, {"error": "No se pudo identificar el router de esta red"}
    keep = [l for l in (TRUSTED_FILE.read_text().splitlines() if TRUSTED_FILE.exists() else [])
            if norm_mac(l.split("#")[0].strip() or "x") != mac]
    if trusted:
        keep.append(f"{mac}  # router {up['gateway']}, {time.strftime('%Y-%m-%d')}")
    TRUSTED_FILE.write_text("\n".join(keep) + "\n" if keep else "")
    if _net["v"] and _net["v"]["gateway_mac"] == mac:
        _net["v"]["trusted"] = trusted
    return 200, {"gateway_mac": mac, "trusted": trusted}


SERVICE_KINDS = [("_androidtvremote2", "Android TV"), ("_googlecast", "Chromecast"), ("_airplay", "AirPlay"),
                 ("_raop", "AirPlay"), ("_companion-link", "Apple"), ("_hap", "HomeKit"), ("_ipp", "Impresora"),
                 ("_printer", "Impresora"), ("_scanner", "Escáner"), ("_uscan", "Escáner"),
                 ("_spotify-connect", "Spotify Connect"), ("_smb", "Archivos compartidos"), ("_ssh", "SSH")]
MAX_SCAN_HOSTS = 1024
SCAN = {"status": "idle", "result": None}
SCAN_LOCK = threading.Lock()


def dns_unescape(s):
    raw = re.sub(rb"\\(\d{3})", lambda m: bytes([int(m.group(1))]), s.encode())
    return re.sub(rb"\\(.)", rb"\1", raw).decode(errors="replace").replace("\xa0", " ")


def mdns(ip, *query):
    # Unicast mDNS straight to the device; it answers "legacy" queries even though the port isn't 5353.
    out = sh(["dig", "+short", "+time=1", "+tries=1", "-p", "5353", f"@{ip}", *query], timeout=4)
    return [l for l in out.splitlines() if l and not l.startswith(";")]


def identify(ip):
    # Some devices answer with every record they own (A, SRV, TXT…), so keep only lines of the asked shape.
    host = next((l for l in mdns(ip, "-x", ip) if l.endswith(".local.")), None)
    services = mdns(ip, "_services._dns-sd._udp.local", "PTR")
    known = [(s, label) for t, label in SERVICE_KINDS for s in services if s.startswith(t + ".")]
    name = None
    if known:
        inst = next((l for l in mdns(ip, known[0][0], "PTR") if l.endswith("." + known[0][0])), None)
        name = re.sub(r"-[0-9a-f]{32}$", "", dns_unescape(inst.removesuffix("." + known[0][0]))) if inst else None
    if not name and host:
        name = dns_unescape(host).removesuffix(".local.")
    return name, list(dict.fromkeys(label for _, label in known))


def lan(dev):
    me = sh(["ipconfig", "getifaddr", dev]).strip()
    return me, ipaddress.ip_interface(f"{me}/{sh(['ipconfig', 'getoption', dev, 'subnet_mask']).strip()}").network


def run_scan(up, mac, me, net):
    dev = up["device"]
    with ThreadPoolExecutor(64) as pool:
        list(pool.map(lambda h: sh(["ping", "-q", "-c", "1", "-W", "500", "-b", dev, str(h)], timeout=3),
                      [h for h in net.hosts() if str(h) != me]))
        found = {}
        for ip, hw in re.findall(r"\(([\d.]+)\) at ([0-9a-f:]+) on " + dev, sh(["arp", "-an", "-i", dev])):
            if ipaddress.ip_address(ip) in net and ip != me:
                found[ip] = norm_mac(hw)
        names = dict(zip(found, pool.map(identify, found)))
    devices = [{"ip": me, "mac": None, "name": sh(["scutil", "--get", "ComputerName"]).strip() or None,
                "kinds": [], "role": "self", "private": False}]
    for ip, hw in found.items():
        name, kinds = names[ip]
        devices.append({"ip": ip, "mac": hw, "name": name, "kinds": kinds,
                        "role": "router" if ip == up["gateway"] else None,
                        "private": bool(int(hw[:2], 16) & 2)})
    devices.sort(key=lambda d: ({"router": 0, "self": 1}.get(d["role"], 2), ipaddress.ip_address(d["ip"])))
    SCAN.update(status="ok", result={"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "gateway_mac": mac,
                                     "subnet": str(net), "devices": devices})


def start_scan():
    up = uplink()
    mac = gateway_mac(up["gateway"]) if up else None
    if not mac or mac not in trusted_macs():
        return 403, {"error": "Esta red no está marcada como de casa: no se escanea"}
    try:
        me, net = lan(up["device"])
    except ValueError:
        return 409, {"error": "Sin dirección IPv4 en esta red"}
    if net.num_addresses > MAX_SCAN_HOSTS:
        return 400, {"error": "La subred es demasiado grande para escanearla"}
    with SCAN_LOCK:
        if SCAN["status"] == "running":
            return 409, {"error": "La búsqueda ya está corriendo"}
        SCAN["status"] = "running"

    def job():
        try:
            run_scan(up, mac, me, net)
        except (ValueError, OSError):
            SCAN.update(status="error")

    threading.Thread(target=job, daemon=True).start()
    return 200, scan_state()


def scan_state():
    # Results stay in memory and only for the network they came from: device names can identify people.
    res = SCAN["result"]
    cur = (_net["v"] or {}).get("gateway_mac")
    return {**SCAN, "result": res if res and res["gateway_mac"] == cur else None}


def metrics():
    load = os.getloadavg()
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cpus": os.cpu_count(),
        "machine": machine(),
        "load": [round(x, 2) for x in load],
        "memory": memory(),
        "disk": disk(),
        "battery": battery(),
        "apps": top_apps(),
        "docker": docker(),
        "network": _net["v"],
    }


SAMPLE_EVERY = 15
HISTORY = collections.deque(maxlen=24 * 3600 // SAMPLE_EVERY)
WINDOWS = {"15m": 900, "1h": 3600}
MAX_POINTS = 240


def sample():
    m = memory()
    net = network()
    net["rx_bps"], net["tx_bps"] = throughput(_net["v"], net)
    _net["v"] = net
    return {
        "t": int(time.time()),
        "mem": 100 - m["free_pct"] if m["free_pct"] is not None else None,
        "load": round(os.getloadavg()[0], 2),
        "swap": round(m["swap_used_gb"], 3),
        "disk": round(disk()["free_gb"], 2),
        "batt": battery_now()["percent"],
        "ping": net["internet"]["ms"],
        "gw": (net["gateway"] or {}).get("ms"),
        "rx": net["rx_bps"],
        "tx": net["tx_bps"],
    }


def sampler():
    while True:
        start = time.time()
        try:
            HISTORY.append(sample())
        except Exception:
            pass
        time.sleep(max(1, SAMPLE_EVERY - (time.time() - start)))


def boot_time():
    m = re.search(r"sec = (\d+)", sh(["sysctl", "-n", "kern.boottime"]))
    return int(m.group(1)) if m else 0


def history(window):
    since = boot_time() if window == "boot" else int(time.time()) - WINDOWS.get(window, 900)
    pts = [s for s in list(HISTORY) if s["t"] >= since]
    step = max(1, -(-len(pts) // MAX_POINTS))
    picked = pts[::-1][::step][::-1]
    return {"window": window, "since": since, "now": int(time.time()), "every": SAMPLE_EVERY, "samples": picked}


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
        url = urlsplit(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            html = DASHBOARD.read_text().replace("__CSRF_TOKEN__", self.token)
            return self.send(200, html.encode(), "text/html; charset=utf-8")
        if path == "/api/live":
            return self.send(200, metrics())
        if path == "/api/live/history":
            return self.send(200, history(parse_qs(url.query).get("window", ["15m"])[0]))
        if path == "/api/network/speedtest":
            return self.send(200, SPEEDTEST)
        if path == "/api/network/scan":
            return self.send(200, scan_state())
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
        if self.path == "/api/network/speedtest":
            return self.send(*start_speedtest())
        if self.path == "/api/network/trust":
            return self.send(*set_trusted(bool(body.get("trusted"))))
        if self.path == "/api/network/scan":
            return self.send(*start_scan())
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
        _net["v"] = network()
        print(json.dumps(metrics(), indent=2))
        sys.exit()
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8765
    REPORTS.mkdir(parents=True, exist_ok=True)
    try:
        SPEEDTEST.update(status="ok", result=json.loads(SPEEDTEST_FILE.read_text()))
    except (OSError, ValueError):
        pass
    RUN_ENV.update(os.environ, PATH=schedule.user_path(), HOMEBREW_NO_AUTO_UPDATE="1")
    Handler.token = secrets.token_urlsafe(24)
    Handler.allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    threading.Thread(target=sampler, daemon=True).start()
    print(f"Repaso dashboard: http://localhost:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
