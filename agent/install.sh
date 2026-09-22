#!/usr/bin/env bash
# Install the packaged engine, or prepare the checked-in engine source for development.
set -euo pipefail

CAD_AGENT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CAD_AGENT_ROOT="$(cd -- "$CAD_AGENT_DIR/.." && pwd)"

case "${1:-}" in
  ""|--source) ;;
  --help|-h)
    printf '%s\n' '用法：bash agent/install.sh [--source]' \
      '默认：按 package-lock.json 安装发行版。' \
      '--source：安装固定 Bun 及已纳入仓库的 agent/engine 源码依赖；不会覆盖源码。'
    exit 0
    ;;
  *) printf '未知参数：%s\n' "$1" >&2; exit 2 ;;
esac
if (( $# > 1 )); then
  printf '%s\n' '一次只支持一个安装模式参数。' >&2
  exit 2
fi

# The shared environment adds local Node/Python paths, without loading .env secrets.
source "$CAD_AGENT_ROOT/scripts/env.sh"
if [[ "${1:-}" != --source ]]; then
  command -v npm >/dev/null || { printf '%s\n' '找不到 npm，请先配置 Node.js。' >&2; exit 1; }
  exec npm ci --prefix "$CAD_AGENT_DIR"
fi

command -v python3 >/dev/null || { printf '%s\n' '源码安装需要 Python 3。' >&2; exit 1; }
python3 - "$CAD_AGENT_ROOT" <<'PY'
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

root = Path(sys.argv[1])
lock = json.loads((root / "agent/source.lock.json").read_text())
engine = root / lock["source_directory"]


def fail(message):
    raise SystemExit(message)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


if not engine.is_dir():
    fail("缺少 agent/engine 源码。请获取包含该目录的完整项目仓库；安装脚本不会下载或覆盖源码。")
for relative, expected in lock["manifest_sha256"].items():
    path = engine / relative
    if not path.is_file() or digest(path) != expected:
        fail(f"源码依赖清单与 source.lock.json 不符：{relative}。"
             "请检查是否获取完整版本；若有意修改依赖，请同步更新来源记录中的清单摘要。")
for relative in ["LICENSE", ".github/TEAM_MEMBERS", "packages/opencode/src/index.ts",
                 "packages/opencode/bunfig.toml", "packages/core/script/fix-node-pty.ts"]:
    if not (engine / relative).is_file():
        fail(f"源码不完整，缺少 {relative}；请获取完整项目仓库。")
for workspace in lock["install_workspaces"]:
    if not (engine / workspace / "package.json").is_file():
        fail(f"源码工作区不完整：{workspace}；请获取完整项目仓库。")

bun_version = lock["bun"]["version"]
manifest = json.loads((engine / "package.json").read_text())
package = json.loads((engine / "packages/opencode/package.json").read_text())
if manifest.get("packageManager") != f"bun@{bun_version}" or package.get("version") != lock["tag"].removeprefix("v"):
    fail("源码版本或 Bun 版本与 source.lock.json 不一致。")
if not shutil.which("node"):
    fail("源码依赖的安装脚本需要 Node.js，请先配置项目 Node.js 环境。")

bun_bin = root / ".tools/bun/bin/bun"
installed_version = None
if bun_bin.is_file():
    result = subprocess.run([str(bun_bin), "--version"], text=True, capture_output=True)
    if result.returncode == 0:
        installed_version = result.stdout.strip()

if installed_version != bun_version:
    system = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    machine = {"x86_64": "x64", "amd64": "x64", "aarch64": "aarch64", "arm64": "aarch64"}.get(platform.machine().lower())
    asset = f"bun-{system}-{machine}.zip"
    expected = lock["bun"]["archive_sha256"].get(asset)
    if not expected:
        fail("此安装脚本支持 Linux/macOS 的 x64 与 ARM64；当前平台没有已核实的 Bun 下载摘要。")
    if system == "linux" and list(Path("/lib").glob("ld-musl-*.so.1")):
        fail("当前为 musl 系统；此脚本固定的是 glibc Bun 包，请使用受支持的 glibc Linux 环境。")
    cache = root / ".cache/downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"bun-{bun_version}-{system}-{machine}.zip"
    if not archive.is_file():
        url = f"https://github.com/oven-sh/bun/releases/download/bun-v{bun_version}/{asset}"
        print(f"下载 Bun {bun_version}：{asset}", flush=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=cache, prefix="bun-download-", delete=False) as output:
                temporary = Path(output.name)
                with urllib.request.urlopen(url, timeout=60) as response:
                    shutil.copyfileobj(response, output)
            if digest(temporary) != expected:
                fail("Bun 下载的 SHA256 与官方发行清单不一致，未安装。")
            temporary.replace(archive)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    if digest(archive) != expected:
        fail(f"Bun 缓存包校验失败，请移除后重试：{archive}")
    # Digests are pinned from the official SHASUMS256.txt URL recorded in source.lock.json.
    print("Bun 下载包已通过官方 SHA256 摘要校验。", flush=True)
    bun_bin.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="bun-install-", dir=bun_bin.parent) as temporary:
        candidate = Path(temporary) / "bun"
        with zipfile.ZipFile(archive) as bundle:
            # Extract only the expected executable; no archive paths are accepted as output paths.
            with bundle.open(f"{asset.removesuffix('.zip')}/bun") as source, candidate.open("wb") as target:
                shutil.copyfileobj(source, target)
        candidate.chmod(0o755)
        version = subprocess.check_output([str(candidate), "--version"], text=True).strip()
        if version != bun_version:
            fail(f"Bun 可执行文件版本不符：预期 {bun_version}，实际 {version}。")
        candidate.replace(bun_bin)

environment = os.environ.copy()
environment["PATH"] = str(bun_bin.parent) + os.pathsep + environment.get("PATH", "")
environment["BUN_INSTALL_CACHE_DIR"] = str(root / ".cache/bun")
environment["HUSKY"] = "0"
# Bun 1.3.14 unions filter selections: '!./' would select every other
# workspace, not subtract the root. Use only this explicit positive list.
command = [str(bun_bin), "install", "--frozen-lockfile"]
for workspace in lock["install_workspaces"]:
    command.extend(["--filter", f"./{workspace}"])
print(f"安装智能体源码依赖（基于 {lock['tag']}，Bun {bun_version}）。", flush=True)
subprocess.run(command, cwd=engine, env=environment, check=True)
subprocess.run([str(bun_bin), "run", "--cwd", "packages/core", "fix-node-pty"],
               cwd=engine, env=environment, check=True)
print("源码依赖安装完成。源码启动与服务验证请按 agent/README.md 执行。")
PY
