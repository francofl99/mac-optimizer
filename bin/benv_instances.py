#!/usr/bin/env python3
"""List benv instances (parallel branch environments) and remove the ones the user picks.

Usage: benv_instances.py                   scan (read-only): writes <data>/benv/summary.json and instances.tsv
       benv_instances.py rm <tsv> <key...>  remove each picked instance (key = <project>/<instance>)

The scan reads ~/.benv/*/state.json and never runs benv or the workspace's .benv.json commands.
Tracked instances are removed with `benv rm`; orphan dirs (on disk, missing from state.json) go to the Trash.
Instances with uncommitted changes or unpushed commits are never removed.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from git_repos import git, risky_config, tilde

DATA = Path(os.environ.get("MAC_OPTIMIZER_HOME", Path.home() / ".mac-optimizer"))
OUT = DATA / "benv"
HOME = str(Path.home())
BENV_HOME = Path(os.environ.get("MAC_OPTIMIZER_BENV_HOME", Path.home() / ".benv"))
PROJECT_DIRS = os.environ.get("MAC_OPTIMIZER_PROJECT_DIRS", f"{HOME}/Projects {HOME}/Desktop {HOME}/code").split()
BENV_FILE = "docker-compose.benv.yml"
IDLE_DAYS = int(os.environ.get("MAC_OPTIMIZER_BENV_DAYS", "14"))
ORPHAN_GRACE = 6 * 3600
MAX_DEPTH = 4
SKIP_DIRS = {"node_modules", "vendor", "venv", ".venv", "dist", "build", "target", "Pods"}


def slug(s):
    """Same as benv's slug(): state dirs and compose projects are named with it."""
    return re.sub(r"^-+|-+$", "", re.sub(r"[^a-z0-9]+", "-", str(s).lower()))


def find_manifests():
    """benv project name → its .benv.json: benv rm must run from that workspace dir."""
    found = {}
    for root in PROJECT_DIRS:
        base = Path(root)
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            if ".benv.json" in filenames:
                try:
                    raw = json.loads((Path(dirpath) / ".benv.json").read_text())
                    found.setdefault(raw.get("name") or slug(Path(dirpath).name), {
                        "dir": os.path.realpath(dirpath), "repo_subdir": raw.get("repoSubdir") or ".",
                        "compose_prefix": (raw.get("compose") or {}).get("projectPrefix")})
                except (OSError, ValueError, AttributeError):
                    pass
            depth = len(Path(dirpath).relative_to(base).parts)
            dirnames[:] = [] if depth >= MAX_DEPTH else [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
    return found


def tracked(project):
    try:
        return json.loads((BENV_HOME / project / "state.json").read_text()).get("instances", {})
    except (OSError, ValueError):
        return {}


def orphan_record(project, d, manifest):
    """benv's record shape for an instance dir it no longer tracks (a failed rm or a hand-edited state)."""
    name = d.name
    prefix = manifest and manifest["compose_prefix"]
    return {"name": name, "dir": str(d), "repoDir": str(d / (manifest["repo_subdir"] if manifest else ".")),
            "project": slug(f"{prefix or project}-{name}"), "orphan": True,
            "composeFile": str(d / BENV_FILE) if (d / BENV_FILE).exists() else None}


def records(manifests):
    out = []
    for state in sorted(BENV_HOME.glob("*/state.json")):
        project = state.parent.name
        recs = tracked(project)
        out += [(project, r) for r in recs.values()]
        known = {os.path.realpath(r["dir"]) for r in recs.values() if r.get("dir")}
        for d in sorted((BENV_HOME / project / "instances").glob("*/")):
            # `benv up` only writes state.json once the clone and the stack are up.
            fresh = d.is_dir() and time.time() - d.stat().st_mtime < ORPHAN_GRACE
            if d.is_dir() and not d.is_symlink() and not fresh and os.path.realpath(d) not in known:
                out.append((project, orphan_record(project, d, manifests.get(project))))
    return out


def du_gb(path):
    out = subprocess.run(["du", "-sk", path], capture_output=True, text=True).stdout
    return round(int(out.split("\t")[0]) / 1048576, 2) if out else 0


def containers():
    """compose project → [(container, mem)] for running containers."""
    if not shutil.which("docker"):
        return {}
    try:
        ps = subprocess.run(["docker", "ps", "--format", '{{.Label "com.docker.compose.project"}}\t{{.Names}}'],
                            capture_output=True, text=True, timeout=30).stdout
        stats = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"],
                               capture_output=True, text=True, timeout=60).stdout if ps.strip() else ""
    except subprocess.TimeoutExpired:
        return {}
    mem = dict(l.split("\t", 1) for l in stats.splitlines() if "\t" in l)
    out = defaultdict(list)
    for line in ps.splitlines():
        project, _, name = line.partition("\t")
        if project:
            out[project].append((name, mem.get(name, "").split(" / ")[0]))
    return out


