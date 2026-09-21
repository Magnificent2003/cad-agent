"""Isolated ODA conversion. The original is never repaired or overwritten."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading

from .errors import CadError


def convert(source: Path, target: Path, settings: dict) -> dict:
    extension = target.suffix.lower().lstrip(".")
    if extension not in {"dxf", "dwg"}:
        raise CadError("Only DXF and DWG conversion is supported.", "unsupported_format")
    executable = settings.get("oda_executable") or os.environ.get("ODA_EXECUTABLE")
    if not executable:
        raise CadError("DWG conversion requires ODA_EXECUTABLE to be configured.", "oda_unavailable")
    executable = Path(executable).expanduser()
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise CadError("ODA_EXECUTABLE must identify an executable absolute path.", "oda_unavailable")
    timeout = float(settings.get("conversion_timeout", 120))
    if not 1 <= timeout <= 3600:
        raise CadError("Conversion timeout must be between 1 and 3600 seconds.", "invalid_settings")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="oda-", dir=target.parent) as temporary:
        work = Path(temporary)
        input_dir, output_dir = work / "input", work / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        shutil.copy2(source, input_dir / ("drawing" + source.suffix.lower()))
        command = [str(executable), str(input_dir), str(output_dir), "ACAD2018", extension.upper(), "0", "0", "*" + source.suffix.lower()]
        if settings.get("oda_use_xvfb"):
            xvfb = shutil.which("xvfb-run")
            if not xvfb:
                raise CadError("xvfb-run is required by ODA_USE_XVFB.", "oda_unavailable")
            command = [xvfb, "-a", *command]
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            previous_sigterm = None
            def terminate_converter(signum, frame):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise SystemExit(128 + signum)
            if threading.current_thread() is threading.main_thread():
                previous_sigterm = signal.signal(signal.SIGTERM, terminate_converter)
            try:
                process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise CadError("ODA conversion timed out; the original was preserved.", "conversion_timeout") from None
            finally:
                if previous_sigterm is not None:
                    signal.signal(signal.SIGTERM, previous_sigterm)
        except OSError as exc:
            raise CadError("ODA could not be started.", "oda_unavailable") from exc
        if process.returncode:
            raise CadError(f"ODA conversion failed (exit code {process.returncode}).", "conversion_failed")
        outputs = [path for path in output_dir.iterdir() if path.suffix.lower() == "." + extension]
        if len(outputs) != 1 or outputs[0].stat().st_size == 0:
            raise CadError("ODA did not produce the expected drawing.", "conversion_failed")
        shutil.copy2(outputs[0], target)
    return {"converter": "ODA", "output_version": "ACAD2018", "audit_repair": False, "source_format": source.suffix.lower(), "target_format": "." + extension}


convert_file = convert
