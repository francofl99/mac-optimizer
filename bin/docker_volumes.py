#!/usr/bin/env python3
"""Classify Docker images, build cache and volumes by deletion risk. Read-only.

Writes <data>/docker/summary.json and one volume list per class (consumed by
`xargs docker volume rm < list`), and prints a human summary for the snapshot.
"""
import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DATA = Path(os.environ.get("MAC_OPTIMIZER_HOME", Path.home() / ".mac-optimizer"))
OUT = DATA / "docker"
ORB_VOLUMES = Path.home() / "OrbStack/docker/volumes"
UNITS = {"B": 1e-9, "kB": 1e-6, "KB": 1e-6, "MB": 1e-3, "GB": 1, "TB": 1e3}
DB_MARKERS = ("ibdata1", "mysql.ibd", "#innodb_redo", "undo_001", "PG_VERSION", "pg_wal", "WiredTiger", "mongod.lock")
CACHE_MARKERS = {".bin": "node_modules", ".package-lock.json": "node_modules", "dump.rdb": "redis",
                 "appendonlydir": "redis", "mnesia": "rabbitmq", ".erlang.cookie": "rabbitmq"}


def gb(size):
    m = re.match(r"([\d.]+)\s*([kKMGT]?B)", size or "")
    return float(m.group(1)) * UNITS[m.group(2)] if m else 0.0


def docker(*args, timeout=600):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout).stdout


def patterns(name):
    path = DATA / name
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text().splitlines() if l.strip() and not l.startswith("#")]


def matches(name, globs):
    return any(fnmatch.fnmatchcase(name, g) for g in globs)


_probe_image = None


def list_volume(name):
    """Top-level entries of a volume, via OrbStack's host mount or a throwaway read-only container."""
    host = ORB_VOLUMES / name
    if host.is_dir():
        try:
            return os.listdir(host)
        except OSError:
            return None
    global _probe_image
    if _probe_image is None:
        _probe_image = next((i for i in ("alpine", "busybox") if docker("images", "-q", i).strip()), "")
    if not _probe_image:
        return None
    out = subprocess.run(["docker", "run", "--rm", "--network", "none", "-v", f"{name}:/v:ro", _probe_image, "ls", "-a", "/v"],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
    return [l for l in out.stdout.split() if l not in (".", "..")] if out.returncode == 0 else None


def classify(vol, protected, branch):
    name, anonymous = vol["Name"], bool(re.fullmatch(r"[0-9a-f]{64}", vol["Name"]))
    if vol.get("Links", "0") != "0":
        return "in_use", "montado por un contenedor"
    if matches(name, protected):
        return "protected", "patrón en protected-volumes.txt"
    if matches(name, branch):
        return "branch", "patrón en branch-volumes.txt"
    if re.search(r"node_modules|pnpm[_-]store|yarn[_-]cache", name):
        return "low", "cache de dependencias"
    entries = list_volume(name)
    if entries is None:
        return "protected", "no se pudo inspeccionar"
    if any(e in DB_MARKERS or e.startswith("#innodb") for e in entries):
        return "protected", "contiene archivos de base de datos"
    kinds = {CACHE_MARKERS[e] for e in entries if e in CACHE_MARKERS}
    if kinds:
        return "low", " + ".join(sorted(kinds))
    if not [e for e in entries if e != ".DS_Store"]:
        # A listing can come back empty while the volume is being removed or not yet mounted.
        return ("low", "vacío") if vol.get("_gb", 0) < 0.001 else ("protected", "lectura vacía pero con tamaño")
    # Named volumes with unknown content are user data until proven otherwise.
    return ("low", "anónimo sin datos reconocibles") if anonymous and vol.get("_gb", 0) < 0.01 else ("protected", "contenido desconocido")


def main():
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("Docker daemon not running")
        return
    OUT.mkdir(parents=True, exist_ok=True)
    df = json.loads(docker("system", "df", "-v", "--format", "{{json .}}") or "{}")
    protected, branch = patterns("protected-volumes.txt"), patterns("branch-volumes.txt")

    classes = {k: [] for k in ("in_use", "protected", "branch", "low")}
    for vol in df.get("Volumes") or []:
        vol["_gb"] = gb(vol.get("Size"))
        cls, why = classify(vol, protected, branch)
        classes[cls].append({"name": vol["Name"], "gb": round(vol["_gb"], 3), "why": why})

    for cls, fname in (("low", "volumes-low-risk.txt"), ("branch", "volumes-branch.txt"), ("protected", "volumes-protected.txt")):
        rows = sorted(classes[cls], key=lambda r: -r["gb"])
        (OUT / fname).write_text("".join(f"{r['name']}\n" for r in rows))

    images = df.get("Images") or []
    unused = [i for i in images if str(i.get("Containers", "0")) == "0"]
    # Images without RepoDigests were never pulled from a registry: removing them means a rebuild.
    no_digest = set()
    if unused:
        for line in docker("image", "inspect", "-f", "{{.Id}} {{len .RepoDigests}}", *[i["ID"] for i in unused]).splitlines():
            iid, n = line.split()
            if n == "0":
                no_digest.add(iid.replace("sha256:", "")[:12])
    for i in unused:
        i["_local"] = i["ID"].replace("sha256:", "")[:12] in no_digest
    cache = df.get("BuildCache") or []
    summary = {
        "images": {"count": len(images), "gb": sum(gb(i.get("Size")) for i in images),
                   "unused_count": len(unused), "unused_gb": sum(gb(i.get("UniqueSize") or i.get("Size")) for i in unused),
                   "unused": sorted(({"name": f"{i['Repository']}:{i['Tag']}", "gb": round(gb(i.get("Size")), 2),
                                      "created": i.get("CreatedSince"), "local_build": i["_local"]}
                                     for i in unused), key=lambda x: -x["gb"])},
        "build_cache": {"count": len(cache), "gb": sum(gb(c.get("Size")) for c in cache),
                        "reclaim_gb": sum(gb(c.get("Size")) for c in cache if str(c.get("InUse")).lower() != "true")},
        "volumes": {cls: {"count": len(rows), "gb": round(sum(r["gb"] for r in rows), 2), "items": rows}
                    for cls, rows in classes.items()},
        "running_containers": len(df.get("Containers") or []),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    print(f"Images: {summary['images']['count']} ({summary['images']['gb']:.1f} GB), unused {summary['images']['unused_count']} (~{summary['images']['unused_gb']:.1f} GB)")
    print(f"Build cache: {summary['build_cache']['gb']:.1f} GB, reclaimable {summary['build_cache']['reclaim_gb']:.1f} GB")
    for cls, label in (("in_use", "in use"), ("protected", "protected"), ("branch", "branch copies"), ("low", "low risk")):
        v = summary["volumes"][cls]
        print(f"Volumes {label:14s} {v['count']:4d}  {v['gb']:6.1f} GB")
    reasons = {}
    for r in classes["low"] + classes["protected"]:
        reasons[r["why"]] = reasons.get(r["why"], 0) + 1
    print("Reasons:", ", ".join(f"{k}={v}" for k, v in sorted(reasons.items(), key=lambda x: -x[1])))
    print("Unused images (top 15, * = local build):")
    for i in summary["images"]["unused"][:15]:
        print(f"  {i['gb']:5.2f} GB  {i['created']:>14s}  {'*' if i['local_build'] else ' '} {i['name']}")
    print(f"Lists: {OUT}")


if __name__ == "__main__":
    sys.exit(main())
