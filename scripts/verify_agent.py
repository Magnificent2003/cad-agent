"""Check the agent host; --vision spends a small API call on synthetic data only."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import json
from io import StringIO
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.client import AgentClient, AgentError  # noqa: E402
from agent.launcher import build_config  # noqa: E402
from agent.runtime import engine_command, engine_environment, normalize_environment  # noqa: E402


def client_for(env: dict[str, str], base_url: str | None = None) -> AgentClient:
    env = normalize_environment(env)
    return AgentClient(base_url=base_url or env.get("AGENT_BASE_URL", "http://127.0.0.1:4096"), username=env.get("AGENT_SERVER_USERNAME", "cad-agent"), password=env.get("AGENT_SERVER_PASSWORD", ""))


async def diagnostics(env: dict[str, str]) -> None:
    async with client_for(env) as client:
        health, providers, mcp = await asyncio.gather(client.health(), client.providers(), client.mcp_status())
        print(json.dumps({"health": health, "connected_providers": providers.get("connected", []), "mcp": mcp}, ensure_ascii=False, indent=2))


async def exercise_cad(env: dict[str, str], timeout: int) -> None:
    """Use the complete business API and production MCP with generated CAD only."""
    import ezdxf
    import httpx

    doc = ezdxf.new("R2018")
    doc.units = 4
    model = doc.modelspace()
    model.add_lwpolyline([(0, 0), (100, 0), (100, 60), (0, 60)], close=True)
    model.add_text("DEMO ORIGINAL", dxfattribs={"height": 4, "insert": (10, 25)})
    text_buffer = StringIO()
    doc.write(text_buffer)
    payload = text_buffer.getvalue().encode("utf-8")
    runtime = ROOT / ".runtime" / "agent-cad-smoke"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "synthetic-original.dxf").write_bytes(payload)

    async with httpx.AsyncClient(base_url=env.get("CAD_API_BASE_URL", "http://127.0.0.1:8000"), timeout=45, trust_env=False) as api:
        async def request(method: str, path: str, **kwargs):
            response = await api.request(method, path, **kwargs)
            if response.is_error:
                raise RuntimeError(f"合成 CAD 验收接口失败：{path} HTTP {response.status_code}")
            return response.json()

        project = await request("POST", "/api/projects", json={"name": "合成 CAD 智能体验收"})
        upload = await request("POST", f"/api/projects/{project['id']}/drawings", files={"file": ("synthetic-agent-smoke.dxf", payload, "application/dxf")})
        deadline = time.monotonic() + timeout
        while True:
            job = await request("GET", f"/api/jobs/{upload['job_id']}")
            if job["status"] == "succeeded":
                break
            if job["status"] in {"failed", "cancelled", "interrupted"}:
                raise RuntimeError("合成图导入失败：" + str(job.get("error")))
            if time.monotonic() > deadline:
                raise RuntimeError("合成图导入超时")
            await asyncio.sleep(0.5)
        drawing_id = upload["drawing_id"]
        drawing = await request("GET", f"/api/drawings/{drawing_id}")
        initial_revision = drawing["current_revision"]
        session = await request("POST", f"/api/drawings/{drawing_id}/sessions")
        session_id = session["id"]
        ready = asyncio.Event()
        event_types: set[str] = set()
        published_revisions: set[str] = set()

        async def collect() -> None:
            async with api.stream("GET", f"/api/sessions/{session_id}/events", timeout=None) as response:
                response.raise_for_status()
                ready.set()
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        event = json.loads(line[5:])
                        event_types.add(event.get("type", "unknown"))
                        if event.get("type") == "drawing.updated":
                            published_revisions.add(event.get("revision_id", ""))

        subscription = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(ready.wait(), 10)
            await request("POST", f"/api/sessions/{session_id}/messages", json={"expected_revision": initial_revision, "text": "这是一张自动生成的合成测试图。请先读取概况和查询原生实体确认单位，将唯一 TEXT 的 DEMO ORIGINAL 修改为 DEMO UPDATED。创建新版本后再次读取实体核对，再用 render_region 工具查看新版本，Model 布局、bbox=[-5,-5,105,65]。只修改这一个文本，其余实体不变。最后简要说明实际变化。"})
            deadline = time.monotonic() + timeout
            while True:
                history = await request("GET", f"/api/sessions/{session_id}/messages")
                status = await request("GET", f"/api/sessions/{session_id}/status")
                if any(message.get("error") for message in history):
                    raise RuntimeError("合成 CAD 智能体返回模型错误，请检查本地会话")
                if status["status"] == "idle" and len(history) > 1:
                    break
                if time.monotonic() > deadline:
                    await request("POST", f"/api/sessions/{session_id}/abort")
                    raise RuntimeError("合成 CAD 智能体验收超时，已停止任务")
                await asyncio.sleep(1)
            calls = [part for message in history for part in message.get("parts", []) if part.get("type") == "tool"]
            completed_tools = [part.get("tool") for part in calls if (part.get("state") or {}).get("status") == "completed"]
            required = {"cad_summary", "cad_query_entities", "cad_apply_operations", "cad_render_region"}
            if not required.issubset(set(completed_tools)):
                raise RuntimeError("智能体没有完成所需 CAD 工具链：" + ", ".join(completed_tools))
            render_calls = [part for part in calls if part.get("tool") == "cad_render_region" and (part.get("state") or {}).get("status") == "completed"]
            image_attachments = [attachment for part in render_calls for attachment in (part.get("state") or {}).get("attachments", []) if attachment.get("mime") == "image/png" and attachment.get("url", "").startswith("data:image/png;base64,")]
            if not image_attachments:
                raise RuntimeError("正式 CAD MCP 渲染没有返回 PNG 图片附件")
            drawing = await request("GET", f"/api/drawings/{drawing_id}")
            final_revision = drawing["current_revision"]
            if final_revision == initial_revision:
                raise RuntimeError("没有发布新的 CAD 版本")
            download = await api.get(f"/api/drawings/{drawing_id}/revisions/{final_revision}/download?format=dxf")
            download.raise_for_status()
            (runtime / "synthetic-updated.dxf").write_bytes(download.content)
            updated = ezdxf.read(StringIO(download.text))
            assert [entity.dxf.text for entity in updated.modelspace().query("TEXT")] == ["DEMO UPDATED"]
            assert len(updated.modelspace()) == len(model)
            assert list(updated.modelspace().query("LWPOLYLINE")[0].get_points()) == list(model.query("LWPOLYLINE")[0].get_points())
            png = await api.get(f"/api/drawings/{drawing_id}/revisions/{final_revision}/preview?format=png")
            png.raise_for_status()
            assert png.content.startswith(b"\x89PNG\r\n\x1a\n")
            (runtime / "synthetic-updated.png").write_bytes(png.content)
            assert final_revision in published_revisions, "SSE missing drawing.updated"
            report = {"passed": True, "data": "synthetic DXF only", "model": env.get("DEEPSEEK_MODEL", "deepseek-flash"), "drawing_id": drawing_id, "session_id": session_id, "initial_revision": initial_revision, "final_revision": final_revision, "tools_completed": completed_tools, "native_text_verified": "DEMO UPDATED", "unrelated_polyline_unchanged": True, "png_preview_verified": True, "mcp_png_attachment_verified": True, "event_types": sorted(event_types), "drawing_updated_event_verified": True}
            (runtime / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            subscription.cancel()
            with suppress(asyncio.CancelledError):
                await subscription


async def exercise_vision(env: dict[str, str], port: int, timeout: int) -> dict:
    async with client_for(env, f"http://127.0.0.1:{port}") as client:
        deadline = time.monotonic() + 60
        while True:
            try:
                health = await client.health()
                break
            except AgentError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("合成验收 智能体 服务未能启动；检查 .runtime/vision-smoke/server.log")
                await asyncio.sleep(0.5)
        mcp_status = await client.mcp_status()
        if (mcp_status.get("cad") or {}).get("status") != "connected":
            raise RuntimeError("合成验收 MCP 未连接；检查 .runtime/vision-smoke/server.log")
        session = await client.create_session("Synthetic PNG vision acceptance")
        ready = asyncio.Event()
        event_types: set[str] = set()

        async def collect() -> None:
            async for event in client.events({session["id"]}, ready=ready):
                event_types.add(event.get("type", "unknown"))

        subscription = asyncio.create_task(collect())
        try:
            await asyncio.wait_for(ready.wait(), timeout=10)
            await client.prompt_async(session["id"], "执行合成图片验收：先调用 cad_render_region 看图，识别图中四位数字和红色圆圈数量，再调用 cad_verify_visual 提交你看到的结果。通过后用一句话结束。必须通过工具查看图片。")
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                history = await client.messages(session["id"])
                calls = [part for message in history for part in message.get("parts", []) if part.get("type") == "tool"]
                completed = [part for part in calls if (part.get("state") or {}).get("status") == "completed"]
                rendered = any(part.get("tool") == "cad_render_region" for part in completed)
                for part in completed:
                    if part.get("tool") != "cad_verify_visual":
                        continue
                    output = (part.get("state") or {}).get("output", "")
                    if '"verified": true' in output or '"verified":true' in output:
                        if not rendered:
                            raise RuntimeError("视觉校验没有对应的图片工具调用")
                        return {"passed": True, "agent": health, "model": env.get("DEEPSEEK_MODEL", "deepseek-flash"), "data": "synthetic PNG only", "image_tool": "cad_render_region", "followup_tool": "cad_verify_visual", "session_id": session["id"], "event_types": sorted(event_types)}
                assistant_done = any(message.get("info", {}).get("role") == "assistant" and message.get("info", {}).get("time", {}).get("completed") and message.get("info", {}).get("finish") not in {None, "tool-calls", "unknown"} for message in history)
                if assistant_done:
                    raise RuntimeError("模型已结束但未通过图片识别后的工具校验；不能宣称图片通道可用")
                errors = [message.get("info", {}).get("error") for message in history if message.get("info", {}).get("error")]
                if errors:
                    name = errors[-1].get("name", "unknown") if isinstance(errors[-1], dict) else "unknown"
                    raise RuntimeError(f"模型验收失败（{name}）；详情保留在本地 智能体 会话中")
                await asyncio.sleep(1)
            await client.abort(session["id"])
            raise RuntimeError("视觉验收超时，已停止合成会话")
        finally:
            subscription.cancel()
            with suppress(asyncio.CancelledError):
                await subscription


def vision(env: dict[str, str], port: int, timeout: int) -> None:
    env = normalize_environment(env)
    required = [name for name in ("DEEPSEEK_API_KEY", "AGENT_SERVER_PASSWORD") if not env.get(name)]
    if required:
        raise RuntimeError("缺少环境变量：" + ", ".join(required))
    command = engine_command(env)
    runtime = ROOT / ".runtime" / "vision-smoke"
    runtime.mkdir(parents=True, exist_ok=True)
    config = build_config(env)
    config["agent"]["cad"]["prompt"] = "你是合成图片验收助手。仅通过工具读取图片并校验图中文字和形状，不猜测、不调用其他工具。"
    config["mcp"]["cad"] = {"type": "local", "enabled": True, "command": [str(ROOT / ".venv" / "bin" / "python"), "-m", "cad_mcp.vision_smoke"], "environment": {"PYTHONPATH": str(ROOT), "CAD_SMOKE_CODE": str(secrets.randbelow(9000) + 1000), "CAD_SMOKE_CIRCLES": str(secrets.randbelow(4) + 1)}, "timeout": 60000}
    child_env = engine_environment(env, config, runtime)
    workspace = runtime / "workspace"
    workspace.mkdir(exist_ok=True)
    with (runtime / "server.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen([*command, "serve", "--hostname", "127.0.0.1", "--port", str(port)], cwd=workspace, env=child_env, stdout=log, stderr=log, start_new_session=True)
        try:
            report = asyncio.run(exercise_vision(child_env, port, timeout))
            (runtime / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vision", action="store_true", help="use a separate 智能体 instance and billable DeepSeek call on a generated test PNG")
    parser.add_argument("--cad", action="store_true", help="use the running API and 智能体 to edit a generated synthetic DXF (billable model calls)")
    parser.add_argument("--port", type=int, default=4097)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    try:
        if args.vision and args.cad:
            raise RuntimeError("请选择 --vision 或 --cad 中的一种验收")
        if args.cad:
            asyncio.run(exercise_cad(dict(os.environ), args.timeout))
        elif args.vision:
            vision(dict(os.environ), args.port, args.timeout)
        else:
            asyncio.run(diagnostics(dict(os.environ)))
    except (RuntimeError, AgentError) as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
