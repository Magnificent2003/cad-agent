import asyncio
import io
import time

import ezdxf
import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.opencode_client import OpenCodeError
from backend.app.store import Store, encode, now


class OfflineOpenCode:
    async def health(self):
        return {"healthy": False}

    async def events(self, ready=None):
        if ready:
            ready.set()
        while True:
            await asyncio.sleep(60)
            yield {}

    async def close(self):
        pass

    async def session_status(self):
        return {"stopping-session": {"type": "busy"}}

    async def abort(self, session_id):
        raise OpenCodeError("Connection unavailable", 503)


@pytest.fixture
def client(tmp_path):
    settings = Settings(_env_file=None, cad_data_dir=tmp_path, cad_internal_token="test-token", oda_executable="", cad_preview_dpi=72)
    with TestClient(create_app(settings, OfflineOpenCode())) as client:
        yield client


def sample_bytes():
    doc = ezdxf.new("R2018")
    doc.units = 4
    model = doc.modelspace()
    text = model.add_text("原始说明", dxfattribs={"insert": (10, 20), "height": 3})
    line = model.add_line((0, 0), (100, 0))
    doc.layouts.new("Sheet 1").add_circle((30, 30), 10)
    output = io.StringIO()
    doc.write(output)
    return output.getvalue().encode(), text.dxf.handle, line.dxf.handle


def wait_job(client, job_id):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return job
        time.sleep(0.05)
    pytest.fail("CAD job did not finish within 30 seconds")


def test_upload_versions_conflicts_preview_and_preservation(client):
    source, handle, untouched = sample_bytes()
    response = client.post("/api/drawings", files={"file": ("测试图.dxf", source)})
    assert response.status_code == 202
    drawing_id, job_id = response.json()["drawing_id"], response.json()["job_id"]
    imported = wait_job(client, job_id)
    assert imported["status"] == "succeeded", imported
    base = f"/api/drawings/{drawing_id}"
    drawing = client.get(base).json()
    assert drawing["current_revision"] == "r0001"
    assert drawing["metadata"]["units"]["name"] == "mm"
    assert client.get(base + "/revisions/r0001/preview").headers["content-type"].startswith("image/svg+xml")
    sheet = client.get(base + "/revisions/r0001/preview", params={"layout": "Sheet 1", "format": "png"})
    assert sheet.status_code == 200 and sheet.content.startswith(b"\x89PNG")
    headers = {"Authorization": "Bearer test-token"}
    internal = f"/internal/drawings/{drawing_id}"
    body = {"expected_revision": "r0001", "operation_id": "test-edit", "operation": {"operations": [{"type": "set_text", "handles": [handle], "text": "修改后的说明"}]}}
    assert client.post(internal + "/edit", json=body).status_code == 401
    edited = client.post(internal + "/edit", json=body, headers=headers)
    assert edited.status_code == 202, edited.text
    assert client.post(internal + "/edit", json=body, headers=headers).json()["id"] == edited.json()["id"]
    result = wait_job(client, edited.json()["id"])
    assert result["status"] == "succeeded", result
    assert result["result"]["revision_id"] == "r0002"
    assert result["result"]["changes"]["preservation_verified"]
    assert untouched not in result["result"]["changes"]["modified_handles"]
    assert client.post(internal + "/edit", json={**body, "operation_id": "stale"}, headers=headers).status_code == 409
    original_download = client.get(base + "/revisions/r0001/download").content
    modified_download = client.get(base + "/revisions/r0002/download").content
    old = ezdxf.read(io.StringIO(original_download.decode()))
    new = ezdxf.read(io.StringIO(modified_download.decode()))
    assert old.entitydb[handle].dxf.text == "原始说明"
    assert new.entitydb[handle].dxf.text == "修改后的说明"
    assert tuple(old.entitydb[untouched].dxf.end) == tuple(new.entitydb[untouched].dxf.end)
    assert len(client.get(base + "/revisions").json()) == 2
    crop = client.post(internal + "/render", json={"revision_id": "r0002", "bbox": [0, 0, 30, 30]}, headers=headers)
    crop_job = wait_job(client, crop.json()["id"])
    assert crop_job["status"] == "succeeded", crop_job
    image = client.get(crop_job["result"]["preview_url"], headers=headers)
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    assert client.get("/internal/rag/status", headers=headers).json()["status"] == "not_configured"


