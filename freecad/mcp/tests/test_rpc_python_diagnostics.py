"""Exercise actual execution/capture/response code with only FreeCAD/Qt stubbed."""
import json
import sys
import threading
import time
from unittest.mock import MagicMock

from freecad_mcp.operations.core import execute_code_operation, get_async_status_operation

pytest_plugins = ("test_rpc_handlers",)


def completed_job(rpc, job_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = rpc.get_async_status(job_id)["job"]
        if job["state"] != "running":
            return job
        time.sleep(0.005)
    raise AssertionError("background test script did not finish")


def test_failure_keeps_partial_stdout_stderr_and_user_script_line(rpc_module):
    rpc = rpc_module.FreeCADRPC()
    source = "import sys\nprint('before failure')\nprint('warning first', file=sys.stderr)\nraise TypeError('an integer is required')"
    wrapper = "value = 1\nexec(compile(" + repr(source) + ", '<cad-task:test-call>', 'exec'), globals())"
    result = rpc.execute_code(wrapper)
    assert result["success"] is False
    assert result["stdout"] == "before failure\n"
    assert result["stderr"] == "warning first\n"
    assert 'File "<cad-task:test-call>", line 4' in result["traceback"]
    assert "TypeError: an integer is required" in result["traceback"]


def test_caught_exception_printed_to_stderr_is_visible_even_on_success(rpc_module):
    rpc = rpc_module.FreeCADRPC()
    [response] = execute_code_operation(
        rpc, True,
        "import traceback\ntry:\n    raise ValueError('caught failure')\nexcept ValueError:\n    traceback.print_exc()",
    )
    assert response.text.startswith("Code executed successfully:")
    assert "Stderr:" in response.text
    assert "Traceback (most recent call last):" in response.text
    assert 'File "<freecad-mcp-script>", line 3' in response.text
    assert "ValueError: caught failure" in response.text


def test_failure_feedback_preserves_diagnostics_without_requesting_screenshot(rpc_module):
    rpc = rpc_module.FreeCADRPC()
    rpc.get_active_screenshot = MagicMock(side_effect=AssertionError("must not request another GUI operation"))
    [response] = execute_code_operation(
        rpc, False, "print('partial result')\nraise RuntimeError('failure')", include_screenshot=True,
    )
    assert response.text.startswith("Failed to execute code: RuntimeError: failure")
    assert "Output before failure:\npartial result" in response.text
    assert 'File "<freecad-mcp-script>", line 2' in response.text
    rpc.get_active_screenshot.assert_not_called()


def test_success_keeps_persistent_namespace_and_restores_streams(rpc_module):
    before = (sys.stdout, sys.stderr)
    rpc = rpc_module.FreeCADRPC()
    result = rpc.execute_code("from __future__ import annotations\nshared_answer = 41\nprint(shared_answer)")
    assert result["success"] is True
    assert result["message"] == "Python code executed successfully.\nOutput: 41\n"
    assert rpc.execute_code("print(shared_answer + 1)")["message"].endswith("42\n")
    assert (sys.stdout, sys.stderr) == before


def test_large_diagnostics_are_bounded_with_an_explicit_truncation_marker(rpc_module):
    rpc = rpc_module.FreeCADRPC()
    result = rpc.execute_code(
        "import sys\nprint('BEGIN' + 'x' * 100000 + 'END')\n"
        "print('WARN' + 'y' * 100000 + 'LAST', file=sys.stderr)\nraise ValueError('z' * 20000)"
    )
    assert result["success"] is False
    for field, limit in (("stdout", 32768), ("stderr", 32768), ("traceback", 12000), ("error", 2000)):
        assert len(result[field]) <= limit
        assert "[output truncated]" in result[field]
    assert result["stdout"].startswith("BEGIN") and result["stdout"].endswith("END\n")
    assert result["stderr"].startswith("WARN") and result["stderr"].endswith("LAST\n")
    assert 'File "<freecad-mcp-script>", line 4' in result["traceback"]


def test_async_failure_returns_output_and_exact_location_via_status(rpc_module):
    rpc = rpc_module.FreeCADRPC()
    started = rpc.execute_code_async(
        "import sys\nprint('async before failure')\nprint('async stderr', file=sys.stderr)\nraise LookupError('missing')"
    )
    job = completed_job(rpc, started["job_id"])
    assert job["state"] == "failed"
    assert job["stdout"] == "async before failure\n"
    assert job["stderr"] == "async stderr\n"
    [response] = get_async_status_operation(rpc, started["job_id"])
    assert 'File "<freecad-mcp-script>", line 4' in response.text
    assert "async before failure" in response.text
    assert "async stderr" in response.text


def test_async_commit_and_other_calls_keep_separate_output(rpc_module):
    rpc = rpc_module.FreeCADRPC()
    entered, release = threading.Event(), threading.Event()
    rpc_module.FreeCAD.diagnostic_entered = entered
    rpc_module.FreeCAD.diagnostic_release = release
    started = rpc.execute_code_async(
        "print('worker only')\nFreeCAD.diagnostic_entered.set()\n"
        "FreeCAD.diagnostic_release.wait(3)\n"
        "def apply():\n    import sys\n    print('commit only')\n    print('commit stderr', file=sys.stderr)\n"
        "commit(apply)\nprint('worker finished')"
    )
    try:
        assert entered.wait(2)
        result = rpc.execute_code("import sys\nprint('GUI only')\nprint('GUI stderr', file=sys.stderr)")
        assert result["stdout"] == "GUI only\n"
        assert result["stderr"] == "GUI stderr\n"
        other = rpc.execute_code_async("print('other worker only')")
        other_job = completed_job(rpc, other["job_id"])
        assert other_job["stdout"] == "other worker only\n"
    finally:
        release.set()
    job = completed_job(rpc, started["job_id"])
    assert job["state"] == "done", json.dumps(job)
    assert job["stdout"] == "worker only\ncommit only\nworker finished\n"
    assert job["stderr"] == "commit stderr\n"


def test_system_exit_is_a_script_failure_and_does_not_kill_dispatch(rpc_module):
    rpc = rpc_module.FreeCADRPC()
    result = rpc.execute_code("print('before exit')\nraise SystemExit(2)")
    assert result["success"] is False
    assert result["error"] == "SystemExit: 2"
    assert result["stdout"] == "before exit\n"
    assert rpc.execute_code("print('still usable')")["success"] is True


def test_async_commit_failure_keeps_callback_line_and_partial_output(rpc_module):
    rpc = rpc_module.FreeCADRPC()
    source = "print('before commit')\ndef apply():\n    print('inside commit')\n    raise ValueError('callback failure')\ncommit(apply)"
    started = rpc.execute_code_async(source)
    job = completed_job(rpc, started["job_id"])
    assert job["state"] == "failed"
    assert job["stdout"] == "before commit\ninside commit\n"
    assert 'File "<freecad-mcp-script>", line 4, in apply' in job["traceback"]
    assert "ValueError: callback failure" in job["traceback"]
    assert job["error"].startswith("RuntimeError: commit() failed:")
