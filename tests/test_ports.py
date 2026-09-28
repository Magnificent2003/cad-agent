"""Exercise real TCP states behind repeated-launch failures."""
import errno
import socket

import pytest

from scripts.ports import check_port


def test_closed_server_in_time_wait_can_be_started_again():
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        host, port = server.getsockname()
        with socket.create_connection((host, port), timeout=2) as client:
            connection, _ = server.accept()
            # Close from the server side so its port, not just the client port,
            # enters TIME_WAIT. This happens after FreeCAD's readiness RPC.
            connection.close()
            assert client.recv(1) == b""
    with socket.socket() as old_check:
        with pytest.raises(OSError) as error:
            old_check.bind((host, port))
        assert error.value.errno == errno.EADDRINUSE
    check_port(host, port, "FreeCAD")
    check_port(host, port, "FreeCAD")


@pytest.mark.parametrize("listening", [False, True])
def test_occupied_port_rejected_without_disturbing_its_owner(listening):
    with socket.socket() as owner:
        owner.bind(("127.0.0.1", 0))
        host, port = owner.getsockname()
        if listening:
            owner.listen(1)
        with pytest.raises(RuntimeError, match=rf"网页 无法监听 {host}:{port}.*EADDRINUSE"):
            check_port(host, port, "网页")
        # The failed check must not stop, replace or adopt the existing owner.
        owner.listen(1)
        with socket.create_connection((host, port), timeout=2):
            connection, _ = owner.accept()
            connection.close()


def test_reusable_live_listener_is_still_rejected():
    with socket.socket() as owner:
        owner.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        owner.bind(("127.0.0.1", 0))
        owner.listen(1)
        host, port = owner.getsockname()
        with pytest.raises(RuntimeError, match="EADDRINUSE"):
            check_port(host, port)


def test_reclaim_stubborn_listener_and_restart_without_touching_other_ports():
    import subprocess
    import sys

    from scripts.ports import reclaim_port

    code = (
        "import signal,socket,sys,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1); "
        "s.bind(('127.0.0.1',int(sys.argv[1]))); s.listen(); "
        "print(s.getsockname()[1],flush=True); time.sleep(60)"
    )
    # A distinct listener remains alive throughout two real reclaim/rebinds.
    with socket.socket() as unrelated:
        unrelated.bind(("127.0.0.1", 0))
        unrelated.listen()
        port = 0
        for _ in range(2):
            owner = subprocess.Popen([sys.executable, "-c", code, str(port)],
                                     stdout=subprocess.PIPE, text=True)
            try:
                port = int(owner.stdout.readline())
                messages = []
                reclaim_port("127.0.0.1", port, "测试服务", emit=messages.append, grace=0.1)
                assert owner.wait(timeout=3) < 0
                assert any("强制关闭" in message for message in messages)
                check_port("127.0.0.1", port)
                with socket.create_connection(unrelated.getsockname(), timeout=1):
                    connection, _ = unrelated.accept()
                    connection.close()
            finally:
                if owner.poll() is None:
                    owner.kill()
                owner.wait(timeout=3)
                owner.stdout.close()


def test_reclaim_stops_exact_old_supervisor_before_its_children(tmp_path):
    import subprocess
    import sys

    from scripts.ports import reclaim_port

    package = tmp_path / "scripts"
    package.mkdir()
    (package / "__init__.py").touch()
    finished = tmp_path / "cleanup-finished"
    child_code = (
        "import socket,time; s=socket.socket(); s.bind(('127.0.0.1',0)); "
        "s.listen(); print(s.getsockname()[1],flush=True); time.sleep(60)"
    )
    (package / "dev.py").write_text(
        "import subprocess,sys,signal,time,pathlib\n"
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}],stdout=subprocess.PIPE,text=True)\n"
        "def stop(*_):\n"
        "    time.sleep(0.1)\n"
        "    child.terminate()\n"
        "    child.wait(timeout=3)\n"
        f"    pathlib.Path({str(finished)!r}).touch()\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM,stop)\n"
        "print(child.stdout.readline().strip(),flush=True)\n"
        "while True: time.sleep(1)\n"
    )
    supervisor = subprocess.Popen([sys.executable, "-m", "scripts.dev"], cwd=tmp_path,
                                  stdout=subprocess.PIPE, text=True)
    try:
        port = int(supervisor.stdout.readline())
        messages = []
        reclaim_port("127.0.0.1", port, project_root=tmp_path, emit=messages.append)
        assert supervisor.wait(timeout=3) == 0
        assert finished.is_file()
        assert any("旧 dev 启动器" in message for message in messages)
        check_port("127.0.0.1", port)
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
        supervisor.wait(timeout=3)
        supervisor.stdout.close()


def test_reclaim_refuses_to_kill_its_own_invoker():
    from scripts.ports import reclaim_port

    with socket.socket() as owner:
        owner.bind(("127.0.0.1", 0))
        owner.listen()
        with pytest.raises(RuntimeError, match="当前启动终端/祖先进程"):
            reclaim_port("127.0.0.1", owner.getsockname()[1], emit=lambda _: None)
        assert owner.fileno() >= 0
