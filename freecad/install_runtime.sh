#!/usr/bin/env bash
# Install the official pinned FreeCAD runtime without FUSE or system packages.
set -euo pipefail

component_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$component_root/.." && pwd)"
runtime_root="$component_root/runtime"
lock_file="$component_root/runtime.lock.json"

if test "$(uname -s)" != Linux || test "$(uname -m)" != x86_64; then
  echo '此安装脚本支持 Linux x86_64（包括 WSL2）。其他平台请安装 FreeCAD 官方发行版。' >&2
  exit 1
fi

metadata="$(python3 - "$lock_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    metadata = json.load(stream)
for key in ("version", "asset_name", "download_url", "sha256", "size_bytes"):
    print(metadata[key])
PY
)"
mapfile -t fields <<< "$metadata"
runtime_version="${fields[0]}"
archive_path="$project_root/.cache/downloads/${fields[1]}"
archive_url="${fields[2]}"
archive_sha256="${fields[3]}"
archive_size="${fields[4]}"

if test -x "$runtime_root/squashfs-root/AppRun"; then
  if test -f "$runtime_root/installed.lock.json" && cmp -s -- "$lock_file" "$runtime_root/installed.lock.json"; then
    echo "FreeCAD $runtime_version 已安装：$component_root/run.sh"
    exit 0
  fi
  echo '现有 FreeCAD runtime 与锁定版本不同；请先移走 freecad/runtime 后重新安装。' >&2
  exit 1
fi
if test -e "$runtime_root/squashfs-root"; then
  echo '检测到不完整的 FreeCAD 安装，请先移走 freecad/runtime/squashfs-root 再重试。' >&2
  exit 1
fi

mkdir -p -- "$(dirname -- "$archive_path")" "$runtime_root"
if ! test -f "$archive_path"; then
  echo "正在下载 FreeCAD $runtime_version（约 783 MiB），支持断点续传。"
  curl --fail --location --retry 3 --continue-at - --output "$archive_path.part" "$archive_url"
  if test "$(stat -c '%s' "$archive_path.part")" != "$archive_size" || ! printf '%s  %s\n' "$archive_sha256" "$archive_path.part" | sha256sum --check --status; then
    echo '下载包未通过官方 SHA-256/大小校验；保留 .part 文件供检查，未执行安装。' >&2
    exit 1
  fi
  mv -- "$archive_path.part" "$archive_path"
fi
if test "$(stat -c '%s' "$archive_path")" != "$archive_size" || ! printf '%s  %s\n' "$archive_sha256" "$archive_path" | sha256sum --check --status; then
  echo '缓存的 FreeCAD 安装包未通过官方 SHA-256/大小校验，请移走该文件后重试。' >&2
  exit 1
fi

chmod +x -- "$archive_path"
extract_temp="$(mktemp -d "$runtime_root/.install.XXXXXX")"
cleanup() { rm -rf -- "$extract_temp"; }
trap cleanup EXIT
(cd -- "$extract_temp" && "$archive_path" --appimage-extract > /dev/null)
if ! test -x "$extract_temp/squashfs-root/AppRun"; then
  echo 'FreeCAD 安装包解包后未找到 AppRun。' >&2
  exit 1
fi
mv -- "$extract_temp/squashfs-root" "$runtime_root/squashfs-root"
cp -- "$lock_file" "$runtime_root/installed.lock.json"
echo "FreeCAD $runtime_version 已安装：$component_root/run.sh"
