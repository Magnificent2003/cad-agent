"""Exercise registered CAD tools and the isolated worker transport without FreeCAD."""

import asyncio
import base64
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT / "freecad/mcp/src"))
    module = importlib.import_module("freecad_mcp.site_tools")
    project = tmp_path / "project with spaces"
    python = project / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setenv("CAD_PROJECT_ROOT", str(project))
    return module, project


def report(contents):
    assert contents[0].type == "text"
    return json.loads(contents[0].text)


def worker(monkeypatch, bridge, response, *, returncode=0, stderr=b""):
    module, project = bridge
    calls = []

    def run(command, **kwargs):
        assert command == [str(project / ".venv/bin/python"), "-m", "cad_domain.worker"]
        assert kwargs["cwd"] == str(project)
        assert kwargs["timeout"] == module.WORKER_TIMEOUT_SECONDS
        assert "shell" not in kwargs
        calls.append(json.loads(kwargs["input"]))
        value = response(calls[-1]) if callable(response) else response
        kwargs["stdout"].write(value if isinstance(value, bytes) else json.dumps(value).encode())
        kwargs["stderr"].write(stderr)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(module.subprocess, "run", run)
    return calls


def test_registered_tools_have_compatible_schemas_and_route_data_only(bridge, monkeypatch):
    server = importlib.import_module("freecad_mcp.server")
    registered = {item.name: item.model_dump(by_alias=True) for item in asyncio.run(server.mcp.list_tools())}
    for name in ("cad_inspect", "cad_layout", "cad_review"):
        assert registered[name]["outputSchema"] is None
        assert registered[name]["inputSchema"]["properties"]["context_path"]["default"] == ""
    assert registered["cad_review"]["inputSchema"]["properties"]["profile"]["enum"] == ["general", "site_layout"]
    assert "execute_code" in registered
    calls = worker(monkeypatch, bridge, {"result": {"status": "passed", "file_sha256": "a" * 64}})
    # These parameters must remain data; scope checks belong to the real worker.
    path = "/output/$(never-execute);中文.dxf"
    facts = {"source_sha256": "b" * 64, "boundary": {"handles": ["AB"]}}
    components = [{"id": "office", "kind": "rectangle", "width_m": 6}]
    server.cad_inspect("/binding.json", path, ["AB"], 7)
    server.cad_layout("/binding.json", path, "/output/candidate.dxf", facts, components)
    result = server.cad_review("/binding.json", path, "site_layout", facts, components, ["AB"], False)
    assert [call["action"] for call in calls] == ["inspect", "layout", "review"]
    assert all(call["context_path"] == "/binding.json" for call in calls)
    assert calls[0]["params"] == {"path": path, "handles": ["AB"], "limit": 7}
    assert calls[1]["params"] == {"source": path, "target": "/output/candidate.dxf", "facts": facts, "components": components}
    assert calls[2]["params"] == {"path": path, "profile": "site_layout", "facts": facts, "components": components,
                                  "allowed_changes": ["AB"], "render": False}
    assert report(result)["file_sha256"] == "a" * 64


def test_worker_report_and_png_are_both_returned(bridge, monkeypatch, tmp_path):
    module, _ = bridge
    png = tmp_path / "actual.png"
    data = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQ0AAAAASUVORK5CYII=")
    png.write_bytes(data)
    expected = {"status": "passed", "path": "/output/final.dxf", "file_sha256": "c" * 64, "checks": [],
                "preview": {"path": str(png), "drawing_sha256": "c" * 64, "image_sha256": hashlib.sha256(data).hexdigest()}}
    calls = worker(monkeypatch, bridge, lambda request: (
        {"result": {"status": "passed"}} if request["action"] == "feedback"
        else {"result": expected, "preview": str(png)}
    ))
    contents = module.run_site_tool("review", "/binding.json", {})
    assert report(contents) == expected
    assert contents[1].type == "image"
    assert contents[1].model_dump(by_alias=True)["mimeType"] == "image/png"
    assert base64.b64decode(contents[1].data) == data
    assert calls[1] == {"action": "feedback", "context_path": "/binding.json", "params": {
        "path": expected["path"], "file_sha256": expected["file_sha256"], "preview": expected["preview"],
    }}
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["missing", "relative", "oversized", "wrong_bytes", "wrong_extension"])
def test_preview_failure_keeps_report_and_explains_missing_image(bridge, monkeypatch, tmp_path, kind):
    module, _ = bridge
    monkeypatch.setattr(module, "MAX_PREVIEW_BYTES", 32)
    path = tmp_path / ("preview.txt" if kind == "wrong_extension" else "preview.png")
    if kind != "missing":
        path.write_bytes(b"wrong" if kind == "wrong_bytes" else module.PNG_SIGNATURE + b"x" * (40 if kind == "oversized" else 1))
    calls = worker(monkeypatch, bridge, {"result": {"status": "unknown", "issues": ["evidence needed"]},
                                         "preview": "relative.png" if kind == "relative" else str(path)})
    result = module.run_site_tool("review", "/binding.json", {})
    assert len(result) == 1
    assert report(result)["status"] == "unknown"
    assert report(result)["issues"] == ["evidence needed"]
    assert report(result)["preview_delivery"]["status"] == "unavailable"
    assert len(calls) == 1  # Rejected images must never receive a feedback acknowledgment.


