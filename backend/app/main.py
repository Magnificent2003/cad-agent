import asyncio
import logging
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import chat, drawings, artifacts
from .config import ROOT, Settings, get_settings
from .events import EventBus
from .jobs import JobConflict, JobManager
from .agent_tasks import AgentTasks
from agent.client import AgentClient, AgentError, event_session_id
from agent.runtime import WORKSPACE
from .store import Store

logger = logging.getLogger(__name__)


async def agent_events(app):
    state = app.state
    message_filter = chat.MessageEventFilter(state.agent)
    while True:
        try:
            async for event in state.agent.events(ready=state.agent_ready):
                session_id = event_session_id(event)
                if not session_id or not state.store.one("SELECT id FROM sessions WHERE id=?", (session_id,)):
                    continue
                kind = event.get("type")
                if kind in {"session.status", "session.idle", "session.error"}:
                    status = (event.get("properties", {}).get("status") or {}).get("type")
                    if status == "idle" or kind in {"session.idle", "session.error"}:
                        active = state.store.one("SELECT COUNT(*) AS count FROM jobs WHERE session_id=? AND status IN ('queued','running','cancelling')", (session_id,))["count"] or bool(state.file_tasks.active(session_id))
                        state.store.execute("UPDATE sessions SET busy=? WHERE id=?", (2 if active else 0, session_id))
                        if active:
                            event = {"type": "session.status", "properties": {"sessionID": session_id, "status": {"type": "busy"}}}
                if kind == "session.error":
                    event = {"type": kind, "properties": {"sessionID": session_id, "error": {"message": "智能体任务失败，请检查模型服务后重试。"}}}
                elif kind.startswith("message."):
                    event = await message_filter.filter(event, session_id)
                    if event is None:
                        continue
                elif kind not in {"session.status", "session.idle", "session.error"}:
                    continue
                state.bus.publish(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Agent event connection unavailable: %s", type(exc).__name__)
        finally:
            state.agent_ready.clear()
        await asyncio.sleep(2)


def create_app(settings: Settings | None = None, agent_client=None):
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
        app.state.agent = agent_client or AgentClient(settings, directory=str(WORKSPACE))
        app.state.file_tasks = AgentTasks(settings, app.state.store, app.state.bus, app.state.agent, app.state.jobs)
        app.state.agent_ready = asyncio.Event()
        events_task = asyncio.create_task(agent_events(app))
        try:
            yield
        finally:
            events_task.cancel()
            await asyncio.gather(events_task, return_exceptions=True)
            await app.state.file_tasks.close()
            await app.state.jobs.close()
            await app.state.agent.close()

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

    @app.exception_handler(AgentError)
    async def agent_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.get("/api/health")
    async def health(request: Request):
        try:
            upstream = await asyncio.wait_for(app.state.agent.health(), 2)
            tools = await asyncio.wait_for(app.state.agent.mcp_status(), 10)
            connected = bool(upstream.get("healthy", True)) and set(tools) == {"freecad"} and tools["freecad"].get("status") == "connected"
        except Exception:
            connected = False
        return {"status": "ok", "agent": "connected" if connected else "disconnected", "dwg_available": settings.converter_available, "engine": "freecad", "rag": "not_configured", "max_upload_mb": settings.cad_max_upload_mb}

    app.include_router(drawings.router)
    app.include_router(chat.router)
    app.include_router(artifacts.router)
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
