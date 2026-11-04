# Política de seguridad

[English](SECURITY.md) · **Español**

## Versiones soportadas

Solo el último commit de `main`. Actualiza con `git pull && ./install.sh`.

## Reportar una vulnerabilidad

Repórtala en privado desde [GitHub security advisories](https://github.com/francofl99/mac-optimizer/security/advisories/new). No abras un issue público.

Incluye:

- qué puede hacer un atacante y desde dónde (otra página en el navegador, otro usuario local, un reporte o archivo de configuración armado),
- pasos para reproducirlo, y las versiones de macOS y Python,
- el commit que probaste.

## Alcance

Dentro del alcance:

- El servidor del dashboard (`app/server.py`): saltear el bind a localhost, el chequeo del `Host` o el token de sesión, ejecutar comandos que no salgan de un reporte en disco, o prender o apagar elementos de arranque fuera de los permitidos.
- Archivos en `~/.mac-optimizer/` creados con permisos que otros usuarios pueden leer.
- Inyección de comandos en `install.sh`, `bin/` o el análisis programado.

Fuera del alcance:

- Comandos sugeridos por el análisis que elegiste ejecutar: el dashboard muestra cada comando y pide confirmación.
- Chequeos de seguridad que no detectan algo o lo informan mal. Son bugs; abre un issue normal.
- Cualquier cosa que requiera que el atacante ya esté ejecutando código como tu usuario.
