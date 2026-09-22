"""Session polling must wait for this task and never print raw CAD payloads."""
import json

import pytest

from agent.client import AgentError
from agent.freecad_chat import run_session, session_name


def assistant(message_id, *, finish="stop", text="done", tool_status=None):
    parts = [{"type": "text", "text": text}]
    if tool_status:
        parts.append({
            "id": "tool-1", "type": "tool", "tool": "freecad_execute_code",
            "state": {"status": tool_status, "output": "long-private-tool-output"},
        })
    return {"info": {"id": message_id, "role": "assistant", "time": {"completed": 1}, "finish": finish}, "parts": parts}


class FakeClient:
    def __init__(self, turns, previous=None):
        self.turns = turns
        self.previous = previous or []
        self.prompt = None
        self.reads = 0
        self.aborted = False

    async def mcp_status(self):
        return {"freecad": {"status": "connected"}}

    async def create_session(self, title):
        return {"id": "ses_test"}

    async def session_status(self):
        return {}

    async def messages(self, session_id):
        if self.prompt is None:
            return self.previous
        turn = self.turns[min(self.reads, len(self.turns) - 1)]
        self.reads += 1
        return self.previous + turn

    async def prompt_async(self, session_id, text):
        self.prompt = text

    async def abort(self, session_id):
        self.aborted = True
        return True


async def test_chat_waits_past_old_response_and_tool_turn_then_saves_full_report(tmp_path, capsys):
    tool_message = assistant("a1", finish="tool-calls", tool_status="completed")
    client = FakeClient(
        [[], [tool_message], [tool_message, assistant("a2", text="文件已另存")]],
        previous=[assistant("old", text="previous response")],
    )
    result = await run_session(client, "修改图纸", session_id="ses_test", reports_dir=tmp_path, poll_interval=0)
    assert result == 0
    assert client.reads == 3
    output = capsys.readouterr().out
    assert "freecad_execute_code：已返回" in output
    assert "文件已另存" in output
    assert "long-private-tool-output" not in output
    assert "previous response" not in output
    report = json.loads((tmp_path / "ses_test.json").read_text())
    assert report["status"] == "completed"
    assert report["messages"] == client.previous + client.turns[-1]
    assert report["messages"][1]["parts"][1]["state"]["output"] == "long-private-tool-output"


async def test_chat_timeout_requests_abort_and_leaves_report(tmp_path):
    client = FakeClient([[]])
    result = await run_session(client, "任务", timeout=0.01, reports_dir=tmp_path, poll_interval=0.1)
    assert result == 124
    assert client.aborted
    report = json.loads((tmp_path / "ses_test.json").read_text())
    assert report["status"] == "timeout"
    assert report["abort_requested"] is True


async def test_chat_refuses_normal_cad_service_without_sending_prompt(tmp_path):
    client = FakeClient([[]])

    async def wrong_mcp():
        return {"cad": {"status": "connected"}}

    client.mcp_status = wrong_mcp
    with pytest.raises(AgentError, match="agent.freecad_experiment"):
        await run_session(client, "任务", reports_dir=tmp_path)
    assert client.prompt is None
    assert not list(tmp_path.iterdir())


async def test_chat_does_not_append_to_running_session(tmp_path):
    client = FakeClient([[]])

    async def busy():
        return {"ses_test": {"type": "busy"}}

    client.session_status = busy
    with pytest.raises(AgentError, match="仍在运行"):
        await run_session(client, "任务", session_id="ses_test", reports_dir=tmp_path)
    assert client.prompt is None


async def test_chat_reports_model_error_without_dumping_error_payload(tmp_path, capsys):
    failure = assistant("failed", text="")
    failure["info"]["error"] = {"name": "ProviderError", "data": {"message": "private-error-details"}}
    client = FakeClient([[failure]])
    result = await run_session(client, "任务", reports_dir=tmp_path, poll_interval=0)
    assert result == 1
    output = capsys.readouterr().out
    assert "ProviderError" in output
    assert "private-error-details" not in output
    report = json.loads((tmp_path / "ses_test.json").read_text())
    assert report["messages"][0]["info"]["error"]["data"]["message"] == "private-error-details"


def test_report_session_name_cannot_escape_report_directory():
    with pytest.raises(ValueError):
        session_name("../../outside")
