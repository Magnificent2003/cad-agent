"""HTTP transport for the actual OpenCode agent host; no local model loop."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from collections.abc import AsyncIterator, Iterable
from typing import Any
from urllib.parse import quote

import httpx

from .preview import PreviewUnavailable, read_preview

logger = logging.getLogger(__name__)


class AgentError(RuntimeError):
    """A credential-safe upstream error suitable for the business API."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def event_session_id(event: dict[str, Any]) -> str | None:
    """Extract ownership from released OpenCode event shapes, never from text."""
    properties = event.get("properties") or {}
    if properties.get("sessionID"):
        return str(properties["sessionID"])
    for field in ("part", "info"):
        value = properties.get(field) or {}
        if value.get("sessionID"):
            return str(value["sessionID"])
    if event.get("type") in {"session.created", "session.updated", "session.deleted"}:
        return (properties.get("info") or {}).get("id")
    return None


class AgentClient:
    def __init__(
        self,
        settings: Any = None,
        *,
        base_url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        directory: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        def setting(name: str, default: Any) -> Any:
            return getattr(settings, name, default) if settings is not None else default

        def secret(value: Any) -> str:
            return value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)

        self._client = httpx.AsyncClient(
            base_url=(base_url or setting("agent_base_url", "http://127.0.0.1:4096")).rstrip("/"),
            auth=httpx.BasicAuth(
                username or setting("agent_server_username", "cad-agent"),
                secret(password if password is not None else setting("agent_server_password", "")),
            ),
            timeout=httpx.Timeout(45.0, connect=5.0),
            transport=transport,
            trust_env=False,
            params={"directory": directory} if directory else None,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AgentClient:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = "智能体认证失败，请检查服务密码" if exc.response.status_code == 401 else f"智能体请求失败（HTTP {exc.response.status_code}）"
            raise AgentError(message) from exc
        except httpx.RequestError as exc:
            raise AgentError("无法连接 智能体服务，请检查服务是否启动", 503) from exc
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def health(self) -> dict[str, Any]:
        return await self._request("GET", "/global/health")

    async def create_session(self, title: str = "CAD 图纸对话") -> dict[str, Any]:
        return await self._request("POST", "/session", json={"title": title})

    async def messages(self, session_id: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/session/{quote(session_id, safe='')}/message")

    async def prompt_async(self, session_id: str, text: str, system: str | None = None, *, internal: bool = False, preview: Path | None = None) -> None:
        part: dict[str, Any] = {"type": "text", "text": text}
        if internal:
            part.update(synthetic=True, metadata={"cad_task_recovery": True})
        payload: dict[str, Any] = {"agent": "cad", "parts": [part]}
        if preview is not None:
            try:
                data, _ = await asyncio.to_thread(read_preview, preview)
            except PreviewUnavailable as exc:
                logger.warning("Agent preview omitted: %s", exc)
                system = (system or "") + "\n本次未附加预览图片：" + str(exc) + "继续使用当前任务的 CAD 输入文件。"
            else:
                payload["parts"].append({
                    "type": "file", "mime": "image/png", "filename": preview.name,
                    "url": "data:image/png;base64," + base64.b64encode(data).decode("ascii"),
                })
        if system:
            payload["system"] = system
        await self._request("POST", f"/session/{quote(session_id, safe='')}/prompt_async", json=payload)

    async def abort(self, session_id: str) -> bool:
        return bool(await self._request("POST", f"/session/{quote(session_id, safe='')}/abort"))

    async def session_status(self) -> dict[str, Any]:
        return await self._request("GET", "/session/status")

    async def providers(self) -> dict[str, Any]:
        return await self._request("GET", "/provider")

    async def mcp_status(self) -> dict[str, Any]:
        return await self._request("GET", "/mcp")

    async def events(
        self,
        session_ids: Iterable[str] | None = None,
        *,
        ready: asyncio.Event | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Close the upstream stream on disconnect; readiness precedes prompts.

        OpenCode does not promise replay, so callers reconcile with messages().
        With an ownership filter, global events are intentionally not forwarded.
        """
        allowed = set(session_ids) if session_ids is not None else None
        timeout = httpx.Timeout(connect=5.0, read=None, write=30.0, pool=5.0)
        try:
            async with self._client.stream("GET", "/event", timeout=timeout) as response:
                response.raise_for_status()
                if ready is not None:
                    ready.set()
                data: list[str] = []
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip(" "))
                    elif not line and data:
                        try:
                            event = json.loads("\n".join(data))
                        except json.JSONDecodeError:
                            data.clear()
                            continue
                        data.clear()
                        if isinstance(event, dict) and (allowed is None or event_session_id(event) in allowed):
                            yield event
        except httpx.HTTPStatusError as exc:
            raise AgentError(f"智能体事件订阅失败（HTTP {exc.response.status_code}）") from exc
        except httpx.RequestError as exc:
            raise AgentError("智能体事件连接已断开，可重连恢复历史", 503) from exc