def mem_mb(s):
    m = re.match(r"([\d.]+)\s*([KMG])i?B", s or "")
    return float(m[1]) * {"K": 1 / 1024, "M": 1, "G": 1024}[m[2]] if m else 0


def git_state(repo):
    """(dirty, unpushed, last commit or checkout) or None if git can't be inspected safely.

    The index mtime is not used: any `git status` without GIT_OPTIONAL_LOCKS (an IDE, a shell prompt) rewrites it.
    """
    if not (Path(repo) / ".git").exists() or risky_config(repo):
        return None
    dirty = any(l[3:] != BENV_FILE for l in git(repo, "status", "--porcelain", "--untracked-files=normal").splitlines())
    unpushed = int(git(repo, "rev-list", "--count", "HEAD", "--not", "--remotes").strip() or 0)
    gitdir = Path(repo) / ".git"
    stamps = [os.stat(gitdir / "logs" / "HEAD").st_mtime] if (gitdir / "logs" / "HEAD").exists() else []
    last_commit = git(repo, "log", "-1", "--format=%ct").strip()
    if last_commit.isdigit():
        stamps.append(int(last_commit))
    return dirty, unpushed, max(stamps, default=0)


def lock_reason(exists, gs, workspace):
    if not workspace:
        return "no encontré el .benv.json del workspace"
    if not exists:
        return None
    if gs is None:
        return "git no se pudo inspeccionar"
    if gs[0]:
        return "tiene cambios sin commitear"
    if gs[1]:
        return f"{gs[1]} commits sin pushear"
    return None


