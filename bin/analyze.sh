#!/bin/bash
# Headless repaso: deterministic collection, then Claude writes the report.
# Claude only gets read access plus write access to reports/: it can never run cleanup commands.
# Permission rules need "//" for absolute paths; "/" would be relative to the project root.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
DATA="${MAC_OPTIMIZER_HOME:-$HOME/.mac-optimizer}"
mkdir -p "$DATA/reports"

echo "→ Recolectando datos (3-4 min)…"
SNAP="$(bash "$HERE/collect.sh")"
STAMP="$(basename "$SNAP" .txt)"
REPORT="$DATA/reports/$STAMP.json"
echo "→ Snapshot: $SNAP"

PROMPT="Modo headless del skill repaso-sistema. Las instrucciones están en $REPO/skill/SKILL.md: léelas y sigue los pasos desde el 2 en adelante.
El snapshot ya fue generado: $SNAP (y $DATA/docker/summary.json).
Escribe el reporte en $REPORT respetando exactamente el esquema del skill.
No ejecutes comandos ni acciones de limpieza; solo lee archivos y escribe el reporte.
Al terminar, responde con una sola línea: el campo summary del reporte."

echo "→ Claude analizando…"
cd "$DATA"
claude -p "$PROMPT" \
  --permission-mode dontAsk \
  --allowedTools "Read" "Glob" "Grep" "Edit(/$DATA/reports/**)" \
  --add-dir "$REPO" \
  --no-session-persistence \
  ${MAC_OPTIMIZER_MODEL:+--model "$MAC_OPTIMIZER_MODEL"}

python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$REPORT" || { echo "✘ El reporte no existe o no es JSON válido: $REPORT"; exit 1; }
echo "✔ Reporte: $REPORT"
osascript -e "display notification \"Reporte listo en http://localhost:${MAC_OPTIMIZER_PORT:-8765}\" with title \"mac-optimizer\"" 2>/dev/null || true
