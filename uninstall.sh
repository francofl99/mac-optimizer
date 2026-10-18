#!/bin/bash
# Removes launchd agents and the skill symlink. Data in ~/.mac-optimizer is kept.
set -u
for label in com.mac-optimizer.dashboard com.mac-optimizer.analysis; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
[ -L "$HOME/.claude/skills/repaso-sistema" ] && rm "$HOME/.claude/skills/repaso-sistema"
echo "✔ Desinstalado. Datos conservados en ${MAC_OPTIMIZER_HOME:-$HOME/.mac-optimizer}"
