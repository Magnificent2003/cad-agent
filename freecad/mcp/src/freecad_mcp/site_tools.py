"""JSON-only bridge to the project's CAD worker; no FreeCAD GUI dependency.

The worker, not model-supplied tool parameters, owns task binding and file scope
validation. This module only transports its report and optional PNG feedback.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from mcp.types import ImageContent, TextContent


WORKER_TIMEOUT_SECONDS = 120
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 4096
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _text(report: dict[str, Any]) -> TextContent:
    return TextContent(type="text", text=json.dumps(report, ensure_ascii=False, allow_nan=False))


def _failure(code: str, message: str, **details: Any) -> list[TextContent]:
    return [_text({"success": False, "error": {"code": code, "message": message, **details}})]


def _stderr_tail(stream) -> str:
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(max(0, size - MAX_STDERR_BYTES))
    value = stream.read(MAX_STDERR_BYTES).decode("utf-8", errors="replace").strip()
    return ("[truncated] " if size > MAX_STDERR_BYTES else "") + value


def _preview_contents(context_path: str, report: dict[str, Any], preview: Any) -> list[TextContent | ImageContent]:
    if not preview:
        return [_text(report)]
    try:
        if not isinstance(preview, str):
            raise ValueError("Worker preview path must be a string.")
        path = Path(preview)
        if not path.is_absolute() or path.suffix.lower() != ".png" or not path.is_file():
            raise ValueError("Worker preview must name an existing absolute PNG file.")
        # Read at most the limit plus one byte, including when the file changes
        # between the existence check and read.
        with path.open("rb") as source:
            data = source.read(MAX_PREVIEW_BYTES + 1)
        if len(data) > MAX_PREVIEW_BYTES:
            raise ValueError(f"Preview exceeds the {MAX_PREVIEW_BYTES // (1024 * 1024)} MiB limit.")
        if not data.startswith(PNG_SIGNATURE):
            raise ValueError("Preview does not contain PNG data.")
        metadata = report.get("preview")
        if not isinstance(metadata, dict) or metadata.get("path") != preview:
            raise ValueError("Preview is missing its canonical file binding.")
        if not report.get("path") or not report.get("file_sha256") or metadata.get("drawing_sha256") != report["file_sha256"]:
            raise ValueError("Preview and report refer to different drawing snapshots.")
        if metadata.get("image_sha256") != hashlib.sha256(data).hexdigest():
            raise ValueError("Preview bytes changed after rendering; request a fresh review.")
    except (OSError, ValueError) as exc:
        report = {**report, "preview_delivery": {"status": "unavailable", "reason": str(exc)}}
        return [_text(report)]
    content = ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mimeType="image/png")
    # The image is now held in the outgoing response. Let the worker recheck the
    # actual DXF and fully validate this exact PNG before recording feedback.
    acknowledgment = run_site_tool("feedback", context_path, {
        "path": report["path"], "file_sha256": report["file_sha256"], "preview": metadata,
    })
    ack = json.loads(acknowledgment[0].text)
    if ack.get("status") != "passed" or ack.get("success") is False:
        report = {**report, "preview_delivery": {
            "status": "unavailable", "reason": "Image feedback could not be confirmed; request a fresh review.",
            "acknowledgment": ack,
        }}
        return [_text(report)]
    return [_text(report), content]


def run_site_tool(action: str, context_path: str, params: dict[str, Any]) -> list[TextContent | ImageContent]:
    """Send data to a bounded worker process; never execute a shell command."""
    configured_root = os.environ.get("CAD_PROJECT_ROOT", "")
    root = Path(configured_root)
    if not configured_root or not root.is_absolute() or not root.is_dir():
        return _failure("worker_unavailable", "CAD_PROJECT_ROOT must name the project directory; start MCP with freecad/mcp.sh.")
    python = root / ".venv/bin/python"
    if not python.is_file():
        return _failure("worker_unavailable", "Project .venv/bin/python is missing. Prepare the project Python environment and restart MCP.")
    try:
        payload = json.dumps({"action": action, "context_path": context_path, "params": params},
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        return _failure("invalid_parameters", f"Tool arguments must contain finite JSON data: {exc}")
    # Spool process output to temporary files so a verbose worker does not fill
    # MCP memory. Only the bounded JSON report and stderr tail are ever read.
    try:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            try:
                process = subprocess.run(
                    [str(python), "-m", "cad_domain.worker"], cwd=str(root), input=payload,
                    stdout=stdout, stderr=stderr, timeout=WORKER_TIMEOUT_SECONDS, check=False,
                )
            except subprocess.TimeoutExpired:
                return _failure(
                    "worker_timeout",
                    f"CAD worker exceeded {WORKER_TIMEOUT_SECONDS}s and was stopped. Check current outputs before retrying with a smaller scope.",
                    stderr=_stderr_tail(stderr),
                )
            stderr_text = _stderr_tail(stderr)
            stdout.seek(0)
            raw = stdout.read(MAX_REPORT_BYTES + 1)
    except OSError as exc:
        return _failure("worker_unavailable", f"Could not start or read the CAD worker: {exc}")
    if len(raw) > MAX_REPORT_BYTES:
        return _failure("invalid_worker_response", "Worker report exceeded the size limit. Retry with fewer selected handles.", stderr=stderr_text)
    try:
        # JSON constants accepted by Python's parser are not valid MCP JSON.
        response = json.loads(raw)
        if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
            raise ValueError("Expected an object containing a result object.")
        json.dumps(response, allow_nan=False)
    except (ValueError, TypeError) as exc:
        return _failure("invalid_worker_response", f"CAD worker did not return the expected JSON report: {exc}",
                        worker_exit_code=process.returncode, stderr=stderr_text)
    if process.returncode != 0:
        return _failure("worker_failed", "CAD worker exited unsuccessfully. Resolve the reported problem before retrying.",
                        worker_exit_code=process.returncode, worker_result=response["result"], stderr=stderr_text)
    if action == "feedback":
        return [_text(response["result"])]
    return _preview_contents(context_path, response["result"], response.get("preview"))
