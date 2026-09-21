import hmac
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from .drawings import completed_job, get_drawing, get_job, get_revision, preview_response


def require_internal(request: Request):
    token = request.headers.get("authorization", "").removeprefix("Bearer ") or request.headers.get("x-cad-token", "")
    expected = request.app.state.settings.cad_internal_token.get_secret_value()
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(401, "内部服务身份验证失败。")


router = APIRouter(prefix="/internal", dependencies=[Depends(require_internal)])


class QueryBody(BaseModel):
    revision_id: str | None = None
    query: dict = Field(default_factory=dict)
    session_id: str | None = None


class EditBody(BaseModel):
    expected_revision: str
    operation: dict
    session_id: str | None = None
    operation_id: str | None = Field(default=None, max_length=128)


class RenderBody(BaseModel):
    revision_id: str | None = None
    layout: str = Field(default="Model", max_length=255)
    bbox: list[float] | None = None
    session_id: str | None = None

    @model_validator(mode="after")
    def valid_bbox(self):
        import math
        if self.bbox is not None and (len(self.bbox) != 4 or not all(math.isfinite(x) for x in self.bbox) or self.bbox[0] >= self.bbox[2] or self.bbox[1] >= self.bbox[3]):
            raise ValueError("bbox 必须是递增且有限的 [xmin,ymin,xmax,ymax]。")
        return self


class ExportBody(BaseModel):
    revision_id: str
    format: Literal["dwg", "dxf"] = "dwg"
    session_id: str | None = None


class CheckBody(BaseModel):
    drawing_id: str
    revision_id: str | None = None
    constraints: list[dict] = Field(max_length=100)
    session_id: str | None = None


@router.get("/drawings/{drawing_id}")
def summary(drawing_id: str, request: Request, revision_id: str | None = None):
    drawing = get_drawing(request, drawing_id)
    revision = get_revision(request, drawing_id, revision_id)
    return {**drawing, "revision_id": revision["id"], "metadata": revision["metadata"]}


@router.post("/drawings/{drawing_id}/query")
async def query(drawing_id: str, body: QueryBody, request: Request):
    revision = get_revision(request, drawing_id, body.revision_id)
    job = request.app.state.jobs.submit(drawing_id, "query", {"revision_id": revision["id"], "query": body.query}, session_id=body.session_id)
    return await completed_job(request, job)


@router.post("/drawings/{drawing_id}/edit", status_code=202)
async def edit(drawing_id: str, body: EditBody, request: Request):
    get_revision(request, drawing_id, body.expected_revision)
    return request.app.state.jobs.submit(drawing_id, "edit", {"expected_revision": body.expected_revision, "operation": body.operation}, body.session_id, body.operation_id)


@router.post("/drawings/{drawing_id}/render", status_code=202)
async def render(drawing_id: str, body: RenderBody, request: Request):
    revision = get_revision(request, drawing_id, body.revision_id)
    return request.app.state.jobs.submit(drawing_id, "render", {"revision_id": revision["id"], "layout": body.layout, "bbox": body.bbox}, session_id=body.session_id)


@router.get("/drawings/{drawing_id}/revisions/{revision_id}/preview")
async def preview(drawing_id: str, revision_id: str, request: Request, layout: str = "Model", format: Literal["png", "svg"] = "png", render_id: str | None = None):
    return await preview_response(request, drawing_id, revision_id, layout, format, render_id)


@router.get("/jobs/{job_id}")
def job(job_id: str, request: Request):
    return get_job(job_id, request)


@router.post("/drawings/{drawing_id}/export", status_code=202)
async def export(drawing_id: str, body: ExportBody, request: Request):
    get_revision(request, drawing_id, body.revision_id)
    if body.format == "dxf":
        return {"status": "succeeded", "result": {"download_url": f"/api/drawings/{drawing_id}/revisions/{body.revision_id}/download?format=dxf"}}
    return request.app.state.jobs.submit(drawing_id, "export", body.model_dump(exclude={"session_id"}), session_id=body.session_id)


@router.get("/rag/status")
def rag():
    return {"status": "not_configured", "message": "规范审查未运行", "evidence": []}


@router.post("/checks")
async def check(body: CheckBody, request: Request):
    revision = get_revision(request, body.drawing_id, body.revision_id)
    job = request.app.state.jobs.submit(body.drawing_id, "check", {"revision_id": revision["id"], "constraints": body.constraints}, session_id=body.session_id)
    return await completed_job(request, job)
