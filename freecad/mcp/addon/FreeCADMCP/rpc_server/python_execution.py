"""Bounded Python diagnostics shared by GUI and background script execution.

Stream routing is per thread: a background job must not steal output from the
GUI or another job. Unrelated threads continue writing to the original stream.
This captures Python text output, not native-library file descriptor writes.
"""
from contextlib import contextmanager
import sys
import threading
import traceback
from typing import Any, Callable, Iterator


OUTPUT_LIMIT = 32768
TRACEBACK_LIMIT = 12000
_STREAM_LOCK = threading.RLock()
_ACTIVE_CAPTURES = 0
_STREAMS: dict[str, Any] = {}


def bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "\n... [output truncated] ...\n"
    available = limit - len(marker)
    head = available // 2
    return value[:head] + marker + value[-(available - head):]


class _Output:
    """Retain the beginning and end without accumulating unbounded output."""
    def __init__(self) -> None:
        self.head = ""
        self.tail = ""
        self.length = 0

    def write(self, value: str) -> int:
        if not isinstance(value, str):
            raise TypeError("write() argument must be str")
        self.length += len(value)
        missing = OUTPUT_LIMIT // 2 - len(self.head)
        self.head += value[:missing]
        self.tail = (self.tail + value[missing:])[-(OUTPUT_LIMIT // 2):]
        return len(value)

    def flush(self) -> None:
        pass

    def getvalue(self) -> str:
        if self.length <= OUTPUT_LIMIT:
            return self.head + self.tail
        return bounded_text(self.head + "\n... [output truncated] ...\n" + self.tail, OUTPUT_LIMIT)


class _ThreadStream:
    def __init__(self, original: Any) -> None:
        self.original = original
        self.local = threading.local()

    def write(self, value: str) -> int:
        return getattr(self.local, "target", self.original).write(value)

    def flush(self) -> None:
        getattr(self.local, "target", self.original).flush()

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.original, name)


@contextmanager
def _capture(stdout: _Output | None = None, stderr: _Output | None = None) -> Iterator[tuple[_Output, _Output]]:
    global _ACTIVE_CAPTURES
    stdout = stdout if stdout is not None else _Output()
    stderr = stderr if stderr is not None else _Output()
    previous = {}
    with _STREAM_LOCK:
        if not _ACTIVE_CAPTURES:
            for name in ("stdout", "stderr"):
                _STREAMS[name] = _ThreadStream(getattr(sys, name))
                setattr(sys, name, _STREAMS[name])
        _ACTIVE_CAPTURES += 1
        for name, target in (("stdout", stdout), ("stderr", stderr)):
            stream = _STREAMS[name]
            previous[name] = getattr(stream.local, "target", None)
            stream.local.target = target
    try:
        yield stdout, stderr
    finally:
        with _STREAM_LOCK:
            for name, target in previous.items():
                stream = _STREAMS[name]
                if target is None:
                    del stream.local.target
                else:
                    stream.local.target = target
            _ACTIVE_CAPTURES -= 1
            if not _ACTIVE_CAPTURES:
                for name, stream in _STREAMS.items():
                    if getattr(sys, name) is stream:
                        setattr(sys, name, stream.original)
                _STREAMS.clear()


def forward_output(callback: Callable[[], Any]) -> Callable[[], Any]:
    """Carry an async script's output routing into its GUI commit callback."""
    with _STREAM_LOCK:
        targets = tuple(getattr(_STREAMS.get(name), "local", None) for name in ("stdout", "stderr"))
        outputs = tuple(getattr(local, "target", None) for local in targets)
    if not any(outputs):
        return callback

    def run() -> Any:
        with _capture(*outputs):
            return callback()

    return run


def execute_python(code: str, namespace: dict[str, Any]) -> dict[str, Any]:
    """Execute in the persistent namespace and retain diagnostics on failure."""
    result: dict[str, Any] = {"success": True}
    with _capture() as (stdout, stderr):
        try:
            exec(compile(code, "<freecad-mcp-script>", "exec"), namespace)
        except BaseException as exc:
            result.update(
                success=False,
                error=bounded_text(f"{type(exc).__name__}: {exc}", 2000),
                traceback=bounded_text(traceback.format_exc(limit=-12).rstrip(), TRACEBACK_LIMIT),
            )
    result.update(stdout=stdout.getvalue(), stderr=stderr.getvalue())
    return result
