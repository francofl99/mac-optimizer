---
name: repaso-sistema
description: Repaso del Mac (procesos, RAM, CPU, disco, batería, Docker, arranque) con recomendaciones de optimización y dashboard local (mac-optimizer). Usar cuando el usuario pida "repaso del sistema", "qué puedo optimizar", "/repaso-sistema", "abrir el dashboard del repaso", o cuando lo dispare la rutina programada.
---

# Repaso del sistema (mac-optimizer)

Solo diagnóstico. Nunca borrar archivos, matar procesos, desinstalar ni cambiar ajustes por tu cuenta. Las acciones las ejecuta el usuario desde el dashboard (botón con confirmación) o te las pide explícitamente en el chat, una por una.

- Código: `~/.mac-optimizer/repo` (symlink al repo, creado por `install.sh`).
- Datos (`~/.mac-optimizer/`, o `$MAC_OPTIMIZER_HOME`):
  - `runs/<stamp>.txt`: snapshots.
  - `reports/<stamp>.json`: reportes.
  - `docker/summary.json` y `docker/volumes-*.txt`: clasificación de Docker.
  - `state.json`: lo que el usuario marcó en el dashboard.
  - `exec.log`: acciones ejecutadas.
  - `protected-volumes.txt` y `branch-volumes.txt`: patrones del usuario.

## Pasos

1. Ejecutar `bash ~/.mac-optimizer/repo/bin/collect.sh` en background (tarda unos 3-4 min). Imprime la ruta del snapshot. En modo headless este paso ya viene hecho.
2. Leer el snapshot completo y `docker/summary.json`.
3. Si hay reportes previos en `reports/`, leer el más reciente y comparar (crecimiento de disco, swap, procesos nuevos, ciclos de batería) → campo `changes`.
4. Leer `state.json` y `exec.log`:
   - Los hallazgos con `status: "ignored"` no se vuelven a sugerir.
   - Para los `done` o ejecutados, verificar si se liberó lo esperado y mencionarlo en `changes`.
5. Escribir `reports/<mismo-stamp-que-el-snapshot>.json` con el esquema de abajo.
6. En una sesión interactiva: asegurar que el dashboard esté arriba (`curl -s localhost:8765/api/reports`; si no responde, `launchctl kickstart gui/$(id -u)/com.mac-optimizer.dashboard`) y responder con un resumen corto: estado general, top 3 acciones con impacto y el link http://localhost:8765.

## Esquema del reporte

```json
{
  "id": "2026-10-18_0026", "date": "2026-10-18T00:26",
  "health": "ok|warn|bad",
  "summary": "1-2 frases",
  "metrics": { "swap_used_gb": 0, "compressed_gb": 0, "mem_free_pct": 0, "disk_free_gb": 0, "load1": 0, "battery_cycles": 0, "battery_max_capacity": 0, "processes": 0 },
  "disk_breakdown": [{ "label": "...", "gb": 0, "kind": "docker|cache|app|dev|user|system" }],
  "docker": { "images_gb": 0, "images_reclaim_gb": 0, "volumes_gb": 0, "volumes_reclaim_gb": 0, "build_cache_gb": 0, "build_cache_reclaim_gb": 0, "vm_disk_gb": 0, "running": 0 },
  "findings": [{ "id": "slug-estable", "area": "disk|ram|cpu|battery|startup", "title": "...", "detail": "...", "impact": "~39 GB", "savings_gb": 0, "risk": "bajo|medio|alto", "command": "..." }],
  "changes": ["..."]
}
```

- `id` de los hallazgos: un slug **estable** entre repasos (por ejemplo `docker-volumes-low-risk`), así el estado guardado en `state.json` se mantiene.
- `savings_gb` solo para disco (lo usa el total "recuperable"). Para RAM, CPU o batería: 0, y el impacto se describe en `impact`.
- `docker.volumes_reclaim_gb` = solo los volúmenes de riesgo bajo, nunca los protegidos.
- `disk_breakdown`: sin contar dos veces una carpeta que está dentro de otra.
- Máximo ~15 hallazgos, ordenados por impacto. Un hallazgo informativo "Protegido: no se toca" con `command` vacío ayuda a dar tranquilidad.

### `command`

El dashboard lo ejecuta con un botón (`bash -c`, sin TTY ni stdin, desde `$HOME`, con el PATH del login de zsh). Por eso tiene que ser:

- no interactivo y autocontenido: stdin es `/dev/null`, así que cualquier `[y/N]` se corta y el dashboard lo marca como "pidió confirmación". Siempre `-f`/`--force`/`-y` (`docker image prune -a -f`, `docker builder prune -a -f`, `brew cleanup` no pregunta). Nada de `nvm` sin `source ~/.nvm/nvm.sh`.
- sin `sudo` (si hace falta, el dashboard lo muestra como "requiere sudo" y no lo ejecuta),
- verboso (`-v`), para que la salida muestre qué hizo,
- reversible cuando se pueda: mover a `~/.Trash` con nombres únicos o a `~/.mac-optimizer/backup/`.
- Vacío si la acción es manual (reiniciar, ajustes del sistema).

El `risk` define la confirmación: con `bajo` alcanza un clic; con `medio` o `alto` hay que escribir EJECUTAR.

## Criterios

- **RAM**: memory pressure, swap > 2 GB, apps con muchos helpers (Electron, navegadores), contenedores Docker corriendo hace días, procesos huérfanos.
- **CPU / batería**: energy impact alto sostenido, assertions que impiden el reposo, servicios nativos (mysqld, postgres) que duplican los de Docker, salud de la batería y hábitos de carga.
- **Disco**: Docker, caches regenerables, node_modules inactivos, restos de apps desinstaladas, Downloads, papelera.
- **Arranque**: LaunchAgents/Daemons de apps que ya no están en /Applications.
- Distinguir cache regenerable (riesgo bajo) de datos de usuario (riesgo medio/alto).

### Docker: reglas duras

- Nunca sugerir `docker volume prune` ni `docker system prune --volumes`. Los volúmenes se borran solo desde las listas que genera `bin/docker_volumes.py`:
  - `xargs docker volume rm < ~/.mac-optimizer/docker/volumes-low-risk.txt` → riesgo bajo.
  - `xargs docker volume rm < ~/.mac-optimizer/docker/volumes-branch.txt` → riesgo medio. Avisar que hay que revisar la lista y sacar los branches que sigan activos.
- `volumes-protected.txt` y los volúmenes en uso no se tocan nunca. El clasificador ya aplica los patrones del usuario y detecta archivos de bases de datos.
- Imágenes: `docker image prune -a -f --filter until=720h` respeta las que usa cualquier contenedor. Listar las que son `local_build` en `summary.json`, porque habría que reconstruirlas.
- Build cache: `docker builder prune -a -f`.
