#!/usr/bin/env bash
# Launch the installed FreeCAD addon and its original local RPC server.
set -euo pipefail
component_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
"$component_root/../.venv/bin/python" "$component_root/../scripts/ports.py" --port 9875 --name "FreeCAD RPC"
export CAD_FREECAD_ROOT="$component_root"
exec bash "$component_root/run.sh" "$component_root/start_mcp.FCMacro" "$@"
