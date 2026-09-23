"""Transport boundaries: authentication, event ownership, and safe failures."""

import asyncio
import base64
import json

import httpx
import pytest

from agent.client import AgentClient, AgentError, event_session_id


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
