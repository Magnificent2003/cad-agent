"""Manage an isolated package directory using FreeCAD's own Python and pip.

The official runtime supplies the baseline packages. Pip resolves against that
baseline without installing anything, then installs only missing wheels into a
staging directory. The project lock records exact wheels and SHA-256 hashes.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib
import importlib.machinery
import importlib.metadata as metadata
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

COMPONENT = Path(__file__).resolve().parent
REQUIREMENTS = COMPONENT / "requirements-python.txt"
LOCK = COMPONENT / "python-deps.lock.json"
TARGET = COMPONENT / "runtime/python-deps"


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def read_requirements(path: Path) -> list[Requirement]:
    requirements = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        pins = list(requirement.specifier)
        if requirement.url or len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version:
            raise RuntimeError(f"请在 {path.name} 固定版本，例如 ezdxf==1.4.4：{line}")
        requirements.append(requirement)
    return requirements


def baseline_packages() -> dict[str, str]:
    return {canonicalize_name(d.metadata["Name"]): d.version for d in metadata.distributions()}


def identity(baseline: dict[str, str]) -> dict:
    return {
        "python": platform.python_version(),
        "abi": sys.implementation.cache_tag,
        "platform": f"{sys.platform}-{platform.machine()}",
        "runtime_sha256": digest((COMPONENT / "runtime.lock.json").read_bytes()),
        "baseline_sha256": digest(json.dumps(baseline, sort_keys=True).encode()),
    }


def imports_for(distribution) -> list[str]:
    names = (distribution.read_text("top_level.txt") or "").split()
    if not names:
        # Modern wheels may omit top_level.txt. Infer packages/modules from RECORD.
        for file in distribution.files or []:
            parts = file.parts
            if len(parts) > 1 and parts[0].isidentifier() and file.suffix == ".py":
                names.append(parts[0])
            elif len(parts) == 1 and file.suffix == ".py":
                names.append(file.stem)
            elif len(parts) == 1 and any(file.name.endswith(s) for s in importlib.machinery.EXTENSION_SUFFIXES):
                names.append(file.name.split(".", 1)[0])
    return sorted({n for n in names if n.isidentifier() and not n.startswith("_") and n != "tests"})


def verify_requirements(requirements: list[Requirement]) -> dict[str, str]:
    """Check the dependency closure, including markers/extras, and actual imports."""
    queue = [(r, {""}) for r in requirements]
    seen = set()
    versions = {}
    while queue:
        requirement, parent_extras = queue.pop()
        if requirement.marker and not any(requirement.marker.evaluate({"extra": e}) for e in parent_extras):
            continue
        key = (canonicalize_name(requirement.name), frozenset(requirement.extras))
        try:
            distribution = metadata.distribution(requirement.name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"缺少依赖：{requirement}") from exc
        if distribution.version not in requirement.specifier:
            raise RuntimeError(f"依赖版本不兼容：需要 {requirement}，实际 {distribution.version}")
        if key in seen:
            continue
        seen.add(key)
        versions[key[0]] = distribution.version
        for module in imports_for(distribution):
            try:
                importlib.import_module(module)
            except Exception as exc:
                raise RuntimeError(f"{requirement.name} 已安装但无法导入 {module}：{exc}") from exc
        extras = {"", *requirement.extras}
        queue.extend((Requirement(r), extras) for r in distribution.requires or [])
    return versions


def check_target(target: Path, lock: dict, expected_identity: dict) -> dict[str, str]:
    if lock.get("schema") != 1 or lock.get("environment") != expected_identity:
        raise RuntimeError("FreeCAD/Python 环境已变化，需要重新安装专用依赖")
    if lock.get("requirements_sha256") != digest(REQUIREMENTS.read_bytes()):
        raise RuntimeError("FreeCAD 依赖清单已变化，需要重新安装")
    if json.loads((target / "installed.lock.json").read_text()) != lock:
        raise RuntimeError("FreeCAD 专用依赖尚未按当前锁文件安装")
    installed = {canonicalize_name(d.metadata["Name"]): d.version for d in metadata.distributions(path=[str(target)])}
    expected = {p["name"]: p["version"] for p in lock["packages"]}
    if installed != expected:
        raise RuntimeError("FreeCAD 专用依赖目录不完整或含有未登记的包")
    # This process exits after checking; no GUI or user document is involved.
    sys.path.insert(0, str(target))
    importlib.invalidate_caches()
    return verify_requirements(read_requirements(REQUIREMENTS))


def pip(*args: str) -> None:
    subprocess.run(
        [sys.executable, "-I", "-m", "pip", "--isolated", "--disable-pip-version-check",
         "--cache-dir", str(COMPONENT.parent / ".cache/freecad-python"),
         "install", "--only-binary=:all:", "--no-input", *args],
        check=True,
    )


def resolve_lock(work: Path, baseline: dict[str, str], environment: dict) -> dict:
    constraints = work / "baseline.txt"
    constraints.write_text("".join(f"{name}=={version}\n" for name, version in sorted(baseline.items())))
    report = work / "resolution.json"
    # No --target here: the resolver must see and reuse FreeCAD's installed libs.
    pip("--dry-run", "--quiet", "--report", str(report), "--no-user",
        "-r", str(REQUIREMENTS), "-c", str(constraints))
    resolution = json.loads(report.read_text())
    if resolution.get("version") != "1":
        raise RuntimeError("不支持的 pip 安装报告格式")
    packages = []
    for item in resolution["install"]:
        name = canonicalize_name(item["metadata"]["name"])
        if name in baseline:
            raise RuntimeError(f"专用依赖不能覆盖 FreeCAD 自带的 {name}")
        download = item["download_info"]
        sha256 = download.get("archive_info", {}).get("hashes", {}).get("sha256")
        if not sha256 or not urlsplit(download["url"]).path.endswith(".whl"):
            raise RuntimeError(f"{name} 没有可锁定 SHA-256 的兼容 wheel")
        packages.append({"name": name, "version": item["metadata"]["version"],
                         "url": download["url"], "sha256": sha256})
    return {"schema": 1, "environment": environment,
            "requirements_sha256": digest(REQUIREMENTS.read_bytes()),
            "packages": sorted(packages, key=lambda p: p["name"])}


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def publish(staging: Path, target: Path, lock: dict) -> None:
    """Roll back the existing directory if publication fails."""
    backup = staging.parent / "previous"
    lock_temp = LOCK.with_suffix(".json.tmp")
    write_json(lock_temp, lock)
    had_previous = target.exists()
    if had_previous:
        target.rename(backup)
    try:
        staging.rename(target)
        lock_temp.replace(LOCK)
    except BaseException:
        if target.exists():
            shutil.rmtree(target)
        if had_previous:
            backup.rename(target)
        raise
    finally:
        lock_temp.unlink(missing_ok=True)


def install(baseline: dict[str, str], environment: dict) -> None:
    read_requirements(REQUIREMENTS)
    previous = json.loads(LOCK.read_text()) if LOCK.exists() else {}
    same = (previous.get("schema") == 1 and previous.get("environment") == environment
            and previous.get("requirements_sha256") == digest(REQUIREMENTS.read_bytes()))
    if same and TARGET.is_dir():
        # Run in a fresh interpreter so imports cannot leak into resolution or staging checks.
        checked = subprocess.run([sys.executable, "-B", "-I", __file__, "--check", "--quiet"])
        if checked.returncode == 0:
            print("FreeCAD 专用 Python 依赖已安装并通过检查。")
            return
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".python-deps-", dir=TARGET.parent) as temporary:
        work = Path(temporary)
        lock = previous if same else resolve_lock(work, baseline, environment)
        staging = work / "packages"
        staging.mkdir()
        wheels = work / "wheels.txt"
        wheels.write_text("".join(f"{p['name']} @ {p['url']} --hash=sha256:{p['sha256']}\n" for p in lock["packages"]))
        if lock["packages"]:
            pip("--no-deps", "--no-index", "--require-hashes", "-r", str(wheels), "--target", str(staging))
        write_json(staging / "installed.lock.json", lock)
        pending_lock = work / "pending.lock.json"
        write_json(pending_lock, lock)
        subprocess.run([sys.executable, "-B", "-I", __file__, "--check", "--target", str(staging),
                        "--lock", str(pending_lock)], check=True)
        publish(staging, TARGET, lock)
    print(f"FreeCAD 专用依赖已安装：{TARGET}\n版本锁定：{LOCK.name}；重启 FreeCAD 后生效。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只验证依赖，不下载、不安装")
    parser.add_argument("--quiet", action="store_true", help="检查成功时不输出")
    parser.add_argument("--target", type=Path, default=TARGET, help=argparse.SUPPRESS)
    parser.add_argument("--lock", type=Path, default=LOCK, help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if not Path(sys.executable).resolve().is_relative_to(COMPONENT / "runtime/squashfs-root/usr"):
            raise RuntimeError("必须通过 bash freecad/install_python.sh 使用 FreeCAD 自带 Python")
        baseline = baseline_packages()
        environment = identity(baseline)
        if args.check:
            versions = check_target(args.target, json.loads(args.lock.read_text()), environment)
            if not args.quiet:
                print("FreeCAD Python 依赖检查通过：" + ", ".join(f"{k}={v}" for k, v in sorted(versions.items())))
        else:
            state = COMPONENT.parent / ".runtime/freecad"
            state.mkdir(parents=True, exist_ok=True)
            with (state / "python-deps.install.lock").open("a") as guard:
                try:
                    fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError("另一个 FreeCAD 依赖安装正在运行") from exc
                install(baseline, environment)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"FreeCAD Python 依赖检查/安装失败：{exc}\n请运行 bash freecad/install_python.sh。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
