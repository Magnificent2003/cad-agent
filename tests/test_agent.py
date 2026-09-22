"""Transport boundaries: authentication, event ownership, and safe failures."""

import asyncio
import base64
import json
from io import BytesIO

import httpx
import pytest

from agent.client import AgentClient, AgentError, event_session_id
from cad_mcp.client import CADAPI


async def test_auth_applies_to_prompt_history_abort_and_sse():
    requests = []

    def upstream(request):
        requests.append(request)
        expected = base64.b64encode(b"agent-user:local-password").decode()
        assert request.headers["authorization"] == f"Basic {expected}"
        if request.url.path == "/event":
            return httpx.Response(200, text='data: {"type":"session.status","properties":{"sessionID":"s1","status":{"type":"idle"}}}\n\n')
        if request.url.path.endswith("prompt_async"):
            body = json.loads(request.content)
            assert body["agent"] == "cad"
            assert body["system"] == "bound drawing context"
            return httpx.Response(204)
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json=[{"info": {"role": "assistant"}, "parts": [{"type": "reasoning", "text": "preserved upstream history"}]}])
        if request.url.path.endswith("/abort"):
            return httpx.Response(200, json=True)
        return httpx.Response(200, json={"id": "s1"})

    async with AgentClient(username="agent-user", password="local-password", transport=httpx.MockTransport(upstream)) as client:
        session = await client.create_session()
        assert session["id"] == "s1"
        await client.prompt_async("s1", "查询图纸", system="bound drawing context")
        history = await client.messages("s1")
        assert history[0]["parts"][0]["type"] == "reasoning"
        assert await client.abort("s1")
        assert len([event async for event in client.events({"s1"})]) == 1
    assert len(requests) == 5


@pytest.mark.parametrize(
    "event,expected",
    [
        ({"type": "message.part.delta", "properties": {"sessionID": "s1"}}, "s1"),
        ({"type": "message.part.updated", "properties": {"part": {"sessionID": "s1"}}}, "s1"),
        ({"type": "message.updated", "properties": {"info": {"sessionID": "s1"}}}, "s1"),
        ({"type": "session.updated", "properties": {"info": {"id": "s1"}}}, "s1"),
        ({"type": "server.connected", "properties": {}}, None),
        ({"type": "message.updated", "properties": {"info": {"id": "message-id"}}}, None),
    ],
)
def test_event_ownership(event, expected):
    assert event_session_id(event) == expected


async def test_sse_ignores_other_sessions_heartbeat_and_bad_json():
    data = (
        ": heartbeat\n\n"
        'data: {"type":"server.connected","properties":{}}\n\n'
        "data: invalid json\n\n"
        'data: {"type":"message.part.delta",\n'
        'data: "properties":{"sessionID":"ours","delta":"你好"}}\n\n'
        'data: {"type":"message.part.updated","properties":{"part":{"sessionID":"other"}}}\n\n'
    )

    def upstream(request):
        assert request.extensions["timeout"]["read"] is None
        return httpx.Response(200, text=data)

    ready = asyncio.Event()
    async with AgentClient(transport=httpx.MockTransport(upstream)) as client:
        events = [event async for event in client.events({"ours"}, ready=ready)]
    assert ready.is_set()
    assert [event["properties"]["delta"] for event in events] == ["你好"]


async def test_error_never_echoes_upstream_body():
    def upstream(request):
        return httpx.Response(401, text="sensitive upstream body")

    async with AgentClient(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(AgentError, match="认证失败") as error:
            await client.create_session()
        assert "sensitive" not in str(error.value)
        ready = asyncio.Event()
        with pytest.raises(AgentError, match="401"):
            _ = [event async for event in client.events(ready=ready)]
        assert not ready.is_set()


async def test_unavailable_upstream_is_a_service_error():
    def upstream(request):
        raise httpx.ConnectError("connection failed", request=request)

    async with AgentClient(transport=httpx.MockTransport(upstream)) as client:
        with pytest.raises(AgentError) as error:
            await client.health()
        assert error.value.status_code == 503


async def test_disconnecting_sse_closes_transport():
    class Stream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'data: {"type":"server.connected","properties":{}}\n\n'
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    stream = Stream()
    async with AgentClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))) as client:
        events = client.events()
        assert (await anext(events))["type"] == "server.connected"
        await events.aclose()
    assert stream.closed


async def test_mcp_png_keeps_crop_url_and_returns_image_bytes(monkeypatch):
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (16, 16), "red").save(buffer, format="PNG")
    png = buffer.getvalue()
    monkeypatch.setenv("CAD_INTERNAL_TOKEN", "local-test-token")

    def upstream(request):
        assert request.url.params["render_id"] == "cropped-region"
        assert request.headers["authorization"] == "Bearer local-test-token"
        return httpx.Response(200, content=png, headers={"Content-Type": "image/png"})

    async with CADAPI(transport=httpx.MockTransport(upstream)) as api:
        content = await api.png("/internal/preview?format=png&render_id=cropped-region", {"revision_id": "r0002"})
    assert content[0].type == "text"
    assert content[1].type == "image"
    assert content[1].mime_type == "image/png"
    assert base64.b64decode(content[1].data) == png


async def test_mcp_immediate_dxf_export_needs_no_job_id(monkeypatch):
    monkeypatch.setenv("CAD_INTERNAL_TOKEN", "local-test-token")

    def unexpected_request(request):
        raise AssertionError("A completed export must not trigger job polling")

    result = {"status": "succeeded", "result": {"download_url": "/api/drawing.dxf"}}
    async with CADAPI(transport=httpx.MockTransport(unexpected_request)) as api:
        assert await api.wait_job(result) == result


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("query_entities", {}),
        ("read_entities", {"entity_ids": ["e1"]}),
        ("render_region", {"bbox": [0, 0, 10, 10]}),
        ("check_geometry", {"constraints": []}),
        ("export", {"format": "dwg"}),
    ],
)
async def test_mcp_queued_tools_bind_trusted_session(monkeypatch, name, arguments):
    from cad_mcp import server

    requests = []

    class API:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def request(self, method, path, **kwargs):
            requests.append(kwargs["json"])
            return {"id": "job", "status": "succeeded", "result": {"preview_url": "/internal/preview?render_id=crop"}}

        async def wait_job(self, job):
            return job

        async def png(self, *args, **kwargs):
            return []

    monkeypatch.setattr(server, "CADAPI", API)
    await getattr(server, name)(drawing_id="drawing", revision_id="r0001", session_id="trusted-session", **arguments)
    assert len(requests) == 1
    assert requests[0]["session_id"] == "trusted-session"
