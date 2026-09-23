import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent.client import AgentError, event_session_id
from agent.session import is_internal_message, is_internal_part
from ..store import now
from .drawings import get_drawing

router = APIRouter(prefix="/api")


def visible_part(part):
    if is_internal_part(part):
        return None
    common = {key: part[key] for key in ("id", "sessionID", "messageID", "type") if key in part}
    if part.get("type") == "text":
        return {**common, "text": part.get("text", "")}
    if part.get("type") == "tool":
        state = part.get("state", {})
        return {**common, "tool": part.get("tool"), "state": {"status": state.get("status"), "title": state.get("title"), "error": "工具执行未成功，请查看助手说明。" if state.get("error") else None}}
    return None


class MessageEventFilter:
    """Check a streamed part's parent before forwarding any text or tool data."""

    def __init__(self, agent):
        self.agent = agent
        self.parents = {}
        self.parts = {}

    async def parent_visible(self, session_id, message_id):
        if not message_id:
            return False
        key = (session_id, message_id)
        if key not in self.parents:
            # Reconnection can miss message.updated; never infer visibility from a part.
            try:
                history = await self.agent.messages(session_id)
            except AgentError:
                return False
            for message in history:
                info = message.get("info", message)
                if info.get("id"):
                    self.parents[(session_id, info["id"])] = not is_internal_message(info)
        return self.parents.get(key, False)

    async def filter(self, event, session_id):
        kind = event.get("type")
        properties = event.get("properties") or {}
        if kind == "message.updated":
            info = properties.get("info") or {}
            key = (session_id, info.get("id"))
            self.parents[key] = not is_internal_message(info)
            # User text arrives as parts; this avoids an empty bubble for synthetic users.
            if not self.parents[key] or info.get("role") != "assistant":
                return None
            safe = {key: info[key] for key in ("id", "sessionID", "role", "time", "finish") if key in info}
            return {"type": kind, "properties": {"info": safe}}
        if kind == "message.part.updated":
            raw = properties.get("part") or {}
            key = (session_id, raw.get("messageID"), raw.get("id"))
            part = visible_part(raw)
            if not part or not await self.parent_visible(session_id, raw.get("messageID")):
                self.parts.pop(key, None)
                return None
            self.parts[key] = part.get("type")
            return {"type": kind, "properties": {"part": part}}
        if kind in {"message.part.delta", "message.part.removed"}:
            key = (session_id, properties.get("messageID"), properties.get("partID"))
            part_type = self.parts.get(key)
            if not part_type or not await self.parent_visible(session_id, properties.get("messageID")):
                return None
            if kind == "message.part.delta":
                if part_type != "text" or properties.get("field") != "text":
                    return None
                fields = ("sessionID", "messageID", "partID", "field", "delta")
            else:
                self.parts.pop(key, None)
                fields = ("sessionID", "messageID", "partID")
            return {"type": kind, "properties": {key: properties[key] for key in fields if key in properties}}
        if kind == "message.removed":
            key = (session_id, properties.get("messageID"))
            if not self.parents.pop(key, False):
                return None
            self.parts = {part: value for part, value in self.parts.items() if part[:2] != key}
            return {"type": kind, "properties": {key: properties[key] for key in ("sessionID", "messageID") if key in properties}}
        return None


def get_session(request, session_id):
    session = request.app.state.store.one("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not session:
        raise HTTPException(404, "会话不存在。")
    return session


async def refresh_status(request, session_id):
    session = get_session(request, session_id)
    if session.get("engine") != "freecad":
        return {"status": "idle", "model_status": "archived"}
    active = request.app.state.file_tasks.active(session_id)
    return {"status": "busy" if active else "idle", "model_status": active["status"] if active else "idle"}


@router.get("/drawings/{drawing_id}/sessions")
async def sessions(drawing_id: str, request: Request):
    get_drawing(request, drawing_id)
    rows = request.app.state.store.all("SELECT * FROM sessions WHERE drawing_id=? ORDER BY created_at DESC", (drawing_id,))
    return [{**row, **await refresh_status(request, row["id"])} for row in rows]


@router.post("/drawings/{drawing_id}/sessions", status_code=201)
async def create_session(drawing_id: str, request: Request):
    drawing = get_drawing(request, drawing_id)
    title = f"{drawing['name']} · 图纸对话"
    await request.app.state.file_tasks.ensure_ready()
    session = await request.app.state.agent.create_session(title)
    request.app.state.store.execute("INSERT INTO sessions (id,drawing_id,title,busy,created_at,engine) VALUES (?,?,?,?,?,?)", (session["id"], drawing_id, title, 0, now(), "freecad"))
    return {"id": session["id"], "title": title, "status": "idle", "engine": "freecad"}


@router.get("/sessions/{session_id}/messages")
async def messages(session_id: str, request: Request):
    get_session(request, session_id)
    raw = await request.app.state.agent.messages(session_id)
    result = []
    for message in raw:
        info = message.get("info", message)
        if is_internal_message(message):
            continue
        parts = [visible for part in message.get("parts", []) if (visible := visible_part(part))]
        if info.get("role") == "user" and not parts:
            continue
        # Do not expose internal context, reasoning history or system prompts.
        result.append({"id": info.get("id"), "role": info["role"], "parts": parts, "created_at": info.get("time", {}).get("created"), "error": "模型任务未正常完成，请重试。" if info.get("error") else None})
    return result


@router.get("/sessions/{session_id}/status")
async def status(session_id: str, request: Request):
    return await refresh_status(request, session_id)


class MessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    expected_revision: str


@router.post("/sessions/{session_id}/messages", status_code=202)
async def send(session_id: str, body: MessageBody, request: Request):
    state = request.app.state
    session = get_session(request, session_id)
    drawing = get_drawing(request, session["drawing_id"])
    if drawing["current_revision"] != body.expected_revision:
        raise HTTPException(409, "图纸已有新版本，请切回当前版本后发送。")
    if not body.text.strip():
        raise HTTPException(422, "请输入问题。")
    try:
        await asyncio.wait_for(state.agent_ready.wait(), timeout=8)
    except TimeoutError:
        raise HTTPException(503, "智能体事件服务未连接，请稍后重试。") from None
    task_id = await state.file_tasks.start(session, drawing, body.text)
    return {"accepted": True, "session_id": session_id, "task_id": task_id}


@router.post("/sessions/{session_id}/abort")
async def abort(session_id: str, request: Request):
    session = get_session(request, session_id)
    if session.get("engine") != "freecad":
        raise HTTPException(409, "旧会话已归档，请新建 FreeCAD 对话。")
    stopped = await request.app.state.file_tasks.abort(session_id)
    return {"status": "stopped" if stopped else "stopping", "model_stopped": stopped, "jobs": []}


@router.get("/sessions/{session_id}/events")
async def events(session_id: str, request: Request):
    session = get_session(request, session_id)

    async def stream():
        async with request.app.state.bus.subscribe() as queue:
            yield 'data: {"type":"connected"}\n\n'
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if event.get("drawing_id") == session["drawing_id"] or event_session_id(event) == session_id:
                    yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
