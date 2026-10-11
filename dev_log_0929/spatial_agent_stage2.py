"""One real backend task: raw DXF + the sole user prompt '请你进行场地布置'.

Run this client only after the experiment services are ready. It starts/stops no
service, injects no review seed, calls no CAD tools itself and publishes nothing.
The actual backend controls review, feedback continuation, artifact collection,
version import and opening. A standard original-drawing preview is allowed and
recorded; it comes from this upload, never an earlier generated layout.

An optional Uvicorn factory is exposed as ``backend_app`` so an isolated backend
can use CAD_STAGE2_AGENT_WORKSPACE without changing application code. Configure
CAD_DATA_DIR, AGENT_BASE_URL and FREECAD_RPC_PORT in its process environment.
The experiment client still has to be invoked separately and explicitly.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import time
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from dotenv import dotenv_values  # noqa: E402
from agent.client import AgentClient  # noqa: E402
from agent.runtime import WORKSPACE, normalize_environment  # noqa: E402
from agent.session import is_internal_message, is_internal_part  # noqa: E402
from dev_log_0929.spatial_agent_stage1 import checksum, sanitizer, save_json, visible_trace  # noqa: E402

PROMPT = "请你进行场地布置"
ACTIVE = {"preparing", "running", "collecting", "stopping"}


def source_fingerprints():
    """Hash repository sources only, never credential-bearing runtime config."""
    patterns = (
        "cad_domain/*.py", "cad_domain/*.yaml", "agent/*.py", "agent/config.json",
        "agent/prompts/*", "agent/plugins/*", "agent/skills/**/SKILL.md",
        "freecad/mcp/src/**/*.py", "freecad/mcp/addon/**/*.py",
        "backend/app/**/*.py", "dev_log_0929/spatial_agent_stage1.py",
        "dev_log_0929/spatial_agent_stage2.py", "dev_log_0929/spatial_stage2_services.py",
    )
    paths = sorted({path for pattern in patterns for path in ROOT.glob(pattern)
                    if path.is_file() and not path.is_symlink()})
    files = {path.relative_to(ROOT).as_posix(): checksum(path) for path in paths}
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"sha256": fingerprint, "files": files}


def backend_app():
    """Uvicorn factory only; calling the experiment CLI never starts a backend."""
    from backend.app.config import Settings
    from backend.app.main import create_app
    settings = Settings()
    workspace = os.environ.get("CAD_STAGE2_AGENT_WORKSPACE", str(WORKSPACE))
    return create_app(settings, agent_client=AgentClient(settings, directory=workspace))


def local_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password:
        raise ValueError("Use a local HTTP service URL without embedded credentials")
    return value.rstrip("/")


def database_row(data_dir, sql, values=()):
    """Evidence only: never create tables, import rows or change task status."""
    database = (data_dir / "app.sqlite").resolve()
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(sql, values).fetchone()
    return dict(row) if row else None


async def api(client, method, path, **kwargs):
    if not path.startswith("/api/") or path.startswith("//"):
        raise ValueError("Expected a local backend API path")
    response = await client.request(method, path, **kwargs)
    if response.is_error:
        try:
            detail = response.json().get("detail", "")
        except (ValueError, AttributeError):
            detail = ""
        raise RuntimeError(f"Backend {method} {path} returned HTTP {response.status_code}: {detail}")
    return response


async def wait_import(web, job_id):
    while True:
        job = (await api(web, "GET", f"/api/jobs/{job_id}")).json()
        if job["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            if job["status"] != "succeeded":
                raise RuntimeError(f"Original DXF import {job['status']}: {job.get('error', '')}")
            return job
        await asyncio.sleep(2)


async def capture_events(web, session_id, path, clean, ready, counts):
    with path.open("w", encoding="utf-8") as log:
        async with web.stream("GET", f"/api/sessions/{session_id}/events", timeout=None) as response:
            response.raise_for_status()
            ready.set()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                event = json.loads(line[5:])
                counts[event.get("type", "unknown")] += 1
                # Public text deltas are reconstructed by the messages endpoint.
                # Do not duplicate every token or persist internal engine events.
                if event.get("type") == "message.part.delta":
                    continue
                log.write(json.dumps(clean(event), ensure_ascii=False) + "\n")
                log.flush()


async def run(args):
    source = args.source.expanduser().resolve(strict=True)
    if source.suffix.lower() != ".dxf" or not source.is_file():
        raise ValueError("--source must identify the original DXF")
    data_dir = args.data_dir.expanduser().resolve(strict=True)
    if not (data_dir / "app.sqlite").is_file():
        raise ValueError("Start the real isolated backend first; --data-dir must contain its app.sqlite")
    env = normalize_environment({**{k: v for k, v in dotenv_values(ROOT / ".env").items() if v is not None}, **os.environ})
    clean = sanitizer(env)
    workspace = str(args.agent_workspace.expanduser().resolve()) if args.agent_workspace else str(WORKSPACE)
    before = checksum(source)
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    runtime = args.runtime.expanduser().resolve() / stamp
    runtime.mkdir(parents=True, exist_ok=False)
    result = {"experiment": "real_backend_site_layout", "request": PROMPT, "source": str(source),
              "source_sha256": before, "runtime": str(runtime), "backend_data_dir": str(data_dir),
              "status": "starting", "services_owned_by_harness": False,
              "browser_url": local_url(args.backend_url) + "/",
              "prior_review_supplied": False, "prior_layout_supplied": False,
              "browser_ui_observed": False, "backend_review_bypassed": False}
    result["source_fingerprints_start"] = source_fingerprints()
    web = httpx.AsyncClient(base_url=local_url(args.backend_url), trust_env=False, timeout=45)
    agent = AgentClient(base_url=local_url(args.agent_url), username=env["AGENT_SERVER_USERNAME"],
                        password=env.get("AGENT_SERVER_PASSWORD", ""), directory=workspace)
    started = time.monotonic()
    session_id = task_id = task_root = event_task = None
    task_state, trace, messages, visible = {}, [], [], []
    event_counts = Counter()
    active_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, active_task.cancel)

    async def snapshot():
        nonlocal messages, trace, visible
        messages = await agent.messages(session_id)
        trace = visible_trace(messages, clean)
        visible = (await api(web, "GET", f"/api/sessions/{session_id}/messages")).json()
        save_json(runtime / "public-trace.json", trace)
        save_json(runtime / "web-messages.json", clean(visible))
        if task_root and (task_root / "scratch").is_dir():
            save_json(task_root / "scratch/experiment-visible-trace.json", trace)

    try:
        async with asyncio.timeout(args.timeout):
            health = (await api(web, "GET", "/api/health")).json()
            result["backend_health"] = health
            if health.get("agent") != "connected":
                raise RuntimeError("The backend is not connected to its configured agent/MCP")
            project = (await api(web, "POST", "/api/projects", json={"name": "真实场布实验 " + stamp})).json()
            result["project_id"] = project["id"]
            result["project_name"] = project["name"]
            if not database_row(data_dir, "SELECT id FROM projects WHERE id=?", (project["id"],)):
                raise RuntimeError("--data-dir does not belong to the selected backend; no model request was sent")
            with source.open("rb") as source_file:
                upload = (await api(web, "POST", f"/api/projects/{project['id']}/drawings",
                                    files={"file": ("drawing.dxf", source_file, "application/dxf")})).json()
            result["original_drawing_id"], result["import_job_id"] = upload["drawing_id"], upload["job_id"]
            await wait_import(web, upload["job_id"])
            drawing = (await api(web, "GET", f"/api/drawings/{upload['drawing_id']}")).json()
            revision = drawing["current_revision"]
            imported = data_dir / "drawings" / drawing["id"] / "revisions" / revision / "drawing.dxf"
            if checksum(imported) != before:
                raise RuntimeError("Imported DXF differs from raw input; refusing a contaminated trial")
            session_id = (await api(web, "POST", f"/api/drawings/{drawing['id']}/sessions")).json()["id"]
            result["session_id"] = session_id
            if await agent.messages(session_id):
                raise RuntimeError("Backend session is not fresh; refusing to reuse a conversation")
            ready = asyncio.Event()
            event_task = asyncio.create_task(capture_events(web, session_id, runtime / "public-events.jsonl", clean, ready, event_counts))
            await asyncio.wait_for(ready.wait(), 10)
            sent = (await api(web, "POST", f"/api/sessions/{session_id}/messages",
                              json={"text": PROMPT, "expected_revision": revision})).json()
            task_id = sent["task_id"]
            task_root = data_dir / "agent-tasks" / task_id
            result["task_id"] = task_id
            context = json.loads((task_root / "context.json").read_text(encoding="utf-8"))
            if context.get("previous_artifacts") or len(context["input_files"]) != 1 or context["input_files"][0]["sha256"] != before:
                raise RuntimeError("Unexpected prior artifacts or additional CAD inputs in the backend task")
            result["input_context"] = {"cad_input_count": len(context["input_files"]),
                                       "previous_artifacts": [], "input_sha256": before,
                                       "original_preview": {key: context.get("preview", {}).get(key)
                                                            for key in ("available", "sha256", "drawing_id", "revision_id")}}
            result["status"] = "running"
            save_json(runtime / "result.json", clean(result))
            print(json.dumps({key: result[key] for key in ("status", "runtime", "project_id", "session_id", "task_id")}, ensure_ascii=False), flush=True)
            last_snapshot = last_progress = 0.0
            while True:
                artifact_state = (await api(web, "GET", f"/api/sessions/{session_id}/artifacts")).json()
                task_state = artifact_state.get("task") or {}
                if task_state.get("id") != task_id:
                    raise RuntimeError("A different task replaced the experiment's current task")
                elapsed = time.monotonic() - started
                if elapsed - last_snapshot >= 10 or task_state.get("status") not in ACTIVE:
                    await snapshot()
                    last_snapshot = elapsed
                if elapsed - last_progress >= 30:
                    tools = [part for message in trace for part in message.get("parts", []) if part["type"] == "tool"]
                    print(json.dumps({"elapsed_seconds": round(elapsed, 1), "backend_task": task_state.get("status"),
                                      "tool_calls": len(tools), "latest_tools": [p["tool"] for p in tools[-3:]]}, ensure_ascii=False), flush=True)
                    last_progress = elapsed
                if task_state.get("status") not in ACTIVE:
                    break
                await asyncio.sleep(3)
            result["backend_task"] = task_state
            if task_state.get("status") != "completed":
                result["status"] = "backend_task_not_completed"
            else:
                # Collection/import is exclusively performed by AgentTasks. This
                # client only waits, downloads and checks the published results.
                while True:
                    artifact_state = (await api(web, "GET", f"/api/sessions/{session_id}/artifacts")).json()
                    artifacts = [a for a in artifact_state["artifacts"] if a["task_id"] == task_id]
                    if not any(a.get("preview_status") in {"queued", "running"} for a in artifacts):
                        break
                    await asyncio.sleep(3)
                downloads = runtime / "downloads"
                downloads.mkdir()
                published = []
                for artifact in artifacts:
                    response = await api(web, "GET", artifact["download_url"])
                    filename = artifact["id"] + "_" + Path(artifact["name"]).name
                    target = downloads / filename
                    target.write_bytes(response.content)
                    stored = database_row(data_dir, "SELECT sha256 FROM artifacts WHERE id=? AND task_id=?", (artifact["id"], task_id))
                    digest = hashlib.sha256(response.content).hexdigest()
                    item = {**artifact, "download_sha256": digest, "download_matches_published": bool(stored and stored["sha256"] == digest),
                            "download_path": str(target), "download_http_url": local_url(args.backend_url) + artifact["download_url"]}
                    if not item["download_matches_published"]:
                        raise RuntimeError("Downloaded artifact differs from the backend's published hash")
                    if artifact.get("drawing_id") and artifact.get("preview_status") == "succeeded":
                        generated = (await api(web, "GET", f"/api/drawings/{artifact['drawing_id']}")).json()
                        generated_revision = generated["current_revision"]
                        preview_path = f"/api/drawings/{generated['id']}/revisions/{generated_revision}/preview"
                        previews = {}
                        for fmt in ("svg", "png"):
                            preview = await api(web, "GET", preview_path, params={"format": fmt})
                            (downloads / (artifact["id"] + "." + fmt)).write_bytes(preview.content)
                            previews[fmt] = {"url": local_url(args.backend_url) + preview_path + "?format=" + fmt,
                                             "bytes": len(preview.content), "content_type": preview.headers.get("content-type"),
                                             "sha256": hashlib.sha256(preview.content).hexdigest()}
                        item.update(revision_id=generated_revision, previews=previews)
                    published.append(item)
                result["artifacts"] = published
                workbench = (await api(web, "GET", f"/api/projects/{project['id']}/workbench")).json()
                result["versions"] = workbench["versions"]
                ready_cad = [a for a in published if a.get("drawing_id") and a.get("preview_status") == "succeeded"
                             and a["name"].lower().endswith(".dxf") and a["download_sha256"] != before]
                if ready_cad:
                    newest = ready_cad[-1]
                    opened = (await api(web, "POST", f"/api/artifacts/{newest['id']}/open")).json()
                    result["open_result"] = opened
                    result["selected_artifact_id"] = newest["id"]
                    result["selected_drawing_id"] = newest["drawing_id"]
                    result["selected_preview_urls"] = {fmt: info["url"] for fmt, info in newest["previews"].items()}
                    if opened.get("drawing_id") != newest["drawing_id"] or opened.get("session_id") != session_id:
                        raise RuntimeError("Open-and-continue did not bind the published drawing")
                changed_dxf = [a for a in published if a["name"].lower().endswith(".dxf") and a["download_sha256"] != before]
                result["status"] = "published" if changed_dxf and ready_cad else "completed_without_openable_modified_dxf"
            await snapshot()
    except asyncio.CancelledError:
        result["status"] = "interrupted"
    except TimeoutError:
        result["status"] = "timeout"
    except Exception as exc:
        result["status"], result["error"] = "error", clean(f"{type(exc).__name__}: {exc}")
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: None)
        if session_id:
            try:
                state = (await api(web, "GET", f"/api/sessions/{session_id}/artifacts")).json().get("task") or {}
                if state.get("id") == task_id and state.get("status") in ACTIVE:
                    await asyncio.wait_for(api(web, "POST", f"/api/sessions/{session_id}/abort"), 10)
                    result["abort_requested_for_own_session"] = True
                    for _ in range(10):
                        await asyncio.sleep(2)
                        state = (await asyncio.wait_for(api(web, "GET", f"/api/sessions/{session_id}/artifacts"), 5)).json().get("task") or {}
                        if state.get("status") not in ACTIVE:
                            break
                result["backend_task"] = state
                await asyncio.wait_for(snapshot(), 15)
            except Exception as exc:
                result["cleanup_note"] = type(exc).__name__ + ": could not fully confirm this task's terminal state"
        if event_task:
            event_task.cancel()
            await asyncio.gather(event_task, return_exceptions=True)
        await agent.close()
        await web.aclose()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        tools = [part for message in trace for part in message.get("parts", []) if part["type"] == "tool"]
        result["tool_counts"] = dict(Counter(p["tool"] for p in tools))
        result["skill_loads"] = [{"name": p.get("input", {}).get("name"), "status": p.get("status"),
                                  "tool_part_id": p.get("id"), "evidence": p.get("output", "")[:400]}
                                 for p in tools if p.get("tool") == "skill"]
        result["skill_loaded"] = any(item["status"] == "completed" for item in result["skill_loads"])
        result["observed_models"] = sorted({f"{m.get('info', {}).get('providerID', '')}/{m.get('info', {}).get('modelID', '')}"
                                            for m in messages if m.get("info", {}).get("role") == "assistant"})
        result["public_user_messages"] = ["\n".join(p.get("text", "") for p in m.get("parts", [])
                                                  if p.get("type") == "text" and not is_internal_part(p))
                                          for m in messages if m.get("info", {}).get("role") == "user" and not is_internal_message(m)]
        # Framework recovery may be a synthetic text plus an ordinary preview
        # attachment: it is not classified as a wholly internal message, but
        # contributes no public user text. Keep raw evidence; do not count the
        # resulting empty string as a second human instruction.
        result["nonempty_public_user_messages"] = [text for text in result["public_user_messages"] if text.strip()]
        result["automated_recovery_message_count"] = sum(
            any(part.get("synthetic") and part.get("metadata", {}).get("cad_task_recovery")
                for part in message.get("parts", []))
            for message in messages if message.get("info", {}).get("role") == "user")
        result["sole_user_prompt_preserved"] = result["nonempty_public_user_messages"] == [PROMPT]
        result["event_counts"] = dict(event_counts)
        result["source_unchanged"] = source.is_file() and checksum(source) == before
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        result["services_stopped_by_harness"] = False
        result["source_fingerprints_end"] = source_fingerprints()
        beginning = result["source_fingerprints_start"]["files"]
        ending = result["source_fingerprints_end"]["files"]
        result["source_files_changed_during_run"] = [name for name in sorted(beginning.keys() | ending.keys())
                                                    if beginning.get(name) != ending.get(name)]
        result["code_unchanged_during_run"] = not result["source_files_changed_during_run"]
        if task_root:
            for filename in ("cad-review.json", "delivery-review.json", "review-attempts.json"):
                path = task_root / "scratch" / filename
                if path.is_file():
                    data = json.loads(path.read_text(encoding="utf-8"))
                    save_json(runtime / filename, clean(data))
                    if filename == "delivery-review.json":
                        result["backend_delivery_review_status"] = data.get("status")
                    if filename == "review-attempts.json":
                        result["backend_feedback_rounds"] = len(data)
            save_json(task_root / "scratch/experiment-stage2-result.json", clean(result))
        save_json(runtime / "result.json", clean(result))
        print(json.dumps({key: result.get(key) for key in ("status", "runtime", "elapsed_seconds", "skill_loaded",
                                                         "tool_counts", "source_unchanged", "backend_delivery_review_status")}, ensure_ascii=False), flush=True)
    return 0 if (result["status"] == "published" and result["source_unchanged"]
                 and result["sole_user_prompt_preserved"] and result["skill_loaded"]
                 and result["code_unchanged_during_run"]) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    parser.add_argument("--agent-url", default="http://127.0.0.1:4096")
    parser.add_argument("--agent-workspace", type=Path)
    parser.add_argument("--data-dir", type=Path, required=True, help="Data directory of the externally started isolated backend")
    parser.add_argument("--runtime", type=Path, default=ROOT / ".runtime/spatial-stage2-client")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    try:
        code = asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps({"status": "setup_error", "error_type": type(exc).__name__}), flush=True)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
