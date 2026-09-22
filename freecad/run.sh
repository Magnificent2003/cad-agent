#!/usr/bin/env bash
# Launch the pinned application with project-local preferences and addons.
set -euo pipefail

component_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$component_root/.." && pwd)"
application="$component_root/runtime/squashfs-root/AppRun"
if ! test -x "$application"; then
  echo "FreeCAD 尚未安装，请运行：$component_root/install_runtime.sh" >&2
  exit 1
fi

state_root="$project_root/.runtime/freecad"
mkdir -p -- "$state_root"
state_root="$(cd -- "$state_root" && pwd)"
export FREECAD_STATE_DIR="$state_root"
export XDG_CONFIG_HOME="$state_root/config"
export XDG_DATA_HOME="$state_root/data"
export XDG_CACHE_HOME="$state_root/cache"
export XDG_STATE_HOME="$state_root/state"
mkdir -p -- "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME" "$XDG_STATE_HOME"

# XDG_RUNTIME_DIR and DISPLAY belong to the desktop session (including WSLg).
# Keep those values so Qt can connect to the existing display server.
if test -n "${DISPLAY:-}"; then
  export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
fi
export PYTHONNOUSERSITE=1
application_args=("$@")
if test -d "$component_root/runtime/dxf-library"; then
  # FreeCAD uses an isolated Python configuration and ignores PYTHONPATH.
  # Its documented -P option adds an external library without editing FreeCAD.
  if test "${1:-}" = freecadcmd; then
    application_args=(freecadcmd -P "$component_root/runtime/dxf-library" "${@:2}")
  else
    application_args=(-P "$component_root/runtime/dxf-library" "$@")
  fi
fi
exec "$application" "${application_args[@]}"