def test_upload_rejects_unsafe_or_empty_input(client):
    assert client.post("/api/drawings", files={"file": ("evil.lsp", b"test")}).status_code == 415
    assert client.post("/api/drawings", files={"file": ("empty.dxf", b"")}).status_code == 400
    assert client.post("/api/drawings", files={"file": ("fake.dwg", b"not a drawing")}).status_code == 400
    assert client.post("/api/projects", json={"name": "test"}, headers={"Origin": "https://unrelated.example"}).status_code == 403
    assert client.get("/internal/rag/status").status_code == 401


def test_existing_preview_regenerates_without_new_drawing_revision(client):
    source, _, _ = sample_bytes()
    uploaded = client.post("/api/drawings", files={"file": ("legacy-preview.dxf", source)}).json()
    assert wait_job(client, uploaded["job_id"])["status"] == "succeeded"
    drawing_id = uploaded["drawing_id"]
    base = f"/api/drawings/{drawing_id}/revisions/r0001"
    original = client.get(base + "/download").content
    store = client.app.state.store
    row = store.one("SELECT * FROM revisions WHERE drawing_id=?", (drawing_id,))
    row["metadata"]["preview"].pop("cache_signature")
    store.execute("UPDATE revisions SET metadata=? WHERE drawing_id=?", (encode(row["metadata"]), drawing_id))
    response = client.get(base + "/preview")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-cache"
    jobs = store.all("SELECT * FROM jobs WHERE kind='render' AND drawing_id=?", (drawing_id,))
    assert len(jobs) == 1 and jobs[0]["status"] == "succeeded"
    assert client.get(base + "/preview", params={"format": "png"}).status_code == 200
    assert len(store.all("SELECT * FROM jobs WHERE kind='render' AND drawing_id=?", (drawing_id,))) == 1
    assert client.get(base + "/download").content == original
    assert len(client.get(f"/api/drawings/{drawing_id}/revisions").json()) == 1
    client.app.state.settings.cad_fallback_font = "NotoSerifCJKsc-Regular.otf"
    assert client.get(base + "/preview").status_code == 200
    assert len(store.all("SELECT * FROM jobs WHERE kind='render' AND drawing_id=?", (drawing_id,))) == 2


def test_restart_marks_unfinished_jobs_interrupted(tmp_path):
    store = Store(tmp_path / "app.sqlite")
    store.initialize()
    store.execute("INSERT INTO drawings VALUES (?,?,?,?,?,?,?)", ("test", "default", "test.dxf", ".dxf", None, "importing", now()))
    store.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)", ("job", "test", None, "import", "running", encode({}), None, None, now(), now(), None))
    store.initialize()
    assert store.one("SELECT status FROM jobs WHERE id='job'")["status"] == "interrupted"
    assert store.one("SELECT status FROM drawings WHERE id='test'")["status"] == "failed"


def test_failed_abort_blocks_new_prompts_and_late_tool_writes(client):
    store = client.app.state.store
    store.execute("INSERT INTO drawings VALUES (?,?,?,?,?,?,?)", ("stop-test", "default", "test.dxf", ".dxf", "r0001", "ready", now()))
    store.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?)", ("r0001", "stop-test", 1, now(), "test", encode({"units": {"name": "mm"}})))
    store.execute("INSERT INTO sessions (id,drawing_id,title,busy,created_at) VALUES (?,?,?,?,?)", ("stopping-session", "stop-test", "test", 1, now()))
    response = client.post("/api/sessions/stopping-session/abort")
    assert response.json()["model_stopped"] is False
    assert response.json()["status"] == "stopping"
    assert client.get("/api/sessions/stopping-session/status").json()["status"] == "busy"
    new_prompt = client.post("/api/sessions/stopping-session/messages", json={"text": "new task", "expected_revision": "r0001"})
    assert new_prompt.status_code == 409
    late_edit = client.post("/internal/drawings/stop-test/edit", headers={"Authorization": "Bearer test-token"}, json={"session_id": "stopping-session", "expected_revision": "r0001", "operation": {"operations": []}})
    assert late_edit.status_code == 409
