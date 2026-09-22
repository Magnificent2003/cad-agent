#!/usr/bin/env bash
# Source this file from any working directory; no secrets are exported here.
CAD_PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$CAD_PROJECT_ROOT/.tools/bun/bin:$CAD_PROJECT_ROOT/.tools/node/bin:$CAD_PROJECT_ROOT/.tools/uv/bin:$CAD_PROJECT_ROOT/.venv/bin:$PATH"
export UV_CACHE_DIR="$CAD_PROJECT_ROOT/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$CAD_PROJECT_ROOT/.tools/python"
export npm_config_cache="$CAD_PROJECT_ROOT/.cache/npm"
export BUN_INSTALL_CACHE_DIR="$CAD_PROJECT_ROOT/.cache/bun"
export MPLCONFIGDIR="$CAD_PROJECT_ROOT/.cache/matplotlib"
export PLAYWRIGHT_BROWSERS_PATH="$CAD_PROJECT_ROOT/.cache/playwright"
