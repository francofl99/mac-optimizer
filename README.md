# mac-optimizer

[Español](README.es.md)

A periodic health check for your Mac (RAM, swap, CPU, battery, disk, Docker and login items) with a local dashboard. [Claude Code](https://claude.com/claude-code) analyzes the data and suggests actions. You review each one and run it with a button, after confirming.

> [!WARNING]
> Some suggested actions delete data (caches, Docker images or volumes, `node_modules`). Nothing runs without your confirmation, but you are responsible for what you approve. Read every command before running it. Provided as is, with no warranty (see [LICENSE](LICENSE)).

## How it works

```
collect.sh ──────────► runs/<stamp>.txt ─┐
docker_volumes.py ───► docker/*.txt ─────┼─► Claude (skill "repaso-sistema") ─► reports/<stamp>.json ─► dashboard
                                         │     interactive session or headless                          │
state.json / exec.log ◄──────────────────┴───────────────── "Run" button (with confirmation) ◄──────────┘
```

| Component | What it does | Uses Claude? |
|---|---|---|
| `bin/collect.sh` | Read-only snapshot: processes, memory, battery, disk usage, caches, LaunchAgents | No |
| `bin/docker_volumes.py` | Classifies Docker images, build cache and volumes by risk. It inspects each volume's contents and protects anything that looks like a database | No |
| `skill/SKILL.md` | Analysis criteria and the report schema | Yes |
| `bin/analyze.sh` | `collect.sh` + headless `claude -p`. Claude can only read files and write to `reports/` | Yes |
| `app/server.py` + `app/dashboard.html` | Dashboard on `localhost:8765`: live metrics, findings, Run button, concurrent jobs, history | No |

Data collection and command execution are deterministic. Claude only writes the analysis, and in headless mode it cannot run commands.

## Requirements

- macOS (tested on Apple Silicon, macOS 26).
- The system Python 3 (no dependencies).
- [Claude Code](https://claude.com/claude-code) installed and logged in, for the analysis.
- Optional: Docker. [OrbStack](https://orbstack.dev) gives the best results. With Docker Desktop, volume inspection needs a local `alpine` or `busybox` image; without one, unreadable volumes are classified as protected.

The dashboard UI is in Spanish.

## Install

```bash
git clone https://github.com/francofl99/mac-optimizer ~/Projects/mac-optimizer
cd ~/Projects/mac-optimizer
./install.sh                      # dashboard + skill, no schedule
./install.sh --schedule weekly --weekday 0 --hour 20   # also run a headless check every Sunday at 20:00
./install.sh --schedule daily --hour 9
```

`install.sh`:

- symlinks the skill to `~/.claude/skills/repaso-sistema`,
- runs the dashboard as a LaunchAgent (`com.mac-optimizer.dashboard`),
- optionally schedules the analysis (`com.mac-optimizer.analysis`),
- creates `~/.mac-optimizer/` (mode `700`) with your config files.

Re-running it (for example after `git pull`) keeps your current schedule unless you pass `--schedule`. `./uninstall.sh` removes the agents and the skill link and keeps your data.

## Usage

- **In a Claude Code session:** `/repaso-sistema`.
- **From the dashboard:** the **Nuevo repaso** button runs `analyze.sh` headless.
- **On a schedule:** click **Rutina** in the dashboard header to pick daily or weekly, the day and the time, or use `./install.sh --schedule …`. It runs as a LaunchAgent (`com.mac-optimizer.analysis`). If the Mac is asleep at that time, it runs on wake; if it's off, that run is skipped. You get a macOS notification when the report is ready.

Several actions can run at the same time. Each job window can be minimized to the bottom bar and reopened with a click. Commands run with no stdin, so a command that asks for confirmation (`[y/N]`) is aborted and flagged with ⚠ instead of reporting success.

### Why launchd and not a cloud routine?

The analysis has to read your machine (processes, disk, Docker), so it runs locally. You have two options:

1. **launchd + `claude -p`**, which is what this repo uses. It's versioned with the code and doesn't need any app to be open.
2. **A scheduled local task in Claude Desktop** with the prompt `/repaso-sistema`. It also works, and the session shows up in the app, but the configuration lives in the app instead of in the repo.

## Configuration (`~/.mac-optimizer/`)

| File | Purpose |
|---|---|
| `protected-volumes.txt` | Glob patterns for Docker volumes that are never suggested for deletion (e.g. DB backups) |
| `branch-volumes.txt` | Glob patterns for per-branch or worktree DB copies. These are suggested with medium risk |
| `trusted-networks.txt` | Router MACs of the networks where the dashboard may look for connected devices (set from the dashboard's "Es mi red de casa" button) |
| `state.json` | Findings you marked as done or ignored in the dashboard |
| `exec.log` | History of commands run from the dashboard |

Environment variables:

- `MAC_OPTIMIZER_HOME`: data directory.
- `MAC_OPTIMIZER_PROJECT_DIRS`: where to look for stale `node_modules`. Default: `~/Projects ~/Desktop ~/code`.
- `MAC_OPTIMIZER_MODEL`: model for headless analysis.

## Privacy

- Snapshots, reports and logs stay in `~/.mac-optimizer/` on your machine. Nothing is uploaded by this project, and nothing personal lives in the repo.
- **The analysis step sends the snapshot to Anthropic through Claude Code**, like any other Claude session. The snapshot includes process and app names, folder paths and sizes, project directory names, Docker container, image and volume names, LaunchAgent names and battery stats. It does not include file contents, environment variables or credentials. If that's not acceptable, don't run the analysis: the collectors and the dashboard work without it.

## Security model

- The server listens on `127.0.0.1` only and rejects requests whose `Host` header isn't localhost, which blocks DNS rebinding.
- Running a command requires a token that is regenerated every time the server starts and is embedded only in the dashboard page. Other sites open in your browser can't trigger commands.
- The command always comes from the report file on disk, never from the browser request.
- Commands that use `sudo` are never run from the dashboard.
- Medium and high risk actions require typing `EJECUTAR` to confirm.
- Docker: the tool never uses `volume prune`. Volumes are deleted from explicit lists, and `docker volume rm` refuses any volume that is in use.

Found a vulnerability? Please report it privately through [GitHub security advisories](https://github.com/francofl99/mac-optimizer/security/advisories/new) rather than opening a public issue.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Nuevo repaso" fails with a permission error | Update Claude Code. The headless run relies on `--permission-mode dontAsk` and `Edit(//abs/path/**)` rules |
| `claude: command not found` from launchd | Re-run `./install.sh`: it captures your login shell `PATH` into the LaunchAgent |
| The dashboard is not reachable | `launchctl kickstart -k gui/$(id -u)/com.mac-optimizer.dashboard`, and check `~/.mac-optimizer/logs/` |
| Collection takes several minutes | `docker system df -v` is slow when there are hundreds of volumes. It runs in parallel with the other scans |

## License

[MIT](LICENSE)
