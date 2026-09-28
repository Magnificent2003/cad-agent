"""Check startup failure and interruption do not leave owned services alive."""
import os
from pathlib import Path
import signal
import subprocess
import sys

import pytest

from scripts import dev


def test_failed_second_launch_cleans_first_service(monkeypatch):
    real_popen = subprocess.Popen
    launched = []

    def launch(*args, **kwargs):
        if launched:
            raise OSError("second executable missing")
        process = real_popen(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(dev.subprocess, "Popen", launch)
    monkeypatch.setattr(dev, "wait_ready", lambda *args: None)
    services = [dev.Service(name, [sys.executable, "-c", "import time; time.sleep(60)"], port)
                for name, port in [("first", 18000), ("second", 18001)]]
    with pytest.raises(OSError, match="second executable missing"):
        dev.run_services(services, dict(os.environ), lambda: None)
    assert launched[0].poll() is not None


def test_interrupt_cleans_descendant_even_after_parent_exits(tmp_path, monkeypatch):
    import time

    child_pid_file = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, pathlib; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"pathlib.Path({str(child_pid_file)!r}).write_text(str(p.pid))"
    )
    service = dev.Service("parent", [sys.executable, "-c", code], 18000)

    def wait_for_parent_exit(service, processes):
        processes[0][1].wait(timeout=5)
        assert child_pid_file.is_file()
        raise KeyboardInterrupt

    monkeypatch.setattr(dev, "wait_ready", wait_for_parent_exit)
    child_pid = None
    try:
        with pytest.raises(KeyboardInterrupt):
            dev.run_services([service], dict(os.environ), lambda: None)
        child_pid = int(child_pid_file.read_text())
        for _ in range(100):
            stat = Path(f"/proc/{child_pid}/stat")
            if not stat.exists() or stat.read_text().split()[2] == "Z":
                break
            time.sleep(0.01)
        else:
            pytest.fail("descendant survived supervisor cleanup")
    finally:
        if child_pid_file.exists():
            child_pid = int(child_pid_file.read_text())
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_startup_timeout_is_actionable_and_dead_service_is_not_ready(monkeypatch, tmp_path):
    service = dev.Service("FreeCAD", [], 9875, tmp_path / "gui.log")
    with pytest.raises(RuntimeError, match="启动超时.*gui.log"):
        dev.wait_ready(service, [], timeout=0)
    process = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"], start_new_session=True)
    process.wait(timeout=5)
    monkeypatch.setattr(dev, "freecad_ready", lambda *_: True)
    with pytest.raises(RuntimeError, match="FreeCAD 已退出（7）"):
        dev.wait_ready(service, [(service, process)])


def test_preflight_bind_failure_stops_before_any_service_launch(tmp_path, monkeypatch, capsys):
    application = tmp_path / "freecad/runtime/squashfs-root/AppRun"
    application.parent.mkdir(parents=True)
    application.touch()
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["dev"])
    monkeypatch.setattr(dev.os, "environ", {})
    monkeypatch.setattr(dev.signal, "signal", lambda *args: None)
    checked = []

    def check(host, port, name, **kwargs):
        checked.append(port)
        if port == 4096:
            raise RuntimeError("智能体 无法监听 127.0.0.1:4096（EADDRINUSE）")

    monkeypatch.setattr(dev, "reclaim_port", check)
    monkeypatch.setattr(dev.subprocess, "run", lambda *a, **kw: pytest.fail("must not launch a process"))
    monkeypatch.setattr(dev, "run_services", lambda *a: pytest.fail("must not start services"))
    assert dev.main() == 1
    assert checked == [9875, 8000, 4096]
    assert "4096（EADDRINUSE）" in capsys.readouterr().err
    assert "4096（EADDRINUSE）" in (tmp_path / ".runtime/startup-errors.log").read_text()


def test_cleanup_finishes_after_repeated_interrupts_and_restores_handlers(monkeypatch):
    real_signal_group = dev.signal_group
    original = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    steps = []
    processes = []

    def interrupt(*_):
        raise KeyboardInterrupt

    def begin_cleanup(service, launched):
        processes.extend(process for _, process in launched)
        raise KeyboardInterrupt

    def signal_group(process, sig):
        if sig == signal.SIGTERM:
            os.kill(os.getpid(), signal.SIGINT)
            os.kill(os.getpid(), signal.SIGTERM)
        real_signal_group(process, sig)
        steps.append(sig)

    monkeypatch.setattr(dev, "wait_ready", begin_cleanup)
    monkeypatch.setattr(dev, "signal_group", signal_group)
    for sig in original:
        signal.signal(sig, interrupt)
    try:
        service = dev.Service("test", [sys.executable, "-c", "import time; time.sleep(60)"], 0)
        with pytest.raises(KeyboardInterrupt):
            dev.run_services([service], dict(os.environ), lambda: None)
        assert steps == [signal.SIGTERM, signal.SIGKILL]
        assert all(process.poll() is not None for process in processes)
        assert all(signal.getsignal(sig) is interrupt for sig in original)
    finally:
        for sig, handler in original.items():
            signal.signal(sig, handler)
        for process in processes:
            real_signal_group(process, signal.SIGKILL)
            process.wait(timeout=3)


def test_freecad_fallback_is_shared_with_every_service(tmp_path, monkeypatch):
    application = tmp_path / "freecad/runtime/squashfs-root/AppRun"
    application.parent.mkdir(parents=True)
    application.touch()
    monkeypatch.setattr(dev, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["dev"])
    monkeypatch.setattr(dev.os, "environ", {})
    monkeypatch.setattr(dev.signal, "signal", lambda *args: None)
    monkeypatch.setattr(dev.subprocess, "run", lambda *a, **kw: None)

    def reclaim(host, port, name, **kwargs):
        if name == "FreeCAD":
            raise dev.UnownedPortError("unidentified bind conflict")

    monkeypatch.setattr(dev, "reclaim_port", reclaim)
    monkeypatch.setattr(dev, "available_alternative", lambda *args: 9876)
    launched = []
    monkeypatch.setattr(dev, "run_services", lambda services, env, ready: launched.append((services, env)))
    assert dev.main() == 0
    services, env = launched[0]
    assert [s.port for s in services] == [9876, 8000, 4096, 5173]
    assert env["FREECAD_RPC_PORT"] == "9876"
