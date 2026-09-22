#!/usr/bin/env bash
# Project-owned launcher; the converter itself is an ODA binary distribution.
set -euo pipefail

component_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
oda_root="$component_root/runtime/squashfs-root"
if ! test -x "$oda_root/usr/bin/ODAFileConverter"; then
  echo "CAD 转换器尚未安装，请运行：$component_root/install.sh" >&2
  exit 1
fi

export LD_LIBRARY_PATH="$oda_root/usr/lib:$oda_root/usr/bin${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export QT_PLUGIN_PATH="$oda_root/usr/plugins"
export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-xcb}"
exec "$oda_root/usr/bin/ODAFileConverter" "$@"
