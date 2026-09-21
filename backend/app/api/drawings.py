import hashlib
import re
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..jobs import drawing_root, render_key
from ..preview_cache import preview_signature
from ..store import now

router = APIRouter(prefix="/api")


def get_drawing(request, drawing_id):
    drawing = request.app.state.store.one("SELECT * FROM drawings WHERE id=?", (drawing_id,))
    if not drawing:
        raise HTTPException(404, "图纸不存在。")
    revision = request.app.state.store.one("SELECT * FROM revisions WHERE drawing_id=? AND id=?", (drawing_id, drawing["current_revision"]))
    drawing["metadata"] = revision["metadata"] if revision else None
    return drawing


def get_revision(request, drawing_id, revision_id=None):
    drawing = get_drawing(request, drawing_id)
    revision_id = revision_id or drawing["current_revision"]
    row = request.app.state.store.one("SELECT * FROM revisions WHERE drawing_id=? AND id=?", (drawing_id, revision_id))
    if not row:
        raise HTTPException(404, "图纸版本尚未生成或不存在。")
    return row


async def completed_job(request, job):
    result = await request.app.state.jobs.wait(job["id"])
    if result["status"] != "succeeded":
        raise HTTPException(422, result.get("error") or "任务已取消。")
    return result["result"]


@router.get("/drawings")
def list_drawings(request: Request):
    return [get_drawing(request, r["id"]) for r in request.app.state.store.all("SELECT id FROM drawings ORDER BY created_at DESC")]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.get("/projects")
def projects(request: Request):
    return request.app.state.store.all("SELECT * FROM projects ORDER BY created_at")


@router.post("/projects", status_code=201)
def create_project(body: ProjectCreate, request: Request):
    project_id = uuid.uuid4().hex
    request.app.state.store.execute("INSERT INTO projects VALUES (?,?,?)", (project_id, body.name, now()))
    return {"id": project_id, "name": body.name}


