#!/usr/bin/env bash
# Install the unmodified optional Python DXF library used by FreeCAD Draft.
set -euo pipefail

component_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
python3 - "$component_root" <<'PY'
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

component = Path(sys.argv[1])
lock_path = component / "dxf-library.lock.json"
lock = json.loads(lock_path.read_text(encoding="utf-8"))
cache = component.parent / ".cache" / "downloads" / f"freecad-dxf-{lock['commit']}"
destination = component / "runtime" / "dxf-library"


def verified(path, entry):
    return (
        path.is_file()
        and path.stat().st_size == entry["size_bytes"]
        and hashlib.sha256(path.read_bytes()).hexdigest() == entry["sha256"]
    )


if all(verified(destination / item["name"], item) for item in lock["files"]):
    print(f"FreeCAD DXF Python 库 {lock['library_version']} 已安装：{destination}")
    raise SystemExit(0)

cache.mkdir(parents=True, exist_ok=True)
destination.mkdir(parents=True, exist_ok=True)
for entry in lock["files"]:
    cached = cache / entry["name"]
    if cached.exists() and not verified(cached, entry):
        raise RuntimeError(f"缓存文件校验失败，请移走该文件后重试：{cached}")
    if not cached.exists():
        with tempfile.TemporaryDirectory(prefix="dxf-", dir=cache) as staging:
            download = Path(staging) / entry["name"]
            subprocess.run(
                ["curl", "--fail", "--location", "--silent", "--show-error",
                 "--retry", "3", "--output", str(download), entry["url"]],
                check=True,
            )
            if not verified(download, entry):
                raise RuntimeError(f"下载文件 SHA-256 或长度校验失败：{entry['name']}")
            download.replace(cached)
    with tempfile.TemporaryDirectory(prefix=".install-", dir=destination) as staging:
        replacement = Path(staging) / entry["name"]
        shutil.copy2(cached, replacement)
        replacement.replace(destination / entry["name"])
shutil.copy2(lock_path, destination / "installed.lock.json")
print(f"FreeCAD DXF Python 库 {lock['library_version']} 已安装：{destination}")
print("4 个上游源码文件、原始版权声明和 GPL 许可证已保留；未开启自动下载。")
PY
