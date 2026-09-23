#!/usr/bin/env bash
# Install/check dependencies with the exact Python shipped inside FreeCAD.
set -euo pipefail
component_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
application="$component_root/runtime/squashfs-root/AppRun"
if ! test -x "$application"; then
  echo 'FreeCAD 尚未安装，请先运行 bash freecad/install_runtime.sh。' >&2
  exit 1
fi
exec bash "$application" python -B -I "$component_root/python_deps.py" "$@"