@pytest.mark.parametrize("mismatch", ["image", "drawing", "path", "missing"])
def test_preview_hash_mismatch_is_not_acknowledged(bridge, monkeypatch, tmp_path, mismatch):
    module, _ = bridge
    path = tmp_path / "preview.png"
    data = module.PNG_SIGNATURE + b"png-data"
    path.write_bytes(data)
    metadata = {"path": str(path), "drawing_sha256": "c" * 64, "image_sha256": hashlib.sha256(data).hexdigest()}
    if mismatch == "missing":
        metadata = None
    elif mismatch == "path":
        metadata["path"] = "/different/preview.png"
    else:
        metadata[f"{mismatch}_sha256"] = "d" * 64
    calls = worker(monkeypatch, bridge, {"result": {"status": "passed", "path": "/output/final.dxf",
                   "file_sha256": "c" * 64, "preview": metadata}, "preview": str(path)})
    contents = module.run_site_tool("review", "/binding.json", {})
    assert len(contents) == 1
    assert report(contents)["preview_delivery"]["status"] == "unavailable"
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [{"status": "unknown", "error": "drawing changed"},
                                    {"status": "passed", "success": False},
                                    b"worker output invalid"])
def test_failed_feedback_acknowledgment_never_returns_an_image(bridge, monkeypatch, tmp_path, failure):
    module, _ = bridge
    path = tmp_path / "preview.png"
    data = module.PNG_SIGNATURE + b"png-data"
    path.write_bytes(data)
    metadata = {"path": str(path), "drawing_sha256": "c" * 64, "image_sha256": hashlib.sha256(data).hexdigest()}
    calls = worker(monkeypatch, bridge, lambda request: (
        (failure if isinstance(failure, bytes) else {"result": failure}) if request["action"] == "feedback"
        else {"result": {"status": "passed", "path": "/output/final.dxf", "file_sha256": "c" * 64,
                          "preview": metadata}, "preview": str(path)}
    ))
    contents = module.run_site_tool("review", "/binding.json", {})
    assert len(contents) == 1
    delivery = report(contents)["preview_delivery"]
    assert delivery["status"] == "unavailable"
    assert "acknowledgment" in delivery
    assert [call["action"] for call in calls] == ["review", "feedback"]


def test_expected_worker_rejection_remains_actionable(bridge, monkeypatch):
    module, _ = bridge
    expected = {"status": "unknown", "success": False, "issues": ["Confirm boundary handle before layout."]}
    worker(monkeypatch, bridge, {"result": expected})
    assert report(module.run_site_tool("layout", "/binding.json", {})) == expected


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"result":null}', b'{"result":{"area":NaN}}'])
def test_invalid_worker_output_is_a_bounded_error(bridge, monkeypatch, raw):
    module, _ = bridge
    worker(monkeypatch, bridge, raw, stderr=b"x" * 9000)
    result = report(module.run_site_tool("inspect", "/binding.json", {}))
    assert result["success"] is False
    assert result["error"]["code"] == "invalid_worker_response"
    assert len(result["error"]["stderr"]) < 4200


def test_nonzero_worker_exit_cannot_be_mistaken_for_pass(bridge, monkeypatch):
    module, _ = bridge
    worker(monkeypatch, bridge, {"result": {"status": "passed"}}, returncode=1, stderr=b"worker crashed")
    result = report(module.run_site_tool("review", "/binding.json", {}))
    assert result["success"] is False
    assert result["error"]["code"] == "worker_failed"
    assert result["error"]["worker_exit_code"] == 1
    assert result["error"]["stderr"] == "worker crashed"


def test_timeout_returns_recovery_guidance_and_bounded_stderr(bridge, monkeypatch):
    module, _ = bridge

    def timeout(command, **kwargs):
        kwargs["stderr"].write(b"x" * 10000)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(module.subprocess, "run", timeout)
    result = report(module.run_site_tool("layout", "/binding.json", {}))
    assert result["error"]["code"] == "worker_timeout"
    assert "Check current outputs" in result["error"]["message"]
    assert len(result["error"]["stderr"]) < 4200


def test_missing_environment_and_oversized_output_fail_clearly(bridge, monkeypatch):
    module, _ = bridge
    worker(monkeypatch, bridge, b"x" * 100)
    monkeypatch.setattr(module, "MAX_REPORT_BYTES", 20)
    assert report(module.run_site_tool("inspect", "", {}))["error"]["code"] == "invalid_worker_response"
    monkeypatch.delenv("CAD_PROJECT_ROOT")
    assert report(module.run_site_tool("inspect", "", {}))["error"]["code"] == "worker_unavailable"


def test_launch_script_binds_its_own_project_root_even_with_spaces(tmp_path):
    component = tmp_path / "project with spaces/freecad"
    binary = component / "mcp/.venv/bin/freecad-mcp"
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\nprintf "%s" "$CAD_PROJECT_ROOT"\n')
    binary.chmod(0o755)
    script = component / "mcp.sh"
    script.write_text((ROOT / "freecad/mcp.sh").read_text())
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, check=True)
    assert result.stdout == str(component.parent)
