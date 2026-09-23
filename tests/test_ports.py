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
