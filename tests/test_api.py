import asyncio
import io
import time

import ezdxf
import pytest
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import create_app
from agent.client import AgentError
from backend.app.store import Store, encode, now


class OfflineAgent:
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
        raise AgentError("Connection unavailable", 503)


@pytest.fixture
def client(tmp_path):
    settings = Settings(_env_file=None, cad_data_dir=tmp_path, cad_internal_token="test-token", oda_executable="", cad_preview_dpi=72)
    with TestClient(create_app(settings, OfflineAgent())) as client:
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
    # Upload/preview still works; the fixed-operation HTTP chain is gone.
    assert client.post(f"/internal/drawings/{drawing_id}/edit", json={}).status_code in {404, 405}
    assert client.get(base + "/revisions/r0001/download").content == source
    assert len(client.get(base + "/revisions").json()) == 1


def test_upload_rejects_unsafe_or_empty_input(client):
    assert client.post("/api/drawings", files={"file": ("evil.lsp", b"test")}).status_code == 415
    assert client.post("/api/drawings", files={"file": ("empty.dxf", b"")}).status_code == 400
    assert client.post("/api/drawings", files={"file": ("fake.dwg", b"not a drawing")}).status_code == 400
    assert client.post("/api/projects", json={"name": "test"}, headers={"Origin": "https://unrelated.example"}).status_code == 403
    assert client.get("/internal/rag/status").status_code == 404


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
