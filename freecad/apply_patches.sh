#!/usr/bin/env bash
# Apply project-owned changes to the editable upstream checkout without resets.
set -euo pipefail
component_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if (( $# > 1 )); then
  echo '用法：bash freecad/apply_patches.sh [MCP源码目录]' >&2
  exit 2
fi
mcp_root="${1:-$component_root/mcp}"
if ! test -f "$mcp_root/pyproject.toml"; then
  echo '缺少 MCP 源码，请先初始化 freecad/mcp 子模块。' >&2
  exit 1
fi
for patch in "$component_root"/patches/*.patch; do
  test -f "$patch" || continue
  if git -C "$mcp_root" apply --check "$patch" >/dev/null 2>&1; then
    git -C "$mcp_root" apply "$patch"
    echo "已应用 MCP 补丁：$(basename -- "$patch")"
  elif git -C "$mcp_root" apply --reverse --check "$patch" >/dev/null 2>&1; then
    echo "MCP 补丁已应用：$(basename -- "$patch")"
  else
    echo "MCP 补丁冲突：$(basename -- "$patch")；未覆盖源码。请保留本地修改，核对上游版本和补丁后再安装。" >&2
    git -C "$mcp_root" apply --check "$patch" >&2 || true
    exit 1
  fi
done
