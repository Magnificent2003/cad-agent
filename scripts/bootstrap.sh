#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
source scripts/env.sh
mkdir -p .tools .cache/downloads
if ! test -x .tools/uv/bin/uv; then
  python3 -m pip install --target .tools/uv --cache-dir .cache/pip uv==0.12.17
fi
if ! test -x .tools/node/bin/node; then
  curl -fL --retry 3 https://nodejs.org/dist/v24.21.0/node-v24.21.0-linux-x64.tar.xz -o .cache/downloads/node-v24.21.0-linux-x64.tar.xz
  curl -fL --retry 3 https://nodejs.org/dist/v24.21.0/SHASUMS256.txt -o .cache/downloads/node-shasums.txt
  python3 - <<'PY'
import hashlib, pathlib, tarfile
p = pathlib.Path('.cache/downloads/node-v24.21.0-linux-x64.tar.xz')
checksum = next(line.split()[0] for line in p.with_name('node-shasums.txt').read_text().splitlines() if line.endswith(' ' + p.name))
if hashlib.sha256(p.read_bytes()).hexdigest() != checksum:
    raise SystemExit('Node archive checksum mismatch')
with tarfile.open(p) as archive:
    archive.extractall('.tools', filter='data')
pathlib.Path('.tools/node').symlink_to('node-v24.21.0-linux-x64', target_is_directory=True)
PY
fi
uv sync --frozen
mkdir -p .tools/opencode
cp tooling/opencode/package.json tooling/opencode/package-lock.json .tools/opencode/
npm ci --prefix .tools/opencode
npm ci --prefix frontend
npm run build --prefix frontend
.venv/bin/python scripts/configure_env.py
echo '依赖安装完成。DWG 转换器安装：bash scripts/install_oda.sh；启动：bash scripts/dev.sh'
