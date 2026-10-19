#!/bin/bash
# Usage: ./install.sh [--schedule off|daily|weekly] [--weekday 0-6 (0=domingo)] [--hour 0-23] [--port 8765]
# Without --schedule the current schedule is kept (it can also be edited from the dashboard).
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
DATA="${MAC_OPTIMIZER_HOME:-$HOME/.mac-optimizer}"
AGENTS="$HOME/Library/LaunchAgents"
SCHEDULE=keep WEEKDAY=1 HOUR=10 PORT=8765
while [ $# -gt 0 ]; do
  case "$1" in
    --schedule) SCHEDULE="$2"; shift 2 ;;
    --hour) HOUR="$2"; shift 2 ;;
    --weekday) WEEKDAY="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    *) echo "Opción desconocida: $1"; exit 1 ;;
  esac
done
case "$SCHEDULE" in keep|off|daily|weekly) ;; *) echo "--schedule debe ser off, daily o weekly"; exit 1 ;; esac

mkdir -p "$DATA"/{runs,reports,docker,logs,backup} "$AGENTS"
# Snapshots list processes, paths and project names: keep them private to the user.
chmod 700 "$DATA"
ln -sfn "$REPO" "$DATA/repo"

for f in protected-volumes branch-volumes; do
  [ -f "$DATA/$f.txt" ] || cp "$REPO/config/$f.example.txt" "$DATA/$f.txt"
done

SKILL="$HOME/.claude/skills/repaso-sistema"
mkdir -p "$(dirname "$SKILL")"
if [ -e "$SKILL" ] && [ ! -L "$SKILL" ]; then
  mv "$SKILL" "$DATA/backup/skill-$(date +%Y%m%d%H%M%S)"
fi
ln -sfn "$REPO/skill" "$SKILL"
echo "✔ Skill /repaso-sistema → $REPO/skill"

# launchd starts jobs with a bare PATH; docker, brew and claude live in the user's shell PATH.
USER_PATH="$(/bin/zsh -ilc 'echo __P__$PATH' 2>/dev/null </dev/null | sed -n 's/.*__P__//p' | tail -1)"
USER_PATH="${USER_PATH:-/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"

load_agent() {
  local label="$1" plist="$AGENTS/$1.plist"
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$plist"
}

write_plist() {
  local label="$1" program="$2" extra="$3"
  cat > "$AGENTS/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>$program</array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$USER_PATH</string>
    <key>MAC_OPTIMIZER_HOME</key><string>$DATA</string>
    <key>MAC_OPTIMIZER_PORT</key><string>$PORT</string>
  </dict>
  <key>StandardOutPath</key><string>$DATA/logs/$label.log</string>
  <key>StandardErrorPath</key><string>$DATA/logs/$label.log</string>
  $extra
</dict>
</plist>
EOF
}

write_plist com.mac-optimizer.dashboard \
  "<string>/usr/bin/python3</string><string>$REPO/app/server.py</string><string>--port</string><string>$PORT</string>" \
  "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"
load_agent com.mac-optimizer.dashboard
echo "✔ Dashboard en http://localhost:$PORT (arranca con el login)"

if [ "$SCHEDULE" != keep ]; then
  MAC_OPTIMIZER_HOME="$DATA" python3 "$REPO/app/schedule.py" "$SCHEDULE" --weekday "$WEEKDAY" --hour "$HOUR" --port "$PORT" >/dev/null
fi
MAC_OPTIMIZER_HOME="$DATA" python3 - "$REPO/app/schedule.py" <<'PY'
import json, subprocess, sys
s = json.loads(subprocess.run([sys.executable, sys.argv[1], "show"], capture_output=True, text=True).stdout)
days = ["domingo", "lunes", "martes", "miércoles", "jueves", "viernes", "sábado"]
if s["mode"] == "off":
    print("• Rutina automática: desactivada (actívala desde el dashboard o con --schedule)")
else:
    when = "todos los días" if s["mode"] == "daily" else f"cada {days[s['weekday']]}"
    print(f"✔ Rutina automática: {when} a las {s['hour']:02d}:{s['minute']:02d} · próxima: {s['next_run']}")
PY

command -v claude >/dev/null || echo "⚠ No se encontró 'claude' en el PATH: el análisis headless no va a funcionar."
