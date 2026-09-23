"""Internal summaries stay private and model stops do not imply delivery."""
import asyncio
import copy
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from agent.client import AgentError
from agent.session import has_visible_text, needs_continuation, terminal_assistant
from backend.app.api import chat
from backend.app.main import agent_events


def message(mid, *, role="assistant", finish="stop", text="已完成图纸介绍。", **fields):
    info = {"id": mid, "sessionID": "s1", "role": role, "time": {"created": 1, "completed": 2}, **fields}
    if role == "assistant":
        info["finish"] = finish
    parts = [] if text is None else [{"id": "p-" + mid, "messageID": mid, "sessionID": "s1", "type": "text", "text": text}]
    return {"info": info, "parts": parts}


@pytest.mark.parametrize("fields", [{"summary": True}, {"agent": "compaction"}, {"mode": "compaction"}])
def test_compaction_is_never_a_terminal_answer(fields):
    summary = message("summary", text="## Objective\nInternal context", **fields)
    assert terminal_assistant([summary], set()) is None
    assert not has_visible_text(summary)
    assert not needs_continuation(summary)
    pending = message("work", finish="tool-calls")
    assert terminal_assistant([pending, summary], set()) is None


def test_latest_incomplete_assistant_cannot_be_masked_by_previous_final_reply():
    final = message("old")
    assert terminal_assistant([final], {"old"}) is None
    pending = message("pending", finish=None)
    pending["info"]["time"] = {"created": 3}
    # Even an out-of-order imported list must not expose the earlier final answer.
    assert terminal_assistant([pending, final], set()) is None
    pending["info"].update(finish="tool-calls", time={"created": 3, "completed": 4})
    assert terminal_assistant([final, pending], set()) is None


@pytest.mark.parametrize("finish,text,extra,recover", [
    ("length", None, {}, True),
    ("length", "尚未完成的答复", {}, True),
    ("stop", None, {}, True),
    ("stop", "   ", {}, True),
    ("stop", "图纸为总平面施工图。", {}, False),
    ("content-filter", None, {}, False),
    ("stop", None, {"error": {"name": "MessageAbortedError"}}, False),
])
def test_stopped_turns_preserve_recovery_information(finish, text, extra, recover):
    reply = message("new", finish=finish, text=text, **extra)
    if text is None:
        reply["parts"] = [{"type": "reasoning", "text": "hidden reasoning"}]
    assert terminal_assistant([reply], set()) == reply
    assert needs_continuation(reply) is recover


def test_synthetic_text_does_not_satisfy_delivery():
    reply = message("new")
    reply["parts"][0]["synthetic"] = True
    assert terminal_assistant([reply], set()) == reply
    assert not has_visible_text(reply)
    assert needs_continuation(reply)


class HistoryAgent:
    def __init__(self, history, events=()):
        self.history = history
        self.script = events
        self.exhausted = asyncio.Event()
        self.lookups = 0

    async def messages(self, session_id):
        self.lookups += 1
        return self.history

    async def events(self, ready=None):
        if ready:
            ready.set()
        for event in self.script:
            yield copy.deepcopy(event)
        self.exhausted.set()
        await asyncio.Event().wait()


def event(kind, **properties):
    return {"type": kind, "properties": properties}


def stream_message(reply):
    result = [event("message.updated", info=reply["info"])]
    for part in reply["parts"]:
        result.append(event("message.part.updated", part=part))
        if part["type"] == "text":
            result.append(event("message.part.delta", sessionID="s1", messageID=reply["info"]["id"],
                                partID=part["id"], field="text", delta=part["text"]))
    return result


async def test_history_endpoint_filters_internal_messages_without_keyword_filters():
    user = message("user", role="user", text="简单介绍一下这张施工图")
    user["parts"].append({"type": "text", "text": "private task context", "synthetic": True})
    summary = message("summary", summary=True, agent="compaction", text="## Objective\nprivate summary")
    compact = message("compact", role="user", text=None)
    compact["parts"] = [{"type": "compaction", "auto": True}]
    followup = message("continue", role="user", text="private continuation")
    followup["parts"][0]["synthetic"] = True
    response = message("response", text="图纸中有文字 Objective，这只是普通图纸内容。")
    response["parts"].extend([
        {"type": "reasoning", "text": "private reasoning"},
        {"type": "tool", "tool": "freecad_execute_code", "state": {
            "status": "completed", "input": {"code": "private code"}, "output": "private tool output",
        }},
    ])
    app = FastAPI()
    app.include_router(chat.router)
    app.state.store = SimpleNamespace(one=lambda *_: {"id": "s1"})
    app.state.agent = HistoryAgent([user, compact, summary, followup, response])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        result = await client.get("/api/sessions/s1/messages")
    assert result.status_code == 200
    assert [item["id"] for item in result.json()] == ["user", "response"]
    assert "private" not in result.text
    assert "Objective" in result.text
    assert result.json()[1]["parts"][1]["state"]["status"] == "completed"


async def test_live_event_pump_filters_summary_parts_and_deltas():
    summary = message("summary", summary=True, agent="compaction", text="private summary")
    followup = message("continue", role="user", text="private continuation")
    followup["parts"][0].update(synthetic=True, metadata={"compaction_continue": True})
    answer = message("answer", text="已完成图纸介绍。")
    answer["parts"].append({"id": "reasoning", "messageID": "answer", "sessionID": "s1",
                            "type": "reasoning", "text": "private reasoning"})
    agent = HistoryAgent([], [*stream_message(summary), *stream_message(followup), *stream_message(answer)])
    received = []
    state = SimpleNamespace(agent=agent, agent_ready=asyncio.Event(),
                            store=SimpleNamespace(one=lambda *_: {"id": "s1"}),
                            bus=SimpleNamespace(publish=received.append))
    task = asyncio.create_task(agent_events(SimpleNamespace(state=state)))
    try:
        await asyncio.wait_for(agent.exhausted.wait(), 2)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    serialized = json.dumps(received, ensure_ascii=False)
    assert "private" not in serialized and "summary" not in serialized and "continue" not in serialized
    assert [item["type"] for item in received] == ["message.updated", "message.part.updated", "message.part.delta"]
    assert "已完成图纸介绍" in serialized


async def test_reconnected_parts_require_parent_verification():
    summary = message("summary", summary=True, text="private summary")
    normal = message("normal")
    agent = HistoryAgent([summary, normal])
    filtered = chat.MessageEventFilter(agent)
    assert await filtered.filter(stream_message(summary)[1], "s1") is None
    assert await filtered.filter(stream_message(summary)[2], "s1") is None
    assert await filtered.filter(stream_message(normal)[1], "s1") is not None
    assert agent.lookups == 1
    # Unknown parents and deltas with no approved text part must not pass.
    unknown = message("unknown", text="private unknown")
    assert await filtered.filter(stream_message(unknown)[1], "s1") is None
    assert await filtered.filter(stream_message(unknown)[2], "s1") is None
    forged = event("message.part.delta", sessionID="s2", messageID="normal", partID="p-normal", field="text", delta="private forged")
    assert await filtered.filter(forged, "s2") is None


async def test_failed_parent_lookup_does_not_guess_visibility():
    class UnavailableAgent:
        async def messages(self, _):
            raise AgentError("unavailable")

    filtered = chat.MessageEventFilter(UnavailableAgent())
    reply = message("unknown", text="private unknown")
    assert await filtered.filter(stream_message(reply)[1], "s1") is None
    assert await filtered.filter(stream_message(reply)[2], "s1") is None
