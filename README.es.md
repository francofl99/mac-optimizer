# mac-optimizer

[English](README.md)

Repaso periódico del Mac (RAM, swap, CPU, batería, disco, Docker y arranque) con un dashboard local. [Claude Code](https://claude.com/claude-code) analiza los datos y propone acciones. Tú revisas cada una y la ejecutas desde un botón, con confirmación.

> [!WARNING]
> Algunas acciones borran datos (caches, imágenes o volúmenes de Docker, `node_modules`). Nada se ejecuta sin tu confirmación, pero eres responsable de lo que apruebas. Lee cada comando antes de ejecutarlo. Se entrega tal cual, sin garantía (ver [LICENSE](LICENSE)).

## Cómo funciona

```
collect.sh ──────────► runs/<stamp>.txt ─┐
docker_volumes.py ───► docker/*.txt ─────┼─► Claude (skill "repaso-sistema") ─► reports/<stamp>.json ─► dashboard
                                         │     sesión interactiva o headless                            │
state.json / exec.log ◄──────────────────┴────────────── botón Ejecutar (con confirmación) ◄────────────┘
```

| Parte | Qué hace | ¿Usa Claude? |
|---|---|---|
| `bin/collect.sh` | Snapshot de solo lectura: procesos, memoria, batería, disco, caches, LaunchAgents | No |
| `bin/docker_volumes.py` | Clasifica imágenes, build cache y volúmenes por riesgo. Mira el contenido de cada volumen y protege lo que parece una base de datos | No |
| `skill/SKILL.md` | Criterios del análisis y esquema del reporte | Sí |
| `bin/analyze.sh` | `collect.sh` + `claude -p` en modo headless. Claude solo puede leer archivos y escribir en `reports/` | Sí |
| `app/server.py` + `app/dashboard.html` | Dashboard en `localhost:8765`: métricas en vivo, hallazgos, botón Ejecutar, acciones en paralelo, historial | No |

La recolección y la ejecución son deterministas. Claude solo escribe el análisis, y en modo headless no puede ejecutar comandos.

## Requisitos

- macOS (probado en Apple Silicon, macOS 26).
- El Python 3 del sistema (sin dependencias).
- [Claude Code](https://claude.com/claude-code) instalado y logueado, para el análisis.
- Opcional: Docker. Con [OrbStack](https://orbstack.dev) funciona mejor. Con Docker Desktop, la inspección de volúmenes necesita una imagen local `alpine` o `busybox`; sin ella, los volúmenes que no se pueden leer quedan como protegidos.

## Instalación

```bash
git clone https://github.com/francofl99/mac-optimizer ~/Projects/mac-optimizer
cd ~/Projects/mac-optimizer
./install.sh                      # dashboard + skill, sin rutina
./install.sh --schedule weekly --weekday 0 --hour 20   # además: repaso headless cada domingo a las 20:00
./install.sh --schedule daily --hour 9
```

`install.sh`:

- symlinkea el skill a `~/.claude/skills/repaso-sistema`,
- deja el dashboard corriendo como LaunchAgent (`com.mac-optimizer.dashboard`),
- opcionalmente programa el análisis (`com.mac-optimizer.analysis`),
- crea `~/.mac-optimizer/` (permisos `700`) con tus archivos de configuración.

Si lo vuelves a correr (por ejemplo después de un `git pull`), mantiene la rutina actual salvo que le pases `--schedule`. `./uninstall.sh` quita los agentes y el link del skill, y conserva tus datos.

## Uso

- **En una sesión de Claude Code:** `/repaso-sistema`.
- **Desde el dashboard:** el botón **Nuevo repaso** corre `analyze.sh` en modo headless.
- **Automático:** toca **Rutina** en el encabezado del dashboard y elige diaria o semanal, el día y la hora, o usa `./install.sh --schedule …`. Corre como LaunchAgent (`com.mac-optimizer.analysis`). Si el Mac está dormido a esa hora, corre al despertar; si está apagado, esa ejecución se saltea. Cuando el reporte está listo llega una notificación de macOS.

Las acciones se pueden correr en paralelo. La ventana de cada una se minimiza a la barra inferior y se vuelve a abrir con un clic. Los comandos corren sin stdin, así que si uno pide confirmación (`[y/N]`) se corta y queda marcado con ⚠ en lugar de OK.

### ¿Por qué launchd y no una rutina en la nube?

El análisis tiene que leer tu Mac (procesos, disco, Docker), así que corre localmente. Hay dos opciones:

1. **launchd + `claude -p`**, que es lo que usa este repo. Se versiona con el código y no necesita ninguna app abierta.
2. **Una tarea programada local de Claude Desktop** con el prompt `/repaso-sistema`. También funciona y la sesión queda visible en la app, pero la configuración vive en la app y no en el repo.

## Configuración (`~/.mac-optimizer/`)

| Archivo | Para qué |
|---|---|
| `protected-volumes.txt` | Patrones glob de volúmenes Docker que nunca se sugieren para borrar (por ejemplo, backups de DB) |
| `branch-volumes.txt` | Patrones de copias de DB por branch o worktree. Se sugieren con riesgo medio |
| `trusted-networks.txt` | MAC del router de las redes donde el dashboard puede buscar dispositivos conectados (se marca con el botón "Es mi red de casa") |
| `state.json` | Hallazgos que marcaste como hechos o ignorados en el dashboard |
| `exec.log` | Historial de comandos ejecutados desde el dashboard |

Variables de entorno:

- `MAC_OPTIMIZER_HOME`: carpeta de datos.
- `MAC_OPTIMIZER_PROJECT_DIRS`: dónde buscar `node_modules` viejos. Por defecto: `~/Projects ~/Desktop ~/code`.
- `MAC_OPTIMIZER_MODEL`: modelo para el análisis headless.

## Privacidad

- Los snapshots, reportes y logs quedan en `~/.mac-optimizer/`, en tu máquina. Este proyecto no sube nada, y el repo no contiene datos personales.
- **El paso de análisis envía el snapshot a Anthropic a través de Claude Code**, como cualquier otra sesión de Claude. El snapshot incluye nombres de procesos y apps, rutas y tamaños de carpetas, nombres de proyectos, nombres de contenedores, imágenes y volúmenes Docker, LaunchAgents y datos de la batería. No incluye contenido de archivos, variables de entorno ni credenciales. Si eso no te sirve, no corras el análisis: los recolectores y el dashboard funcionan sin él.

## Seguridad

- El servidor escucha solo en `127.0.0.1` y rechaza pedidos con un `Host` que no sea localhost, lo que bloquea ataques de DNS rebinding.
- Ejecutar un comando requiere un token que se regenera en cada arranque y solo está embebido en el dashboard. Otras páginas abiertas en el navegador no pueden disparar comandos.
- El comando siempre sale del reporte guardado en disco, nunca del navegador.
- Los comandos con `sudo` nunca se ejecutan desde el dashboard.
- Las acciones de riesgo medio o alto piden escribir `EJECUTAR` para confirmar.
- Docker: nunca se usa `volume prune`. Los volúmenes se borran desde listas explícitas, y `docker volume rm` rechaza cualquiera que esté en uso.

¿Encontraste una vulnerabilidad? Repórtala en privado desde [GitHub security advisories](https://github.com/francofl99/mac-optimizer/security/advisories/new), no en un issue público.

## Problemas comunes

| Síntoma | Solución |
|---|---|
| "Nuevo repaso" falla con un error de permisos | Actualiza Claude Code. El modo headless usa `--permission-mode dontAsk` y reglas `Edit(//ruta/absoluta/**)` |
| `claude: command not found` desde launchd | Vuelve a correr `./install.sh`: guarda el `PATH` de tu shell en el LaunchAgent |
| El dashboard no responde | `launchctl kickstart -k gui/$(id -u)/com.mac-optimizer.dashboard`, y revisa `~/.mac-optimizer/logs/` |
| La recolección tarda varios minutos | `docker system df -v` es lento cuando hay cientos de volúmenes. Corre en paralelo con el resto |

## Licencia

[MIT](LICENSE)
