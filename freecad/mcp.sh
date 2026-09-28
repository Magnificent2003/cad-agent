#!/usr/bin/env bash
# Run the project-maintained MCP checkout; stdout belongs to MCP JSON-RPC.
set -euo pipefail
component_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export CAD_PROJECT_ROOT="$(cd -- "$component_root/.." && pwd)"
if ! test -x "$component_root/mcp/.venv/bin/freecad-mcp"; then
  echo '请先运行 bash freecad/install.sh 安装 FreeCAD MCP。' >&2
  exit 1
fi
exec "$component_root/mcp/.venv/bin/freecad-mcp" \
  --port "${FREECAD_RPC_PORT:-9875}" \
  --freecadcmd "bash \"$component_root/run_headless.sh\"" "$@"
