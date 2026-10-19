#!/usr/bin/env python3
"""Manage the launchd agent that runs the headless analysis.

Usage: schedule.py show | off | daily --hour H [--minute M] | weekly --weekday D --hour H [--minute M]
Weekday: 0 = Sunday … 6 = Saturday (launchd convention).
"""
import argparse
import json
import os
import plistlib
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

LABEL = "com.mac-optimizer.analysis"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
DATA = Path(os.environ.get("MAC_OPTIMIZER_HOME", Path.home() / ".mac-optimizer"))
REPO = Path(__file__).resolve().parent.parent
LOG = DATA / "logs" / f"{LABEL}.log"
DOMAIN = f"gui/{os.getuid()}"


def user_path():
    # launchd starts jobs with a bare PATH; docker, brew and claude live in the user's shell PATH.
    try:
        out = subprocess.run(["/bin/zsh", "-ilc", "echo __P__$PATH"], capture_output=True, text=True,
                             stdin=subprocess.DEVNULL, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    m = re.search(r"__P__(.+)", out)
    path = m.group(1).strip() if m else os.environ.get("PATH", "")
    return path if "/usr/bin" in path.split(":") else f"{path}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def loaded():
    return subprocess.run(["launchctl", "print", f"{DOMAIN}/{LABEL}"], capture_output=True).returncode == 0


def next_run(weekday, hour, minute, now=None):
    now = now or datetime.now()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    for _ in range(8):
        # Python weekday(): Monday=0; launchd: Sunday=0.
        if candidate > now and (weekday is None or (candidate.weekday() + 1) % 7 == weekday):
            return candidate
        candidate += timedelta(days=1)
    return None


def show():
    info = {"mode": "off", "weekday": None, "hour": None, "minute": None, "loaded": loaded(), "next_run": None,
            "last_run": datetime.fromtimestamp(LOG.stat().st_mtime).isoformat(timespec="minutes") if LOG.exists() else None}
    if not PLIST.exists():
        return info
    cal = plistlib.loads(PLIST.read_bytes()).get("StartCalendarInterval", {})
    weekday = cal.get("Weekday")
    info.update(mode="weekly" if weekday is not None else "daily", weekday=weekday % 7 if weekday is not None else None,
                hour=cal.get("Hour", 0), minute=cal.get("Minute", 0))
    nxt = next_run(info["weekday"], info["hour"], info["minute"])
    info["next_run"] = nxt.isoformat(timespec="minutes") if nxt else None
    return info


def apply(mode, weekday=None, hour=10, minute=0, port=None):
    if mode not in ("off", "daily", "weekly"):
        raise ValueError("mode debe ser off, daily o weekly")
    subprocess.run(["launchctl", "bootout", f"{DOMAIN}/{LABEL}"], capture_output=True)
    if mode == "off":
        PLIST.unlink(missing_ok=True)
        return show()
    hour, minute = int(hour), int(minute)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("hora inválida")
    cal = {"Hour": hour, "Minute": minute}
    if mode == "weekly":
        weekday = int(weekday)
        if not 0 <= weekday <= 6:
            raise ValueError("weekday debe ser 0 (domingo) a 6 (sábado)")
        cal["Weekday"] = weekday
    LOG.parent.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(REPO / "bin/analyze.sh")],
        "EnvironmentVariables": {"PATH": user_path(), "MAC_OPTIMIZER_HOME": str(DATA),
                                 "MAC_OPTIMIZER_PORT": str(port or os.environ.get("MAC_OPTIMIZER_PORT", 8765))},
        "StartCalendarInterval": cal,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }))
    res = subprocess.run(["launchctl", "bootstrap", DOMAIN, str(PLIST)], capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap falló: {res.stderr.strip()}")
    return show()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["show", "off", "daily", "weekly"])
    ap.add_argument("--weekday", type=int)
    ap.add_argument("--hour", type=int, default=10)
    ap.add_argument("--minute", type=int, default=0)
    ap.add_argument("--port", type=int)
    a = ap.parse_args()
    print(json.dumps(show() if a.mode == "show" else apply(a.mode, a.weekday, a.hour, a.minute, a.port), ensure_ascii=False))
