"""Check isolated dependency resolution, failed installs and both launch modes."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from packaging.requirements import Requirement

from freecad import python_deps as deps


def test_direct_requirements_are_pinned(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("# extra libraries\nezdxf==1.4.4 # DXF support\n")
    assert str(deps.read_requirements(requirements)[0]) == "ezdxf==1.4.4"
    requirements.write_text("numpy>=2\n")
    with pytest.raises(RuntimeError, match="固定版本"):
        deps.read_requirements(requirements)


def distribution(version, requires=(), imports=""):
    return SimpleNamespace(version=version, requires=list(requires), files=[], read_text=lambda _: imports)


def test_transitive_dependencies_and_extras_are_checked(monkeypatch):
    installed = {
        "cadlib": distribution("1.0", ["numpy<2", 'missing>=1; extra == "draw"']),
        "numpy": distribution("1.26.4"),
    }

    def find(name):
        if name not in installed:
            raise deps.metadata.PackageNotFoundError(name)
        return installed[name]

    monkeypatch.setattr(deps.metadata, "distribution", find)
    assert deps.verify_requirements([Requirement("cadlib==1.0")]) == {"cadlib": "1.0", "numpy": "1.26.4"}
    with pytest.raises(RuntimeError, match="缺少依赖：missing"):
        deps.verify_requirements([Requirement("cadlib[draw]==1.0")])
    with pytest.raises(RuntimeError, match="依赖版本不兼容"):
        deps.verify_requirements([Requirement("numpy<1"), Requirement("numpy>=1")])
    installed["numpy"] = distribution("2.0")
    with pytest.raises(RuntimeError, match="依赖版本不兼容"):
        deps.verify_requirements([Requirement("cadlib==1.0")])


def test_installed_metadata_does_not_hide_broken_native_import(monkeypatch):
    monkeypatch.setattr(deps.metadata, "distribution", lambda _: distribution("1.0", imports="cadlib"))

    def broken(_):
        raise ImportError("native ABI mismatch")

    monkeypatch.setattr(deps.importlib, "import_module", broken)
    with pytest.raises(RuntimeError, match="已安装但无法导入 cadlib.*ABI mismatch"):
        deps.verify_requirements([Requirement("cadlib==1.0")])


def test_resolution_preserves_runtime_packages_and_locks_only_missing_wheels(tmp_path, monkeypatch):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("ezdxf==1.4.4\n")
    monkeypatch.setattr(deps, "REQUIREMENTS", requirements)
    result = {"version": "1", "install": [{
        "metadata": {"name": "ezdxf", "version": "1.4.4"},
        "download_info": {"url": "https://example.invalid/ezdxf.whl", "archive_info": {"hashes": {"sha256": "a" * 64}}},
    }]}

    def resolve(*args):
        assert "--target" not in args
        constraints = Path(args[args.index("-c") + 1]).read_text()
        assert "numpy==1.26.4" in constraints
        Path(args[args.index("--report") + 1]).write_text(json.dumps(result))

    monkeypatch.setattr(deps, "pip", resolve)
    lock = deps.resolve_lock(tmp_path, {"numpy": "1.26.4"}, {"python": "3.11"})
    assert [p["name"] for p in lock["packages"]] == ["ezdxf"]
    assert lock["packages"][0]["sha256"] == "a" * 64
    result["install"][0]["metadata"]["name"] = "numpy"
    with pytest.raises(RuntimeError, match="不能覆盖 FreeCAD 自带的 numpy"):
        deps.resolve_lock(tmp_path, {"numpy": "1.26.4"}, {})


def test_publication_failure_restores_previous_directory_and_lock(tmp_path, monkeypatch):
    lock = tmp_path / "deps.lock.json"
    lock.write_text('{"old": true}\n')
    monkeypatch.setattr(deps, "LOCK", lock)
    target = tmp_path / "installed"
    target.mkdir()
    (target / "old.py").write_text("old")
    staging = tmp_path / "work/packages"
    staging.mkdir(parents=True)
    (staging / "new.py").write_text("new")
    replace = Path.replace

    def fail_lock_publish(path, destination):
        if destination == lock:
            raise OSError("cannot publish lock")
        return replace(path, destination)

    monkeypatch.setattr(Path, "replace", fail_lock_publish)
    with pytest.raises(OSError, match="cannot publish lock"):
        deps.publish(staging, target, {"new": True})
    assert (target / "old.py").read_text() == "old"
    assert not (target / "new.py").exists()
    assert json.loads(lock.read_text()) == {"old": True}


@pytest.mark.parametrize("headless", [False, True])
def test_gui_and_headless_share_dependency_paths_and_fail_before_launch(tmp_path, headless):
    component = tmp_path / "project with spaces/freecad"
    executable = component / "runtime/squashfs-root/AppRun"
    executable.parent.mkdir(parents=True)
    executable.write_text(f"#!{sys.executable}\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    executable.chmod(0o755)
    (component / "runtime/dxf-library").mkdir()
    checker = component / "install_python.sh"
    checker.write_text("exit 0\n")
    launcher = component / "run.sh"
    launcher.write_text((deps.COMPONENT / "run.sh").read_text())
    args = ["freecadcmd", "-c", "print('ok')"] if headless else ["some macro.FCMacro"]
    result = subprocess.run(["bash", str(launcher), *args], capture_output=True, text=True, check=True)
    paths = ["-P", str(component / "runtime/python-deps"), "-P", str(component / "runtime/dxf-library")]
    assert json.loads(result.stdout) == (["freecadcmd", *paths, *args[1:]] if headless else [*paths, *args])
    checker.write_text("echo 'missing dependency' >&2\nexit 1\n")
    result = subprocess.run(["bash", str(launcher), *args], capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stdout == ""
    assert "missing dependency" in result.stderr


def test_wheels_without_top_level_metadata_still_check_native_modules():
    package = SimpleNamespace(
        read_text=lambda _: None,
        files=[Path("native_cad.cpython-311-x86_64-linux-gnu.so"), Path("native_cad-1.0.dist-info/METADATA")],
    )
    assert deps.imports_for(package) == ["native_cad"]
