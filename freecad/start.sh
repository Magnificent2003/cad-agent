#!/usr/bin/env bash
# Launch the installed FreeCAD addon and its original local RPC server.
set -euo pipefail
component_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
"$component_root/../.venv/bin/python" - <<'PY'
import socket
import sys

with socket.socket() as listener:
    try:
        listener.bind(("127.0.0.1", 9875))
    except OSError:
        sys.exit("FreeCAD RPC 端口 9875 已占用；请检查并关闭已有实验窗口后再启动，避免连接到另一实例。")
PY
export CAD_FREECAD_ROOT="$component_root"
exec bash "$component_root/run.sh" "$component_root/start_mcp.FCMacro" "$@"
