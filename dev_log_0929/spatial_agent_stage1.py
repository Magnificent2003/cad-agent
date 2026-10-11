"""Run one fresh, isolated real-model/MCP analysis trial on a supplied DXF.

This is an agent-to-MCP experiment, not a test of browser artifact publication.
It does not start FreeCAD, the business backend or the frontend. Nothing from an
old review, generated drawing, session or skill is supplied to the model. Run
only when an actual model call is intended; there is no synthetic success path.

Example:
    .venv/bin/python dev_log_0929/spatial_agent_stage1.py --source INPUT.dxf

The default request is analysis-only. A different user request can be supplied
with --prompt-file. The current project agent config, prompt, task plugin and
MCP command are reused unchanged. Runtime state is isolated under a unique run
directory; only the process group created by this script is stopped on exit.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values  # noqa: E402
from agent.client import AgentClient  # noqa: E402
from agent.provider import required_credentials
from agent.launcher import build_config  # noqa: E402
from agent.network import prepare_model_network  # noqa: E402
from agent.runtime import engine_command, engine_environment, normalize_environment  # noqa: E402
from agent.session import is_internal_message, is_internal_part, terminal_assistant  # noqa: E402

DEFAULT_PROMPT = (
    "请识别当前图纸场界范围内的建筑、候选占地和待定区域，"
    "并给出施工布置的参照轴及避让约束。本轮只分析，不修改图纸。"
)
MAX_TEXT_CHARS = 120000


def checksum(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def free_port():
    # Do not reclaim occupied ports. The child must bind this still-free port;
    # a race is a startup failure, never a reason to stop another process.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def sanitizer(env):
    secrets = [str(value) for key, value in env.items() if value and len(str(value)) >= 4
               and any(word in key.upper() for word in ("PASSWORD", "TOKEN", "SECRET", "API_KEY"))]
    secrets.sort(key=len, reverse=True)

    def clean(value):
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\s]+", "[IMAGE DATA OMITTED]", value)
            if len(value) > MAX_TEXT_CHARS:
                value = value[:MAX_TEXT_CHARS] + f"\n[TRUNCATED: original text {len(value)} characters]"
            return value
        if isinstance(value, dict):
            return {str(key): clean(item) for key, item in value.items()
                    if str(key).lower() not in {"reasoning", "reasoning_content", "system", "api_key", "apikey", "password", "authorization"}}
        if isinstance(value, (tuple, list)):
            return [clean(item) for item in value]
        return value

    return clean


def visible_trace(messages, clean):
    """Only public text and executed tool data; never serialize raw messages."""
    result = []
    for message in messages:
        if is_internal_message(message):
            continue
        info = message.get("info", {})
        if info.get("role") != "assistant":
            continue
        item = {key: info[key] for key in ("id", "role", "modelID", "providerID", "finish", "time") if key in info}
        if info.get("error"):
            item["error"] = clean(info["error"])
        item["parts"] = []
        for part in message.get("parts", []):
            if is_internal_part(part):
                continue
            if part.get("type") == "text" and part.get("text", "").strip():
                item["parts"].append({"type": "text", "text": clean(part["text"])})
            elif part.get("type") == "tool":
                state = part.get("state", {})
                record = {"type": "tool", "id": part.get("id"), "tool": part.get("tool"),
                          "status": state.get("status")}
                for field in ("input", "output", "error", "title", "time"):
                    if field in state:
                        record[field] = clean(state[field])
                timing = state.get("time") or {}
                if timing.get("start") is not None and timing.get("end") is not None:
                    record["elapsed_seconds"] = max(0, (timing["end"] - timing["start"]) / 1000)
                item["parts"].append(record)
        result.append(clean(item))
    return result


async def stop_owned_process(process):
    if process is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=8)
    except TimeoutError:
        pass
    # The leader can exit while MCP descendants remain in its group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    await asyncio.to_thread(process.wait)


async def run(args):
    source = args.source.expanduser().resolve(strict=True)
    if source.suffix.lower() != ".dxf" or not source.is_file():
        raise ValueError("--source must be an actual DXF file")
    prompt = args.prompt_file.read_text(encoding="utf-8").strip() if args.prompt_file else DEFAULT_PROMPT
    if not prompt:
        raise ValueError("The user request is empty")
    # Load existing credentials only to invoke the authorized model. Never dump
    # this mapping, generated engine config or HTTP authentication headers.
    env = normalize_environment({**{k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}, **os.environ})
    missing = required_credentials(env)
    if missing:
        raise ValueError("Missing required settings: " + ", ".join(missing))
    clean = sanitizer(env)
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    runtime = args.runtime.expanduser().resolve() / stamp
    runtime.mkdir(parents=True, exist_ok=False)
    files_root, engine_root = runtime / "files", runtime / "engine"
    workspace = engine_root / "workspace"
    workspace.mkdir(parents=True)
    task_id = uuid.uuid4().hex
    task_root = files_root / "agent-tasks" / task_id
    for name in ("input", "scratch", "output"):
        (task_root / name).mkdir(parents=True)
    input_file = task_root / "input/drawing.dxf"
    before = checksum(source)
    input_file.write_bytes(source.read_bytes())
    input_file.chmod(0o444)
    if checksum(input_file) != before or checksum(source) != before:
        raise ValueError("The source changed while preparing the isolated input")
    env["CAD_DATA_DIR"] = str(files_root)
    port = free_port()
    env["AGENT_BASE_URL"] = f"http://127.0.0.1:{port}"
    result = {"experiment": "fresh_real_agent_to_mcp", "runtime": str(runtime), "task_id": task_id,
              "source": str(source), "source_sha256": before, "input_copy": str(input_file),
              "request": clean(prompt), "status": "starting", "backend_publication_tested": False,
              "prior_review_supplied": False, "prior_session_supplied": False,
              "trace": str(task_root / "scratch/agent-visible-trace.json"),
              "implementation_sha256": {name: checksum(ROOT / name) for name in (
                  "cad_domain/spatial_autonomy.py", "cad_domain/spatial_constraints.py",
                  "cad_domain/spatial_tools.py", "cad_domain/spatial_extract.py", "cad_domain/spatial_facts.py",
                  "freecad/mcp/src/freecad_mcp/server.py", "agent/plugins/task-context.mjs",
                  "agent/prompts/cad.txt", "dev_log_0929/spatial_agent_stage1.py")}}
    started = time.monotonic()
    process, client, log_handle, session_id, binding = None, None, None, None, None
    messages, last_trace = [], []
    active_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, active_task.cancel)
    try:
        # The same startup route selection as the project: one authenticated
        # non-generating models request per candidate route, not a mock call.
        env, _network_note = await asyncio.to_thread(prepare_model_network, env)
        config = build_config(env)
        result["configured_model"] = config.get("model")
        child_env = engine_environment(env, config, runtime=engine_root)
        command = [*engine_command(env), "serve", "--hostname", "127.0.0.1", "--port", str(port)]
        log_handle = (runtime / "server.log").open("w", encoding="utf-8")
        process = subprocess.Popen(command, cwd=workspace, env=child_env, stdout=log_handle,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        client = AgentClient(base_url=env["AGENT_BASE_URL"], username=env["AGENT_SERVER_USERNAME"],
                             password=env["AGENT_SERVER_PASSWORD"], directory=str(workspace))
        deadline = time.monotonic() + min(args.timeout, 90)
        while True:
            if process.poll() is not None:
                raise RuntimeError("The isolated agent exited during startup; inspect its local server.log")
            try:
                await asyncio.wait_for(client.health(), timeout=3)
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise TimeoutError("The isolated agent did not become ready") from None
                await asyncio.sleep(.5)
        # MCP discovery is not a model call. Fail visibly if the configured MCP
        # did not connect instead of silently switching to another tool chain.
        mcp = await client.mcp_status()
        result["mcp_status"] = clean(mcp)
        if set(mcp) != {"freecad"} or mcp["freecad"].get("status") != "connected":
            raise RuntimeError("The configured project MCP did not connect")
        session = await client.create_session("空间事实与约束 · 独立真实调用")
        session_id = session["id"]
        result["session_id"] = session_id
        bindings = files_root / "agent-contexts"
        bindings.mkdir(parents=True)
        binding = bindings / f"{session_id}.json"
        context = {"task_id": task_id, "session_id": session_id, "drawing_id": "isolated_" + task_id,
                   "drawing_name": "drawing.dxf", "revision_id": "input",
                   "input_files": [{"path": str(input_file), "sha256": before, "format": "dxf"}],
                   "output_directory": str(task_root / "output"), "scratch_directory": str(task_root / "scratch"),
                   "previous_artifacts": [], "request": prompt,
                   "preview": {"available": False, "reason": "Fresh CAD-only analysis experiment"}}
        save_json(binding, context)
        save_json(task_root / "context.json", context)
        save_json(runtime / "result.json", result)
        print(json.dumps({"status": "ready", "runtime": str(runtime), "session_id": session_id,
                          "task_id": task_id, "model": result["configured_model"]}, ensure_ascii=False), flush=True)
        # No custom system prompt and no forced tool call. The normal task plugin
        # binds the context and the model decides which available tools to use.
        async with asyncio.timeout(max(1, args.timeout - (time.monotonic() - started))):
            await client.prompt_async(session_id, prompt)
            result["status"] = "running"
            last_progress = 0.0
            while True:
                if process.poll() is not None:
                    raise RuntimeError("The isolated agent exited during the model task")
                messages = await client.messages(session_id)
                last_trace = visible_trace(messages, clean)
                state = (await client.session_status()).get(session_id, {}).get("type", "idle")
                elapsed = time.monotonic() - started
                if elapsed - last_progress >= 30:
                    tools = [part for message in last_trace for part in message["parts"] if part["type"] == "tool"]
                    print(json.dumps({"elapsed_seconds": round(elapsed, 1), "status": state,
                                      "tool_calls": len(tools), "latest_tools": [tool["tool"] for tool in tools[-3:]]}, ensure_ascii=False), flush=True)
                    save_json(task_root / "scratch/agent-visible-trace.json", last_trace)
                    last_progress = elapsed
                terminal = terminal_assistant(messages, set())
                if state in {None, "idle"} and terminal:
                    info = terminal["info"]
                    result["finish"] = info.get("finish")
                    if info.get("error"):
                        result["status"], result["error"] = "model_error", clean(info["error"])
                    elif info.get("finish") == "stop":
                        result["status"] = "completed"
                        result["final_answer"] = clean("\n".join(p.get("text", "") for p in terminal.get("parts", [])
                                                               if p.get("type") == "text" and not is_internal_part(p)))
                        if not result["final_answer"].strip():
                            result["status"] = "incomplete_reply"
                    else:
                        result["status"] = "incomplete_reply"
                    break
                await asyncio.sleep(2)
    except asyncio.CancelledError:
        result["status"] = "interrupted"
    except TimeoutError:
        result["status"] = "timeout"
        result["error"] = "The finite experiment time budget was exceeded"
    except Exception as exc:
        result["status"] = "error"
        result["error"] = clean(f"{type(exc).__name__}: {exc}")
    finally:
        # A second terminal interrupt must not abandon owned child processes.
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: None)
        if client and session_id:
            try:
                state = (await asyncio.wait_for(client.session_status(), 5)).get(session_id, {}).get("type", "idle")
                if state not in {None, "idle"}:
                    await asyncio.wait_for(client.abort(session_id), 5)
            except Exception:
                result["abort_note"] = "Could not confirm model idle before stopping the owned process group"
        await stop_owned_process(process)
        if client:
            await client.close()
        if log_handle:
            log_handle.close()
        if binding:
            binding.unlink(missing_ok=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["source_unchanged"] = source.is_file() and checksum(source) == before
        result["input_copy_unchanged"] = input_file.is_file() and checksum(input_file) == before
        result["owned_process_stopped"] = process is None or process.poll() is not None
        tools = [part for message in last_trace for part in message.get("parts", []) if part["type"] == "tool"]
        result["tool_counts"] = dict(Counter(tool["tool"] for tool in tools))
        result["model_action_rounds"] = sum(message.get("finish") == "tool-calls" for message in last_trace)
        result["tool_content_failures"] = [{"tool": tool["tool"], "message": tool.get("output", "")[:1000]}
                                            for tool in tools if tool.get("output", "").startswith("Failed to ")]
        result["tool_errors"] = [{"tool": tool["tool"], "error": tool.get("error")} for tool in tools if tool["status"] == "error"]
        result["observed_models"] = sorted({f"{m.get('info', {}).get('providerID', '')}/{m.get('info', {}).get('modelID', '')}"
                                            for m in messages if m.get("info", {}).get("role") == "assistant"})
        result["output_files"] = [str(p.relative_to(task_root / "output")) for p in (task_root / "output").rglob("*") if p.is_file()]
        result["public_trace_only"] = True
        save_json(task_root / "scratch/agent-visible-trace.json", last_trace)
        save_json(task_root / "scratch/agent-stage1-result.json", clean(result))
        save_json(runtime / "result.json", clean(result))
        print(json.dumps({key: result.get(key) for key in ("status", "runtime", "elapsed_seconds", "configured_model",
                                                         "tool_counts", "source_unchanged", "owned_process_stopped")}, ensure_ascii=False), flush=True)
    return 0 if result["status"] == "completed" and result["source_unchanged"] and result["input_copy_unchanged"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Original DXF, never a prior generated layout")
    parser.add_argument("--prompt-file", type=Path, help="UTF-8 user request; otherwise the fresh analysis request is used")
    parser.add_argument("--runtime", type=Path, default=ROOT / ".runtime/spatial-stage1")
    parser.add_argument("--timeout", type=int, default=1000, help="Finite total experiment budget in seconds")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        code = asyncio.run(run(args))
    except Exception as exc:
        # Pre-invocation failures should not echo credentials from arbitrary errors.
        print(json.dumps({"status": "setup_error", "error_type": type(exc).__name__}), flush=True)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
