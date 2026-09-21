"""Internal HTTP API client. CAD state belongs exclusively to the backend."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from typing import Any
from urllib.parse import quote

import httpx
from mcp.types import ImageContent, TextContent


def identifier(value: str) -> str:
    return quote(value, safe="")


class CADAPI:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        token = os.getenv("CAD_INTERNAL_TOKEN", "")
        if not token:
            raise ValueError("CAD_INTERNAL_TOKEN 未配置")
        self._client = httpx.AsyncClient(
            base_url=os.getenv("CAD_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(45, connect=5),
            trust_env=False,
            transport=transport,
        )

    async def __aenter__(self) -> CADAPI:
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._client.request(method, path, **kwargs)
        if response.is_error:
            # Business errors are useful, but never include raw headers or URLs.
            try:
                detail = response.json().get("detail", "CAD 服务请求失败")
            except (ValueError, AttributeError):
                detail = "CAD 服务请求失败"
            raise ValueError(f"HTTP {response.status_code}: {detail}")
        return response.json()

    async def wait_job(self, job: dict[str, Any], timeout: float = 300) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        job_id = job.get("job_id") or job.get("id")
        while True:
            status = job.get("status")
            if status in {"completed", "succeeded", "success", "done"}:
                return job
            if status in {"failed", "cancelled", "canceled", "interrupted"}:
                raise ValueError(f"CAD 任务 {status}: {job.get('error') or '未发布新版本'}")
            if not job_id:
                raise ValueError("CAD 服务没有返回任务标识")
            if time.monotonic() >= deadline:
                return {**job, "waiting": True, "message": "任务仍在运行，请使用 get_job 查询，不能报告已完成"}
            await asyncio.sleep(0.4)
            job = await self.request("GET", f"/internal/jobs/{identifier(str(job_id))}")

    async def png(self, path: str, metadata: dict[str, Any], **params: Any) -> list[TextContent | ImageContent]:
        response = await self._client.get(path, **({"params": params} if params else {}))
        response.raise_for_status()
        if not response.content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("CAD 预览返回了非 PNG 内容")
        return [
            TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
            ImageContent(type="image", data=base64.b64encode(response.content).decode("ascii"), mime_type="image/png"),
        ]
