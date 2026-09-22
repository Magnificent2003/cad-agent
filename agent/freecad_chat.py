"""Send a task to the isolated FreeCAD agent and keep a local session report.

Examples:
  python -m agent.freecad_chat '查看 simple.dxf，报告其中的对象'
  python -m agent.freecad_chat --prompt-file task.txt --timeout 900
  python -m agent.freecad_chat '继续修改并另存为 DXF' --session ses_...

Start agent.freecad_experiment first. This command does not use the web app's
agent service, and it never prints raw tool payloads or image data.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

from .client import AgentClient, AgentError
from .freecad_experiment import STATE, WORKSPACE
from .runtime import ROOT, normalize_environment

REPORTS = STATE / "reports"


def session_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("会话 ID 只能包含英文字母、数字、下划线和连字符")
    return value


def save_report(report: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_name(report['session_id'])}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def terminal_assistant(messages: list[dict[str, Any]], previous_ids: set[str]) -> dict[str, Any] | None:
    current = [
        item for item in messages
        if item.get("info", {}).get("role") == "assistant"
        and item.get("info", {}).get("id") not in previous_ids
    ]
    if not current:
        return None
    message = current[-1]
    info = message["info"]
    if info.get("error") or (
        info.get("time", {}).get("completed")
        and info.get("finish") not in {None, "tool-calls", "unknown"}
    ):
        return message
    return None


def print_tool_progress(messages: list[dict[str, Any]], previous_ids: set[str], observed: dict[str, str]) -> None:
    labels = {"pending": "待执行", "running": "运行中", "completed": "已返回", "error": "失败"}
    for message in messages:
        if message.get("info", {}).get("id") in previous_ids:
            continue
        for part in message.get("parts", []):
            if part.get("type") != "tool":
                continue
            key = part.get("id") or part.get("callID")
            status = part.get("state", {}).get("status", "unknown")
            if key and observed.get(key) != status:
                observed[key] = status
                print(f"[工具] {part.get('tool', 'unknown')}：{labels.get(status, status)}", flush=True)


async def run_session(
    client: AgentClient,
    prompt: str,
    *,
    session_id: str | None = None,
    timeout: float = 900,
    reports_dir: Path = REPORTS,
    poll_interval: float = 2,
) -> int:
    mcp = await client.mcp_status()
    if "cad" in mcp or mcp.get("freecad", {}).get("status") != "connected":
        raise AgentError("目标服务没有单独连接 FreeCAD MCP；请先启动 agent.freecad_experiment", 503)
    if session_id is None:
        session_id = session_name((await client.create_session("FreeCAD 文件操作实验"))["id"])
    else:
        session_id = session_name(session_id)
        status = (await client.session_status()).get(session_id, {})
        if status.get("type") not in {None, "idle"}:
            raise AgentError("该实验会话仍在运行，请等待任务完成或换用新会话", 409)
    print(f"会话：{session_id}", flush=True)
    messages = await client.messages(session_id)
    previous_ids = {item["info"]["id"] for item in messages}
    report: dict[str, Any] = {
        "session_id": session_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "prompt": prompt,
        "previous_message_ids": sorted(previous_ids),
        "messages": messages,
    }
    save_report(report, reports_dir)
    observed: dict[str, str] = {}
    result = 0
    try:
        async with asyncio.timeout(timeout):
            await client.prompt_async(session_id, prompt)
            while True:
                statuses = await client.session_status()
                messages = await client.messages(session_id)
                report["messages"] = messages
                print_tool_progress(messages, previous_ids, observed)
                last = terminal_assistant(messages, previous_ids)
                idle = statuses.get(session_id, {}).get("type") in {None, "idle"}
                if idle and last:
                    error = last["info"].get("error")
                    report["status"] = "error" if error else "completed"
                    result = 1 if error else 0
                    texts = [part["text"] for part in last.get("parts", []) if part.get("type") == "text"]
                    if texts:
                        print("\n" + "\n".join(texts), flush=True)
                    if error:
                        print(f"智能体返回错误：{error.get('name', 'unknown')}", flush=True)
                    break
                save_report(report, reports_dir)
                await asyncio.sleep(poll_interval)
    except (TimeoutError, asyncio.CancelledError):
        report["status"] = "timeout"
        result = 124
        try:
            report["abort_requested"] = await asyncio.wait_for(client.abort(session_id), timeout=15)
        except (AgentError, TimeoutError):
            report["abort_requested"] = False
        print("等待超时或已中断，已尝试停止智能体任务；FreeCAD 中已开始的操作仍需核对结果。", flush=True)
    except AgentError:
        report["status"] = "connection_error"
        raise
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        path = save_report(report, reports_dir)
        print(f"完整会话记录：{path}", flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("prompt", nargs="?", help="自然语言任务；与 --prompt-file 二选一")
    parser.add_argument("--prompt-file", type=Path, help="读取 UTF-8 文本文件作为任务")
    parser.add_argument("--session", help="延续已有实验会话；省略时创建新会话")
    parser.add_argument("--timeout", type=float, default=900, help="任务超时秒数，超时请求停止（默认 900）")
    parser.add_argument("--port", type=int, default=4097, help="独立实验服务端口（默认 4097）")
    args = parser.parse_args()
    if (args.prompt is None) == (args.prompt_file is None):
        parser.error("请提供自然语言任务或 --prompt-file，且只能选择一种")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    try:
        prompt = args.prompt if args.prompt is not None else args.prompt_file.read_text(encoding="utf-8")
        if not prompt.strip():
            raise ValueError("任务不能为空")
        if args.session:
            session_name(args.session)
        load_dotenv(ROOT / ".env", override=False)
        env = normalize_environment(dict(os.environ))
        normal_port = urlparse(env.get("AGENT_BASE_URL", "http://127.0.0.1:4096")).port or 4096
        if not 1024 <= args.port <= 65535 or args.port == normal_port:
            raise ValueError("请使用独立实验端口，不能连接网页当前使用的智能体端口")
        if not env.get("AGENT_SERVER_PASSWORD"):
            raise ValueError("缺少 AGENT_SERVER_PASSWORD；请检查项目 .env")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    async def run() -> int:
        async with AgentClient(
            base_url=f"http://127.0.0.1:{args.port}",
            username=env["AGENT_SERVER_USERNAME"],
            password=env["AGENT_SERVER_PASSWORD"],
            directory=str(WORKSPACE),
        ) as client:
            return await run_session(client, prompt, session_id=args.session, timeout=args.timeout)

    try:
        raise SystemExit(asyncio.run(run()))
    except AgentError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
