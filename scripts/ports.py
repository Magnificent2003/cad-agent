"""Check TCP binds without mistaking closed connections for running services."""
import argparse
import errno
import socket
import subprocess


def port_diagnostics(port: int) -> str:
    """Include listeners and TIME_WAIT, without inspecting process environments."""
    try:
        result = subprocess.run(
            ["ss", "-tanp", f"( sport = :{port} )"],
            capture_output=True, text=True, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "无法获取端口状态。"
    if result.returncode:
        return "无法获取端口状态。"
    return result.stdout.strip() or "未发现本机 TCP 记录。"


def check_port(host: str, port: int, name: str = "服务") -> None:
    """Use the same SO_REUSEADDR semantics as our Python/Node TCP servers.

    A failed connect does not imply a bind will succeed. A bare bind without
    SO_REUSEADDR, on the other hand, incorrectly rejects reusable TIME_WAIT.
    Listen as well: bind alone can coexist with another reusable bound socket.
    This is a preflight check, not a reservation through the subsequent launch.
    """
    try:
        for family, kind, protocol, _, address in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
            with socket.socket(family, kind, protocol) as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(address)
                listener.listen(1)
    except OSError as exc:
        code = errno.errorcode.get(exc.errno, str(exc.errno))
        raise RuntimeError(
            f"{name} 无法监听 {host}:{port}（{code}: {exc.strerror}）。\n"
            "请检查端口占用；WSL 镜像网络还需检查 Windows 端。\n"
            + port_diagnostics(port)
        ) from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost", "::1"))
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--name", default="服务")
    args = parser.parse_args()
    try:
        check_port(args.host, args.port, args.name)
    except RuntimeError as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