def inspect(project, rec, manifest, running, now):
    exists = bool(rec.get("dir")) and Path(rec["dir"]).is_dir()
    repo = rec.get("repoDir") or rec["dir"]
    gs = git_state(repo) if exists else None
    conts = running.get(rec.get("project"), [])
    orphan = rec.get("orphan", False)
    return {
        "key": f"{project}/{rec['name']}",
        "project": project,
        "name": rec["name"],
        "branch": git(repo, "branch", "--show-current").strip() if orphan and gs else rec.get("branch", ""),
        "dir": tilde(rec.get("dir") or ""),
        "exists": exists,
        "orphan": orphan,
        "created": (rec.get("created") or "")[:10],
        "gb": du_gb(rec["dir"]) if exists else 0,
        "running": len(conts),
        "mem_mb": round(sum(mem_mb(m) for _, m in conts)),
        "idle_days": int((now - gs[2]) // 86400) if gs and gs[2] else None,
        "dirty": bool(gs and gs[0]),
        "unpushed": gs[1] if gs else 0,
        "workspace": tilde(manifest["dir"]) if manifest else None,
        "locked": lock_reason(exists, gs, manifest),
    }


def dashboard_item(i, suggested):
    if i["orphan"]:
        state = "huérfana: benv ya no la registra, va a la papelera"
    elif not i["exists"]:
        state = "la carpeta ya no existe, solo se limpia el registro"
    else:
        state = f"corriendo ({i['running']} contenedores, {i['mem_mb']} MB)" if i["running"] else "detenida"
    idle = f"{i['idle_days']} días sin commits ni checkouts" if i["idle_days"] is not None else None
    size = f"{i['gb']:.1f} GB" if i["exists"] else None
    return {"key": i["key"], "label": f"{i['key']} · {i['branch']}" if i["branch"] else i["key"],
            "detail": " · ".join(x for x in (idle, size, state) if x), "gb": i["gb"],
            "checked": i["key"] in suggested, "locked": i["locked"]}


def scan():
    OUT.mkdir(parents=True, exist_ok=True)
    manifests, running, now = find_manifests(), containers(), time.time()
    with ThreadPoolExecutor(8) as pool:
        items = list(pool.map(lambda pr: inspect(*pr, manifests.get(pr[0]), running, now), records(manifests)))
    items.sort(key=lambda i: (i["locked"] is not None, -(i["idle_days"] or 0), -i["gb"]))

    (OUT / "instances.tsv").write_text("".join(
        f"{i['key']}\t{os.path.expanduser(i['workspace'])}\t{i['name']}\n" for i in items if not i["locked"]))
    summary = {"idle_days": IDLE_DAYS, "instances": len(items), "running": sum(1 for i in items if i["running"]),
               "total_gb": round(sum(i["gb"] for i in items), 2), "mem_mb": sum(i["mem_mb"] for i in items),
               "suggested": [i["key"] for i in items if not i["locked"] and not i["running"]
                             and (not i["exists"] or i["orphan"] or (i["idle_days"] or 0) >= IDLE_DAYS)],
               "list": items}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    (OUT / "items.json").write_text(json.dumps([dashboard_item(i, summary["suggested"]) for i in items], indent=2, ensure_ascii=False))

    print(f"benv instances: {summary['instances']}  running {summary['running']} ({summary['mem_mb']} MB RAM)  "
          f"disk {summary['total_gb']:.1f} GB (APFS clones, real usage is lower)  suggested: {len(summary['suggested'])}")
    for i in items:
        idle = f"{i['idle_days']}d" if i["idle_days"] is not None else "?"
        state = "orphan" if i["orphan"] else "dir missing" if not i["exists"] else f"{i['running']} running"
        print(f"  {i['key']:<45} {i['branch'][:30]:<30} idle {idle:>5}  {i['gb']:5.1f} GB  {state}"
              + (f"  [{i['locked']}]" if i["locked"] else ""))
    print(f"List: {OUT / 'instances.tsv'}")


def benv_bin():
    return shutil.which("benv") or shutil.which("benv", path=f"{HOME}/.local/bin")


def current(project, name, manifest):
    rec = tracked(project).get(name)
    if rec:
        return rec
    d = BENV_HOME / project / "instances" / name
    return orphan_record(project, d, manifest) if d.is_dir() and not d.is_symlink() else None


def trash_orphan(key, rec):
    if rec.get("composeFile"):
        down = subprocess.run(["docker", "compose", "-p", rec["project"], "-f", rec["composeFile"], "down"],
                              cwd=rec["dir"], stdin=subprocess.DEVNULL)
        if down.returncode and rec["project"] in containers():
            print(f"✘ {key}: docker compose down falló y el stack sigue corriendo, se omite")
            return False
    dest = Path.home() / ".Trash" / f"benv-{key.replace('/', '-')}-{time.strftime('%Y%m%d%H%M%S')}"
    shutil.move(rec["dir"], dest)
    print(f"✔ {key}: borrada (huérfana, movida a {tilde(dest)})")
    return True


def rm(tsv, keys):
    known = {k: (ws, name) for k, ws, name in (l.split("\t") for l in Path(tsv).read_text().splitlines() if l)}
    manifests = find_manifests()
    groups, failed = defaultdict(list), False
    for key in dict.fromkeys(keys):
        if key not in known:
            print(f"✘ {key}: no está en la lista del último repaso, se omite")
            failed = True
            continue
        ws, name = known[key]
        project = key.split("/", 1)[0]
        rec = current(project, name, manifests.get(project))
        if not rec:
            print(f"✔ {key}: borrada (ya no existía)")
            continue
        exists = Path(rec["dir"]).is_dir()
        why = lock_reason(exists, git_state(rec.get("repoDir") or rec["dir"]) if exists else None, ws)
        if why:
            print(f"✘ {key}: {why} desde el repaso, se omite")
            failed = True
        elif rec.get("orphan"):
            failed |= not trash_orphan(key, rec)
        else:
            groups[ws].append((key, name))

    benv = benv_bin() if groups else None
    if groups and not benv:
        print("✘ no encontré el comando benv en el PATH")
        return True
    for ws, picked in groups.items():
        print(f"$ benv rm {' '.join(n for _, n in picked)}   (en {tilde(ws)})", flush=True)
        r = subprocess.run([benv, "rm", *[n for _, n in picked]], cwd=ws, stdin=subprocess.DEVNULL)
        left = tracked(picked[0][0].split("/", 1)[0])
        for key, name in picked:
            if name in left:
                failed = True
                print(f"✘ {key}: benv rm falló (código {r.returncode})")
            else:
                print(f"✔ {key}: borrada")
    return failed


if __name__ == "__main__":
    if len(sys.argv) == 1:
        scan()
    elif len(sys.argv) >= 4 and sys.argv[1] == "rm":
        sys.exit(1 if rm(sys.argv[2], sys.argv[3:]) else 0)
    else:
        sys.exit(__doc__.strip())