@router.post("/drawings", status_code=202)
@router.post("/projects/{project_id}/drawings", status_code=202)
async def upload(request: Request, file: UploadFile = File(...), project_id: str = "default"):
    state = request.app.state
    if not state.store.one("SELECT id FROM projects WHERE id=?", (project_id,)):
        raise HTTPException(404, "项目不存在。")
    name = (file.filename or "drawing").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable())[:200]
    suffix = Path(name).suffix.lower()
    if suffix not in {".dwg", ".dxf"}:
        raise HTTPException(415, "仅支持 DWG 和 DXF 图纸。")
    drawing_id = uuid.uuid4().hex
    root = drawing_root(state.settings, drawing_id)
    source = root / "source" / ("original" + suffix)
    source.parent.mkdir(parents=True)
    size, digest, header = 0, hashlib.sha256(), b""
    try:
        with source.open("xb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > state.settings.cad_max_upload_mb * 1024 * 1024:
                    raise HTTPException(413, "图纸超过允许的上传大小。")
                if not header:
                    header = chunk[:64]
                digest.update(chunk)
                output.write(chunk)
        if not size:
            raise HTTPException(400, "上传文件为空。")
        if suffix == ".dwg" and not re.match(rb"AC10\d\d", header):
            raise HTTPException(400, "文件内容不是可识别的 DWG 图纸。")
        source.chmod(0o444)
        (source.parent / "sha256.txt").write_text(digest.hexdigest(), encoding="ascii")
        state.store.execute("INSERT INTO drawings VALUES (?,?,?,?,?,?,?)", (drawing_id, project_id, name, suffix, None, "importing", now()))
        job = state.jobs.submit(drawing_id, "import", {})
        return {"drawing_id": drawing_id, "job_id": job["id"]}
    except Exception:
        if not state.store.one("SELECT id FROM drawings WHERE id=?", (drawing_id,)):
            import shutil
            shutil.rmtree(root)
        raise
    finally:
        await file.close()


@router.get("/drawings/{drawing_id}")
def drawing(drawing_id: str, request: Request):
    return get_drawing(request, drawing_id)


@router.get("/drawings/{drawing_id}/revisions")
def revisions(drawing_id: str, request: Request):
    get_drawing(request, drawing_id)
    return request.app.state.store.all("SELECT * FROM revisions WHERE drawing_id=? ORDER BY number DESC", (drawing_id,))


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    job = request.app.state.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在。")
    return job


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    get_job(job_id, request)
    return request.app.state.jobs.cancel(job_id)


@router.post("/jobs/{job_id}/retry", status_code=202)
async def retry_job(job_id: str, request: Request):
    row = request.app.state.store.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not row:
        raise HTTPException(404, "任务不存在。")
    if row["status"] not in {"failed", "cancelled", "interrupted"}:
        raise HTTPException(409, "只有失败或中断的任务可以重试。")
    if row["kind"] == "import":
        request.app.state.store.execute("UPDATE drawings SET status='importing' WHERE id=?", (row["drawing_id"],))
    return request.app.state.jobs.submit(row["drawing_id"], row["kind"], row["payload"])


async def preview_response(request, drawing_id, revision_id, layout="Model", format="svg", render_id=None):
    revision = get_revision(request, drawing_id, revision_id)
    root = drawing_root(request.app.state.settings, drawing_id)
    settings = request.app.state.settings.cad_settings()
    if render_id:
        if not re.fullmatch(r"[0-9a-f]{24}", render_id):
            raise HTTPException(400, "无效预览编号。")
        directory = root / "previews" / revision_id / render_id
    elif layout == "Model" and revision["metadata"].get("preview", {}).get("cache_signature") == preview_signature(settings):
        directory = root / "revisions" / revision_id
    else:
        if layout not in [item["name"] for item in revision["metadata"]["layouts"]]:
            raise HTTPException(404, "布局不存在。")
        directory = root / "previews" / revision_id / render_key(layout, settings=settings)
        if not directory.exists():
            payload = {"revision_id": revision_id, "layout": layout}
            pending = request.app.state.store.all("SELECT * FROM jobs WHERE drawing_id=? AND kind='render' AND status IN ('queued','running')", (drawing_id,))
            job = next((job for job in pending if job["payload"] == payload), None)
            if job is None:
                job = request.app.state.jobs.submit(drawing_id, "render", payload)
            await completed_job(request, job)
    path = directory / ("preview." + format)
    if not path.is_file():
        raise HTTPException(404, "预览尚未生成。")
    return FileResponse(path, media_type="image/svg+xml" if format == "svg" else "image/png",
                        headers={"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})


@router.get("/drawings/{drawing_id}/revisions/{revision_id}/preview")
async def preview(drawing_id: str, revision_id: str, request: Request, layout: str = "Model", format: Literal["svg", "png"] = "svg", render_id: str | None = None):
    return await preview_response(request, drawing_id, revision_id, layout, format, render_id)


@router.get("/drawings/{drawing_id}/revisions/{revision_id}/download")
async def download(drawing_id: str, revision_id: str, request: Request, format: Literal["dxf", "dwg", "png"] = "dxf", layout: str = "Model"):
    get_revision(request, drawing_id, revision_id)
    drawing = get_drawing(request, drawing_id)
    root = drawing_root(request.app.state.settings, drawing_id)
    if format == "png":
        response = await preview_response(request, drawing_id, revision_id, layout, "png")
        path = response.path
    elif format == "dxf":
        path = root / "revisions" / revision_id / "drawing.dxf"
    else:
        path = root / "exports" / revision_id / "drawing.dwg"
        if not path.exists():
            job = request.app.state.jobs.submit(drawing_id, "export", {"revision_id": revision_id, "format": "dwg"})
            await completed_job(request, job)
    return FileResponse(path, filename=f"{Path(drawing['name']).stem}_{revision_id}.{format}")


class RestoreBody(BaseModel):
    expected_revision: str


@router.post("/drawings/{drawing_id}/revisions/{revision_id}/restore", status_code=202)
async def restore(drawing_id: str, revision_id: str, body: RestoreBody, request: Request):
    get_revision(request, drawing_id, revision_id)
    return request.app.state.jobs.submit(drawing_id, "restore", {"revision_id": revision_id, "expected_revision": body.expected_revision})
