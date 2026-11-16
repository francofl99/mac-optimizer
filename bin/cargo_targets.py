#!/usr/bin/env python3
"""Find Rust/Cargo build caches (target/) in the project dirs and clean them without touching built apps.

Usage: cargo_targets.py              scan (read-only): writes <data>/cargo/summary.json and targets.tsv
       cargo_targets.py clean <tsv>  delete the compile cache of each listed target/

Final binaries and bundles (target/release/bundle/*.app) stay, so the built app still opens.
"""
import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DATA = Path(os.environ.get("MAC_OPTIMIZER_HOME", Path.home() / ".mac-optimizer"))
OUT = DATA / "cargo"
HOME = str(Path.home())
PROJECT_DIRS = os.environ.get("MAC_OPTIMIZER_PROJECT_DIRS", f"{HOME}/Projects {HOME}/Desktop {HOME}/code").split()
MIN_GB = float(os.environ.get("MAC_OPTIMIZER_CARGO_MIN_GB", "0.5"))
MAX_DEPTH = 5
SKIP_DIRS = {"node_modules", "vendor", "venv", ".venv", "dist", "Pods"}
CACHE_SUBDIRS = ("deps", "build", ".fingerprint", "incremental", "examples")
# Cargo writes this tag in every target dir it creates; a folder merely named "target" is never touched.
CARGO_TAG = "Signature: 8a477f597d28d172789f06886806bc55"


def tilde(p):
    p = str(p)
    return "~" + p[len(HOME):] if p.startswith(HOME + "/") else p


def is_cargo_target(path):
    try:
        return path.name == "target" and not path.is_symlink() and (path / "CACHEDIR.TAG").read_text(errors="replace").startswith(CARGO_TAG)
    except OSError:
        return False


def find_targets():
    found = set()
    for root in PROJECT_DIRS:
        base = Path(root)
        if not base.is_dir():
            continue
        for dirpath, dirnames, _ in os.walk(base):
            if is_cargo_target(Path(dirpath)):
                found.add(os.path.realpath(dirpath))
                dirnames[:] = []
                continue
            depth = len(Path(dirpath).relative_to(base).parts)
            dirnames[:] = [] if depth >= MAX_DEPTH else [d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS]
    return sorted(found)


def profile_dirs(target):
    """target/<profile> and target/<triple>/<profile>: any dir holding a .fingerprint cache."""
    t = Path(target)
    return [p for p in [*t.glob("*/"), *t.glob("*/*/")] if (p / ".fingerprint").is_dir() and not p.is_symlink()]


def du_kb(*paths):
    paths = [str(p) for p in paths if Path(p).exists()]
    if not paths:
        return 0
    out = subprocess.run(["du", "-sk", *paths], capture_output=True, text=True).stdout
    return sum(int(l.split("\t")[0]) for l in out.splitlines() if l)


def inspect(target):
    profiles = profile_dirs(target)
    cache = [p / s for p in profiles for s in CACHE_SUBDIRS]
    last = max((os.stat(p / ".fingerprint").st_mtime for p in profiles), default=os.stat(target).st_mtime)
    return {
        "path": target,
        "project": tilde(Path(target).parent),
        "gb": round(du_kb(target) / 1048576, 2),
        "cache_gb": round(du_kb(*cache) / 1048576, 2),
        "profiles": sorted(str(p.relative_to(target)) for p in profiles),
        "bundles": sorted(tilde(b) for p in profiles for b in (p / "bundle").glob("*/*") if b.suffix in (".app", ".dmg")),
        "days_since_build": int((time.time() - last) // 86400),
    }


def scan():
    OUT.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(8) as pool:
        targets = sorted(pool.map(inspect, find_targets()), key=lambda t: -t["cache_gb"])
    big = [t for t in targets if t["cache_gb"] >= MIN_GB]
    (OUT / "targets.tsv").write_text("".join(f"{t['path']}\t{t['cache_gb']}\n" for t in big))
    summary = {"min_gb": MIN_GB, "targets": len(targets), "total_gb": round(sum(t["gb"] for t in targets), 2),
               "reclaim_gb": round(sum(t["cache_gb"] for t in big), 2), "list": targets}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))

    print(f"Cargo target dirs: {summary['targets']}  total {summary['total_gb']:.1f} GB  "
          f"reclaimable (cache >= {MIN_GB} GB) {summary['reclaim_gb']:.1f} GB")
    for t in targets[:15]:
        print(f"  {t['gb']:6.1f} GB  cache {t['cache_gb']:6.1f} GB  last build {t['days_since_build']:4d}d  {t['project']}")
    print(f"List: {OUT / 'targets.tsv'}")


def building(profile):
    """Cargo holds an flock on <profile>/.cargo-lock while it builds."""
    lock = profile / ".cargo-lock"
    if not lock.exists():
        return False
    with open(lock) as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def clean(tsv):
    failed, freed = False, 0
    for line in Path(tsv).read_text().splitlines():
        target = Path(line.split("\t")[0])
        if not is_cargo_target(target):
            print(f"✘ {tilde(target)}: ya no es un target de Cargo, se omite")
            continue
        profiles = profile_dirs(target)
        if any(building(p) for p in profiles):
            print(f"✘ {tilde(target)}: cargo está compilando ahí, se omite")
            continue
        before = du_kb(target)
        for p in profiles:
            for s in CACHE_SUBDIRS:
                d = p / s
                if d.is_dir() and not d.is_symlink():
                    try:
                        shutil.rmtree(d)
                    except OSError as e:
                        failed = True
                        print(f"✘ {tilde(d)}: {e}")
        after = du_kb(target)
        freed += before - after
        kept = [tilde(b) for p in profiles for b in (p / "bundle").glob("*/*") if b.suffix in (".app", ".dmg")]
        print(f"✔ {tilde(target)}: {before / 1048576:.2f} GB → {after / 1048576:.2f} GB"
              + (f"  (se mantienen: {', '.join(kept)})" if kept else ""))
    print(f"Liberado: {freed / 1048576:.2f} GB. El próximo cargo build / tauri build compila de cero.")
    return failed


if __name__ == "__main__":
    if len(sys.argv) == 1:
        scan()
    elif len(sys.argv) == 3 and sys.argv[1] == "clean":
        sys.exit(1 if clean(sys.argv[2]) else 0)
    else:
        sys.exit(__doc__.strip())
