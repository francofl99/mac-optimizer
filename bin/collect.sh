#!/bin/bash
# Read-only snapshot of macOS resource usage. Never deletes or kills anything.
set -u

DATA="${MAC_OPTIMIZER_HOME:-$HOME/.mac-optimizer}"
HERE="$(cd "$(dirname "$0")" && pwd)"
read -r -a PROJECT_DIRS <<< "${MAC_OPTIMIZER_PROJECT_DIRS:-$HOME/Projects $HOME/Desktop $HOME/code}"
mkdir -p "$DATA/runs"
OUT="$DATA/runs/$(date +%Y-%m-%d_%H%M).txt"

section() { printf '\n===== %s =====  (t+%ss)\n' "$1" "$SECONDS"; }
size_of() { [ -e "$1" ] && du -sk "$1" 2>/dev/null | awk -v p="$1" '{printf "%8.1f GB  %s\n", $1/1048576, p}'; }

# `docker system df -v` takes minutes with many volumes; start it early so it overlaps the du scans.
DOCKER_DF="$(mktemp)"
python3 "$HERE/docker_volumes.py" > "$DOCKER_DF" 2>&1 &
DOCKER_PID=$!

{
section "SYSTEM"
date
sw_vers | tr '\n' ' '; echo
sysctl -n machdep.cpu.brand_string
echo "RAM: $(( $(sysctl -n hw.memsize) / 1073741824 )) GB  CPUs: $(sysctl -n hw.ncpu)"
uptime

section "MEMORY"
memory_pressure -Q 2>/dev/null | tail -1
sysctl -n vm.swapusage
vm_stat | awk -v ps="$(pagesize)" '
  /Pages (free|active|inactive|wired down|occupied by compressor|purgeable)/ {
    gsub(/\./,"",$NF); sub(/:$/,"",$0); n=$0; sub(/ +[0-9]+$/,"",n);
    printf "%-40s %6.2f GB\n", n, $NF*ps/1073741824 }
  /Swapins|Swapouts/ { print }'

section "TOP RSS BY APP (aggregated, MB)"
ps -Axo rss=,comm= | awk '{
    rss=$1; $1=""; c=$0; sub(/^ /,"",c);
    if (match(c, /\/[^\/]+\.app\//)) { a=substr(c,RSTART+1,RLENGTH-2) } else { n=split(c,p,"/"); a=p[n] }
    m[a]+=rss; k[a]++ }
  END { for (a in m) printf "%8.0f MB  %4d procs  %s\n", m[a]/1024, k[a], a }' | sort -rn | head -20

section "TOP CPU PROCESSES (instant)"
ps -Arco pid=,pcpu=,pmem=,etime=,comm= | head -15

section "ENERGY IMPACT (top power sample)"
top -l 2 -s 2 -o power -n 15 -stats pid,command,power,cpu,idlew 2>/dev/null | awk '/^PID/{n++} n==2'

section "BATTERY"
pmset -g batt | tail -n +2
system_profiler SPPowerDataType 2>/dev/null | grep -E "Cycle Count|Condition|Maximum Capacity|Low Power Mode"
pmset -g | grep -E "lowpowermode|displaysleep|sleep |powernap|tcpkeepalive|womp"

section "SLEEP-PREVENTING ASSERTIONS"
pmset -g assertions 2>/dev/null | sed -n '/Listed by owning process/,$p' | grep -vE "^\s*$" | head -25

section "DISK"
df -h / /System/Volumes/Data 2>/dev/null
echo
for p in \
  "$HOME/Library/Developer/Xcode/DerivedData" \
  "$HOME/Library/Developer/Xcode/Archives" \
  "$HOME/Library/Developer/Xcode/iOS DeviceSupport" \
  "$HOME/Library/Developer/CoreSimulator" \
  "$HOME/Library/Containers/com.docker.docker" \
  "$HOME/.docker" \
  "$HOME/Library/Logs" \
  "$HOME/.Trash" \
  "$HOME/Downloads" \
  "$HOME/.npm" \
  "$HOME/Library/pnpm" \
  "$HOME/.pnpm-store" \
  "$HOME/.nvm/versions" \
  "$HOME/.cache" \
  "$HOME/.gradle" \
  "$HOME/.m2" \
  "$HOME/.composer" \
  "$HOME/Library/Group Containers/HUAQ24HBR6.dev.orbstack"; do
  size_of "$p"
done | sort -rn

for dir in "$HOME/Library/Application Support" "$HOME/Library/Caches" "$HOME/.cache"; do
  section "LARGEST $dir (total + top 10)"
  du -sk "$dir"/* 2>/dev/null | sort -rn > "$DOCKER_DF.du"
  awk '{t+=$1} END {printf "%8.1f GB TOTAL\n", t/1048576}' "$DOCKER_DF.du"
  head -10 "$DOCKER_DF.du" | awk '{s=$1; $1=""; printf "%8.1f GB %s\n", s/1048576, $0}'
done
rm -f "$DOCKER_DF.du"

section "STALE node_modules in ${PROJECT_DIRS[*]} (untouched >60d)"
find "${PROJECT_DIRS[@]}" -maxdepth 5 -type d -name node_modules -prune -mtime +60 2>/dev/null \
  | while read -r d; do size_of "$d"; done | sort -rn | head -15

section "FILES > 500MB (Downloads, Documents, Movies, project dirs)"
find "$HOME/Downloads" "$HOME/Documents" "$HOME/Movies" "${PROJECT_DIRS[@]}" -xdev \
  \( -name node_modules -o -name .git -o -name vendor \) -prune -o -type f -size +500M -print 2>/dev/null \
  | while read -r f; do size_of "$f"; done | sort -rn | head -15

section "DOCKER"
wait "$DOCKER_PID" 2>/dev/null; cat "$DOCKER_DF"
if docker info >/dev/null 2>&1; then
  echo; echo "Running containers:"
  docker ps --format '{{.Names}}\t{{.Status}}' | head -30
  echo; docker stats --no-stream --format '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}' 2>/dev/null | sort -t$'\t' -k3 -rh | head -15
  echo "OrbStack disk image (allocated): $(du -sh "$HOME/Library/Group Containers/HUAQ24HBR6.dev.orbstack/data" 2>/dev/null | cut -f1)"
fi
command -v orb >/dev/null && echo "OrbStack: $(orb status 2>/dev/null)  memory_mib: $(orb config get memory_mib 2>/dev/null)"

section "HOMEBREW"
if command -v brew >/dev/null; then
  export HOMEBREW_NO_AUTO_UPDATE=1
  brew cleanup -n 2>/dev/null | tail -1
  echo "Services:"; brew services list 2>/dev/null | awk 'NR>1 && $2!="none"'
fi

section "NVM NODE VERSIONS"
ls "$HOME/.nvm/versions/node" 2>/dev/null

section "LAUNCH AGENTS / DAEMONS (non-Apple)"
for d in "$HOME/Library/LaunchAgents" /Library/LaunchAgents /Library/LaunchDaemons; do
  echo "-- $d"; ls "$d" 2>/dev/null | grep -v '^com\.apple\.'
done
echo "-- Loaded non-Apple user jobs (pid, label)"
launchctl list 2>/dev/null | awk 'NR>1 && $3 !~ /^com\.apple\./ && $3 !~ /^application\.com\.apple/ {print $1"\t"$3}' | sort | head -40

section "NATIVE SERVICES"
pgrep -l 'mysqld|postgres|redis-server|mongod' 2>/dev/null

section "PROCESS COUNT"
echo "Total processes: $(ps -A | wc -l | tr -d ' ')"
echo "Helper/Renderer procs: $(ps -Axo comm= | grep -ciE 'helper|renderer')"
section "DONE"
} > "$OUT" 2>&1
rm -f "$DOCKER_DF"

echo "$OUT"
