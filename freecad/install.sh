#!/usr/bin/env bash
# Install upstream FreeCAD and its editable MCP checkout without changing CAD business code.
set -euo pipefail
component_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$component_root/.." && pwd)"
if ! test -f "$component_root/mcp/pyproject.toml"; then
  git -C "$project_root" submodule update --init -- freecad/mcp
fi
bash "$component_root/apply_patches.sh"
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
