#!/usr/bin/env bash
# Install the pinned third-party converter without changing the tracked wrapper.
set -euo pipefail

component_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$component_root/.." && pwd)"
runtime_root="$component_root/runtime"
converter_binary="$runtime_root/squashfs-root/usr/bin/ODAFileConverter"

if test -x "$converter_binary"; then
  echo "CAD 转换器已安装：$component_root/run.sh"
  exit 0
fi

if test "$(uname -s)" != Linux || test "$(uname -m)" != x86_64; then
  echo '此安装脚本只支持 Linux x86_64（包括 WSL2）。' >&2
  exit 1
fi
if test -e "$runtime_root/squashfs-root"; then
  echo '检测到不完整的 cad_converter/runtime/squashfs-root；请先移走该目录再安装。' >&2
  exit 1
fi

archive_url='https://www.opendesign.com/guestfiles/get?filename=ODAFileConverter_QT6_lnxX64_8.3dll_27.1.AppImage'
# Hash pinned from the verified project baseline, not an ODA digital signature.
archive_sha256='cb77a106354af98a1d355ba0191160aca3f90cb020946803e7c8132cba434dfc'
archive_path="$project_root/.cache/downloads/ODAFileConverter.AppImage"
download_temp=''
extract_temp=''
cleanup() {
  if test -n "$download_temp"; then rm -f -- "$download_temp"; fi
  if test -n "$extract_temp"; then rm -rf -- "$extract_temp"; fi
}
trap cleanup EXIT

mkdir -p -- "$(dirname -- "$archive_path")" "$runtime_root"
if ! test -f "$archive_path"; then
  download_temp="$(mktemp "$project_root/.cache/downloads/oda.XXXXXX")"
  curl -fL --retry 3 "$archive_url" -o "$download_temp"
  printf '%s  %s\n' "$archive_sha256" "$download_temp" | sha256sum --check --status
  mv -- "$download_temp" "$archive_path"
  download_temp=''
fi
if ! printf '%s  %s\n' "$archive_sha256" "$archive_path" | sha256sum --check --status; then
  echo 'ODA 安装包与项目固定的 SHA-256 不一致，已停止安装。请移走缓存包后重试。' >&2
  exit 1
fi

chmod +x -- "$archive_path"
extract_temp="$(mktemp -d "$runtime_root/.install.XXXXXX")"
(cd -- "$extract_temp" && "$archive_path" --appimage-extract >/dev/null)
if ! test -x "$extract_temp/squashfs-root/usr/bin/ODAFileConverter"; then
  echo 'ODA 安装包解包后未找到可执行程序。' >&2
  exit 1
fi
mv -- "$extract_temp/squashfs-root" "$runtime_root/squashfs-root"

echo "CAD 转换器已安装：$component_root/run.sh（ODA 27.1）"
if test -z "${DISPLAY:-}" && ! command -v xvfb-run >/dev/null; then
  echo '无显示服务：请安装 xvfb xauth，并在 .env 中设置 ODA_USE_XVFB=true。'
fi
echo '使用 scripts/verify_cad.py 验证真实 DWG 转换；安装包校验不能代替转换测试。'
