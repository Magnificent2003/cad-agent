"""A fresh upstream checkout must reproduce project MCP fixes without a fork."""
import io
from pathlib import Path
import subprocess
import tarfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
MCP = ROOT / "freecad/mcp"
PATCH = ROOT / "freecad/patches/python-diagnostics.patch"
SCRIPT = ROOT / "freecad/apply_patches.sh"
MODIFIED = [
    "addon/FreeCADMCP/rpc_server/rpc_server.py",
    "src/freecad_mcp/operations/core.py",
]
ADDED = [
    "addon/FreeCADMCP/rpc_server/python_execution.py",
    "tests/test_rpc_python_diagnostics.py",
]


@pytest.fixture
def clean_mcp(tmp_path):
    source = tmp_path / "MCP clean checkout"
    source.mkdir()
    archive = subprocess.run(
        ["git", "-C", str(MCP), "archive", "HEAD", "pyproject.toml", *MODIFIED],
        capture_output=True, check=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        files.extractall(source, filter="data")
    return source


def apply(source):
    return subprocess.run(["bash", str(SCRIPT), str(source)], capture_output=True, text=True)


def snapshot(source):
    return {path.relative_to(source).as_posix(): path.read_bytes() for path in source.rglob("*") if path.is_file()}


def test_patch_reproduces_live_sources_is_idempotent_and_reversible(clean_mcp):
    baseline = snapshot(clean_mcp)
    first = apply(clean_mcp)
    assert first.returncode == 0, first.stderr
    assert "已应用 MCP 补丁" in first.stdout
    for relative in [*MODIFIED, *ADDED]:
        assert (clean_mcp / relative).read_bytes() == (MCP / relative).read_bytes(), relative
    patched = snapshot(clean_mcp)
    second = apply(clean_mcp)
    assert second.returncode == 0, second.stderr
    assert "MCP 补丁已应用" in second.stdout
    assert snapshot(clean_mcp) == patched
    subprocess.run(["git", "-C", str(clean_mcp), "apply", "--reverse", "--check", str(PATCH)], check=True)
    subprocess.run(["git", "-C", str(clean_mcp), "apply", "--reverse", str(PATCH)], check=True)
    assert snapshot(clean_mcp) == baseline


@pytest.mark.parametrize("conflict", ["modified-source", "existing-new-file", "edited-after-apply"])
def test_patch_conflicts_never_overwrite_local_changes(clean_mcp, conflict):
    if conflict == "modified-source":
        source = clean_mcp / MODIFIED[0]
        source.write_text(source.read_text().replace("import contextlib", "import contextlib as local_edit"))
    elif conflict == "existing-new-file":
        source = clean_mcp / ADDED[0]
        source.write_text("# Existing local implementation, preserve this file.\n")
    else:
        assert apply(clean_mcp).returncode == 0
        source = clean_mcp / ADDED[0]
        source.write_text(source.read_text().replace("OUTPUT_LIMIT = 32768", "OUTPUT_LIMIT = 16384"))
    before = snapshot(clean_mcp)
    result = apply(clean_mcp)
    assert result.returncode == 1
    assert "补丁冲突" in result.stderr
    assert "未覆盖源码" in result.stderr
    assert snapshot(clean_mcp) == before
