#!/usr/bin/env bash
# Install official FreeCAD and this repository's editable MCP source.
set -euo pipefail
component_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$component_root/.." && pwd)"
if ! test -f "$component_root/mcp/pyproject.toml"; then
  echo '项目 MCP 源码缺失，请恢复 freecad/mcp/；安装器不会从上游拉取或覆盖源码。' >&2
  exit 1
fi
bash "$component_root/install_runtime.sh"
bash "$component_root/install_python.sh"
bash "$component_root/install_dxf.sh"
if test -x "$project_root/.tools/uv/bin/uv"; then
  uv_binary="$project_root/.tools/uv/bin/uv"
else
  uv_binary="$(command -v uv)"
fi
UV_CACHE_DIR="$project_root/.cache/uv" UV_PROJECT_ENVIRONMENT="$component_root/mcp/.venv" "$uv_binary" sync \
  --project "$component_root/mcp" --frozen --python "$project_root/.venv/bin/python"
export CAD_FREECAD_ROOT="$component_root"
bash "$component_root/run_headless.sh" -c \
  'import os; exec(open(os.path.join(os.environ["CAD_FREECAD_ROOT"], "install_addon.py"), encoding="utf-8").read())'
echo 'FreeCAD 与项目 MCP 插件安装完成。运行 bash freecad/start.sh 启动。'
