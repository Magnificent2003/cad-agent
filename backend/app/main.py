import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import chat, drawings, internal
from .config import ROOT, Settings, get_settings
from .events import EventBus
from .jobs import JobConflict, JobManager
from .opencode_client import OpenCodeClient, OpenCodeError, event_session_id
from .store import Store

logger = logging.getLogger(__name__)


async def opencode_events(app):
    state = app.state
    visible_ids = set()
    while True:
        try:
            async for event in state.opencode.events(ready=state.opencode_ready):
                session_id = event_session_id(event)
                if not session_id or not state.store.one("SELECT id FROM sessions WHERE id=?", (session_id,)):
                    continue
                kind = event.get("type")
                if kind in {"session.status", "session.idle", "session.error"}:
                    status = (event.get("properties", {}).get("status") or {}).get("type")
                    if status == "idle" or kind in {"session.idle", "session.error"}:
                        active = state.store.one("SELECT COUNT(*) AS count FROM jobs WHERE session_id=? AND status IN ('queued','running','cancelling')", (session_id,))["count"]
                        state.store.execute("UPDATE sessions SET busy=? WHERE id=?", (2 if active else 0, session_id))
                        if active:
                            event = {"type": "session.status", "properties": {"sessionID": session_id, "status": {"type": "busy"}}}
                if kind == "session.error":
                    event = {"type": kind, "properties": {"sessionID": session_id, "error": {"message": "智能体任务失败，请检查模型服务后重试。"}}}
                elif kind == "message.part.updated":
                    part = chat.visible_part(event.get("properties", {}).get("part", {}))
                    if not part:
                        continue
                    visible_ids.add(part.get("id"))
                    event = {"type": kind, "properties": {"part": part}}
                elif kind == "message.part.delta":
                    properties = event.get("properties", {})
                    if properties.get("partID") not in visible_ids or properties.get("field") != "text":
                        continue
                elif kind == "message.updated":
                    info = event.get("properties", {}).get("info", {})
                    event = {"type": kind, "properties": {"info": {key: info[key] for key in ("id", "sessionID", "role", "time", "finish") if key in info}}}
                elif kind not in {"session.status", "session.idle", "session.error", "message.part.removed", "message.removed"}:
                    continue
                state.bus.publish(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("OpenCode event connection unavailable: %s", type(exc).__name__)
        finally:
            state.opencode_ready.clear()
        await asyncio.sleep(2)


def create_app(settings: Settings | None = None, opencode_client=None):
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        data = settings.data_dir
        data.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("MPLCONFIGDIR", str(data / "cache" / "matplotlib"))
        os.environ.setdefault("XDG_CACHE_HOME", str(data / "cache"))
        app.state.settings = settings
        app.state.store = Store(data / "app.sqlite")
        app.state.store.initialize()
        app.state.bus = EventBus()
        app.state.jobs = JobManager(settings, app.state.store, app.state.bus)
        app.state.jobs.start()
        app.state.opencode = opencode_client or OpenCodeClient(settings, directory=str(ROOT / ".runtime/opencode/workspace"))
        app.state.opencode_ready = asyncio.Event()
        events_task = asyncio.create_task(opencode_events(app))
        try:
            yield
        finally:
            events_task.cancel()
            await asyncio.gather(events_task, return_exceptions=True)
            await app.state.jobs.close()
            await app.state.opencode.close()

    app = FastAPI(title="CAD Agent", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).hostname not in {"localhost", "127.0.0.1", "::1"}:
            return JSONResponse({"detail": "仅允许本机工作台访问。"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(JobConflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(OpenCodeError)
    async def opencode_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.get("/api/health")
    async def health(request: Request):
        try:
            upstream = await asyncio.wait_for(app.state.opencode.health(), 2)
            connected = bool(upstream.get("healthy", True))
        except Exception:
            connected = False
        return {"status": "ok", "opencode": "connected" if connected else "disconnected", "dwg_available": Path(settings.cad_settings()["oda_executable"]).is_file() if settings.oda_executable else False, "rag": "not_configured", "max_upload_mb": settings.cad_max_upload_mb}

    app.include_router(drawings.router)
    app.include_router(chat.router)
    app.include_router(internal.router)
    dist = ROOT / "frontend" / "dist"
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def index(path: str):
        if path.startswith(("api/", "internal/")):
            return JSONResponse({"detail": "接口不存在。"}, status_code=404)
        if path == "favicon.svg" and (dist / path).is_file():
            return FileResponse(dist / path)
        if (dist / "index.html").exists():
            return FileResponse(dist / "index.html")
        return JSONResponse({"message": "前端尚未构建，请先执行 npm run build。"}, status_code=503)

    return app


app = create_app()
