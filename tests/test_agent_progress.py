"""Visible execution evidence must survive reload without exposing internals."""
import json
from types import SimpleNamespace

import pytest

from agent.progress import tool_progress
from backend.app.api.chat import MessageEventFilter, visible_part


def part(output, *, tool="execute_code", status="completed", **state):
    return {"type": "tool", "id": "p1", "messageID": "m1", "sessionID": "s1",
            "tool": "freecad_" + tool, "state": {"status": status, "output": output,
            "time": {"start": 1000, "end": 123000}, **state}}


@pytest.mark.parametrize("output,tool", [
    ("Failed to execute code: GUI dispatch gave up after 122.3s waiting for 'execute_code' to start", "execute_code"),
    ("Headless FreeCAD script FAILED: ValueError: invalid geometry", "execute_code_headless"),
    ("Async job job-7: failed\nError: ValueError: invalid geometry", "get_async_status"),
])
def test_completed_transport_does_not_hide_execution_failure(output, tool):
    state = visible_part(part(output, tool=tool))["state"]
    assert state["status"] == "completed"
    assert state["outcome"] == "error"
    assert state["time"] == {"start": 1000, "end": 123000}
    if "GUI dispatch" in output:
        assert "等待执行超时" in state["summary"]
        assert "尚未开始" in state["details"]


def test_unknown_review_discloses_skipped_checks_not_engineering_approval():
    state = visible_part(part(json.dumps({
        "status": "unknown", "issues": [{"code": "unsupported_site_geometry",
            "message": "A widened polyline cannot be treated as a zero-width polygon boundary.", "handles": ["AE9E"]}],
        "checks": {"site_dependent_geometry": "skipped_unconfirmed_facts",
                   "site_geometry": {"construction_norms": "not_tested"}},
        "file_sha256": "a12891a8bfd2acf1ff18e1aac5149b0d20bac5db0258f25bac0142bb0252fd58",
    }), tool="cad_review"))["state"]
    assert state["outcome"] == "error"
    assert "AE9E" in state["details"] and "已跳过" in state["details"]
    assert "未检查施工规范" in state["details"] and "a12891a8bfd2" in state["details"]


async def test_history_and_live_share_projection_and_hide_raw_material():
    raw = part("Code executed successfully.\nOutput: components created: 15\n"
               "saved: /home/private/project/output/layout.dxf\n"
               "saved: C:\\Users\\private\\layout.dxf\n"
               "entities: {'DEEPSEEK_API_KEY':'dont-show-this'}\n"
               "preview: data:image/png;base64,AAAA\n"
               "raw unselected output\n",
               input={"code": "private code"}, title="private title",
               attachments=[{"url": "private image"}])
    history = visible_part(raw)
    events = MessageEventFilter(SimpleNamespace())
    await events.filter({"type": "message.updated", "properties": {"info": {
        "id": "m1", "role": "assistant", "sessionID": "s1", "finish": "tool-calls"}}}, "s1")
    live = await events.filter({"type": "message.part.updated", "properties": {"part": raw}}, "s1")
    assert live["properties"]["part"] == history
    serialized = json.dumps(history)
    for hidden in ("private", "dont-show-this", "base64", "AAAA", "unselected", '"input"', '"output"', '"attachments"'):
        assert hidden not in serialized
    assert "components created: 15" in serialized
    assert visible_part({"type": "reasoning", "text": "private reasoning"}) is None
    assert visible_part({"type": "text", "text": "private summary", "synthetic": True}) is None


def test_running_projection_does_not_expose_input_or_fake_a_result():
    state = tool_progress("freecad_cad_layout", {"status": "running", "input": {"code": "private"},
                                               "time": {"start": 1000}})
    assert state["outcome"] == "running" and state["details"] == ""
    assert state["time"] == {"start": 1000}
    assert "正在" in state["summary"]


@pytest.mark.parametrize("output", [
    "Failed to execute code: FreeCAD is blocked by modal dialog 'Document Recovery'. "
    "The operation already started and cannot be safely cancelled. It may continue after the dialog closes; "
    "inspect its result before retrying writes.",
    json.dumps({"success": False, "code": "GUI_MODAL_BLOCKED", "operation_started": True,
                "cancelled": False, "error": "FreeCAD is blocked by modal dialog 'Document Recovery'."}),
    "Failed to execute code: GUI dispatch timed out after 120s while 'execute_code' was still running "
    "on FreeCAD's GUI thread. The task cannot be safely cancelled.",
])
def test_started_operations_are_never_described_as_cancelled_or_safe_to_repeat(output):
    state = visible_part(part(output))["state"]
    assert state["outcome"] == "error"
    assert "已经开始" in state["details"] and "核对" in state["details"]
    assert "尚未" not in state["details"] and "已取消排队" not in state["details"]


def test_cancelled_modal_request_is_distinguished_from_started_operation():
    state = visible_part(part(json.dumps({
        "success": False, "code": "GUI_MODAL_BLOCKED", "operation_started": False,
        "cancelled": True, "error": "FreeCAD is blocked by modal dialog 'Document Recovery'.",
    })))["state"]
    assert state["outcome"] == "error"
    assert "尚未执行，已取消排队" in state["details"]


def test_rpc_stuck_is_execution_failure_not_a_normal_status_reply():
    state = visible_part(part(json.dumps({"success": True, "gui_dispatch": {
        "state": "stuck", "operation": "execute_code", "running_for_seconds": 180,
    }}), tool="get_rpc_status"))["state"]
    assert state["outcome"] == "error" and "仍在执行" in state["summary"]
    assert "无法安全取消" in state["details"] and "不要重复提交" in state["details"]
