# mac-optimizer

Repaso periódico del Mac (RAM, swap, CPU, batería, disco, Docker y arranque) con un dashboard local. Claude analiza los datos y propone acciones; tú las ejecutas desde un botón, con confirmación.

## Cómo funciona

```
collect.sh ──► runs/<stamp>.txt ─┐
docker_volumes.py ─► docker/*.txt ┼─► Claude (skill repaso-sistema) ─► reports/<stamp>.json ─► dashboard
                                 │        interactivo o headless                                 │
state.json / exec.log ◄──────────┴────────────────────────── botón Ejecutar (con confirmación) ◄─┘
```

| Parte | Qué hace | ¿Usa Claude? |
|---|---|---|
| `bin/collect.sh` | Snapshot de solo lectura: procesos, memoria, batería, disco, caches, LaunchAgents | No |
| `bin/docker_volumes.py` | Clasifica imágenes, build cache y volúmenes por riesgo. Mira el contenido de cada volumen y protege los que tienen bases de datos | No |
| `skill/SKILL.md` | Criterios y esquema del análisis | Sí |
| `bin/analyze.sh` | `collect.sh` + `claude -p` en modo headless, con permisos de solo lectura y escritura solo en `reports/` | Sí |
| `app/server.py` + `app/dashboard.html` | Dashboard en `localhost:8765`: métricas en vivo, hallazgos, botón Ejecutar, historial | No |

La recolección y la ejecución son deterministas. Claude solo interviene en el análisis, y en modo headless no puede ejecutar comandos.

## Instalación

```bash
git clone https://github.com/francofl99/mac-optimizer ~/Projects/mac-optimizer
cd ~/Projects/mac-optimizer
./install.sh                      # dashboard + skill, sin rutina automática
./install.sh --schedule weekly    # además: repaso headless cada lunes a las 10:00
./install.sh --schedule daily --hour 9
```

`install.sh`:

- symlinkea el skill a `~/.claude/skills/repaso-sistema`,
- deja el dashboard corriendo como LaunchAgent (`com.mac-optimizer.dashboard`),
- opcionalmente programa el análisis (`com.mac-optimizer.analysis`),
- crea `~/.mac-optimizer/` con tus archivos de configuración.

Se puede correr de nuevo para cambiar la frecuencia. `./uninstall.sh` quita los agentes y conserva los datos.

Requisitos: macOS, Python 3 (el del sistema alcanza), [Claude Code](https://claude.com/claude-code) logueado. Docker/OrbStack es opcional.

## Uso

- **Manual, en una sesión de Claude Code:** `/repaso-sistema`.
- **Manual, desde el dashboard:** botón **Nuevo repaso** (corre `analyze.sh` en headless).
- **Automático:** `./install.sh --schedule weekly`. Al terminar llega una notificación de macOS.

Las acciones se pueden correr en paralelo. La ventana de cada una se minimiza a la barra inferior y se vuelve a abrir con un clic. Si un comando pide confirmación (`[y/N]`), se corta, porque no hay stdin, y queda marcado con ⚠ en lugar de OK.

### ¿Por qué no se usa una rutina en la nube o una tarea de la app?

Necesita leer tu Mac (procesos, disco, Docker), así que tiene que correr localmente. Las opciones son:

1. **launchd + `claude -p`** (lo que usa este repo): vive en el repo, se versiona y no depende de que la app esté abierta.
2. **Tarea programada local de Claude Desktop** con el prompt `/repaso-sistema`: también funciona y deja la sesión visible en la app, pero la configuración vive en la app, no en el repo.

## Configuración (`~/.mac-optimizer/`)

| Archivo | Para qué |
|---|---|
| `protected-volumes.txt` | Patrones glob de volúmenes Docker que nunca se sugieren (por ejemplo, backups de DB) |
| `branch-volumes.txt` | Patrones de copias por branch o worktree: se sugieren con riesgo medio |
| `state.json` | Hallazgos marcados como hecho o ignorado desde el dashboard |
| `exec.log` | Historial de comandos ejecutados desde el dashboard |

Variables de entorno: `MAC_OPTIMIZER_HOME`, `MAC_OPTIMIZER_PROJECT_DIRS` (por defecto `~/Projects ~/Desktop ~/code`), `MAC_OPTIMIZER_MODEL`.

## Seguridad

- El servidor escucha solo en `127.0.0.1` y rechaza pedidos con un `Host` que no sea localhost (protección contra DNS rebinding).
- Ejecutar algo requiere un token que se genera en cada arranque y solo está embebido en el dashboard: otras páginas abiertas en el navegador no pueden disparar comandos.
- El comando siempre sale del reporte guardado en disco, nunca del navegador.
- Los comandos con `sudo` no se ejecutan desde el dashboard.
- Riesgo medio o alto: hay que escribir `EJECUTAR` para confirmar.
- Docker: nunca se usa `volume prune`. Los volúmenes se borran desde listas explícitas, y `docker volume rm` rechaza cualquiera que esté en uso.
