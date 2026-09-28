"""Check TCP binds without mistaking closed connections for running services."""
import argparse
import errno
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import time


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


def _process(pid):
    """Return the Linux process identity; a reused PID must never be signalled."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return int(fields[1]), fields[19]
    except (OSError, ValueError, IndexError):
        return None


def _ancestors(pid):
    result = set()
    while pid > 1 and pid not in result:
        result.add(pid)
        state = _process(pid)
        if not state:
            break
        pid = state[0]
    return result


def _conflicting_addresses(host):
    addresses = {item[4][0] for item in socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM)}
    # An IPv6 wildcard can also accept IPv4 connections (dual-stack socket).
    return addresses | {"*", "::"} | ({"0.0.0.0"} if any(":" not in value for value in addresses) else set())


def _linux_owners(port, host="127.0.0.1"):
    """Inspect listeners only, never clients connected to the same port."""
    result = subprocess.run(["ss", "-H", "-ltnp", f"( sport = :{port} )"],
                            capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise RuntimeError("无法识别 Linux 端口所有者：ss 执行失败。")
    owners = {}
    addresses = _conflicting_addresses(host)
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) < 5:
            continue
        address = fields[3].rsplit(":", 1)[0].strip("[]")
        if address not in addresses:
            continue
        for name, value in re.findall(r'\("([^"\n]+)",pid=(\d+),', line):
            pid = int(value)
            if state := _process(pid):
                owners[pid] = (name, state[1])
    return owners


def _old_supervisors(owners, project_root):
    protected = _ancestors(os.getpid())
    found = {}
    if project_root is None:
        return found
    for pid in owners:
        for ancestor in _ancestors(pid) - protected:
            try:
                args = Path(f"/proc/{ancestor}/cmdline").read_bytes().split(b"\0")
                cwd = Path(f"/proc/{ancestor}/cwd").resolve(strict=True)
            except OSError:
                continue
            if cwd == project_root.resolve() and args[1:3] == [b"-m", b"scripts.dev"]:
                if state := _process(ancestor):
                    found[ancestor] = ("旧 dev 启动器", state[1])
    return found


def _stop_linux(owners, *, grace, emit):
    protected = _ancestors(os.getpid()) | {0, 1}
    for pid, (name, born) in owners.items():
        if pid in protected:
            raise RuntimeError(f"冲突端口属于当前启动终端/祖先进程 {name} (PID {pid})，无法安全自动结束。")
        if not (state := _process(pid)) or state[1] != born:
            continue
        emit(f"  关闭 Linux 进程 {name} (PID {pid})")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise RuntimeError(f"无权结束 Linux 进程 {name} (PID {pid})；请由该进程所有者关闭后重试。") from exc
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if all(not (state := _process(pid)) or state[1] != born for pid, (_, born) in owners.items()):
            return
        time.sleep(0.1)
    for pid, (name, born) in owners.items():
        if (state := _process(pid)) and state[1] == born:
            emit(f"  强制关闭 Linux 进程 {name} (PID {pid})")
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise RuntimeError(f"无权强制结束 Linux 进程 {name} (PID {pid})。") from exc


def _powershell():
    if "microsoft" not in Path("/proc/sys/kernel/osrelease").read_text().lower():
        return None
    return shutil.which("powershell.exe") or next((str(p) for p in (
        Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"),
        Path("/mnt/c/WINDOWS/System32/WindowsPowerShell/v1.0/powershell.exe"),
    ) if p.is_file()), None)


def _windows_run(executable, command):
    try:
        result = subprocess.run([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
                                 "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new(); "
                                 "$ErrorActionPreference='Stop'; " + command],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Windows 端口查询/清理超过 15 秒；请检查 PowerShell 与 WSL 互通后重试。") from exc
    if result.returncode:
        raise RuntimeError("Windows 端口清理失败：" + (result.stderr.strip() or result.stdout.strip())[:1200])
    return result.stdout.strip()


def _windows_address_filter(host):
    values = ",".join("'" + value.replace("'", "''") + "'" for value in sorted(_conflicting_addresses(host)))
    return f"Where-Object {{ @({values}) -contains $_.LocalAddress }} | "


def _windows_owners(executable, port, host="127.0.0.1"):
    output = _windows_run(executable, f"$owners = @(Get-NetTCPConnection -State Listen -LocalPort {int(port)} "
                         "-ErrorAction SilentlyContinue | " + _windows_address_filter(host) +
                         "Select-Object -ExpandProperty OwningProcess -Unique | "
                         "ForEach-Object { $p=Get-Process -Id $_ -ErrorAction SilentlyContinue; "
                         "if ($p) { @{pid=$p.Id; name=$p.ProcessName} } }); "
                         "ConvertTo-Json -InputObject $owners -Compress")
    try:
        owners = json.loads(output or "[]")
        return {int(item["pid"]): str(item["name"]) for item in owners}
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("Windows 未返回可识别的端口所有者信息。") from exc


def _stop_windows(executable, port, owners, emit, host="127.0.0.1"):
    for pid, name in owners.items():
        if pid <= 4 or name.lower() in {"system", "registry", "vmmem", "vmmemwsl", "wslhost", "wslservice"}:
            raise RuntimeError(f"Windows 端口 {port} 属于系统/WSL 进程 {name} (PID {pid})，"
                               "不能通过结束它来清理端口；请检查 Windows 端口转发或保留范围。")
        emit(f"  关闭 Windows 进程 {name} (PID {pid})，释放端口 {port}")
        # Recheck ownership inside Windows immediately before terminating it.
        _windows_run(executable, f"$p=Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
                     f"$owner=@(Get-NetTCPConnection -State Listen -LocalPort {int(port)} "
                     "-ErrorAction SilentlyContinue | " + _windows_address_filter(host) +
                     "Select-Object -ExpandProperty OwningProcess); "
                     f"if ($p -and ($owner -contains {pid}) -and $p.ProcessName -eq "
                     f"'{name.replace(chr(39), chr(39) * 2)}') {{ Stop-Process -Id {pid} -Force }}")


def reclaim_port(host, port, name="服务", *, project_root=None, emit=print, grace=2.0):
    """Reclaim a configured service port before launch; user explicitly opted in.

    Only listener owners are stopped. An older supervisor from this checkout is
    allowed to finish its own cleanup first. Processes and Windows/WSL itself
    are never killed by a broad executable-name or process-group match.
    """
    try:
        check_port(host, port, name)
        return
    except RuntimeError:
        pass
    emit(f"{name} 端口 {host}:{port} 已占用，正在清理冲突进程……")
    owners = _linux_owners(port, host)
    supervisors = _old_supervisors(owners, project_root)
    if supervisors:
        _stop_linux(supervisors, grace=9.0, emit=emit)
        owners = _linux_owners(port, host)
    if owners:
        _stop_linux(owners, grace=grace, emit=emit)
    # Let close/exit propagate, but do not mistake TIME_WAIT for a live owner.
    for _ in range(10):
        try:
            check_port(host, port, name)
            return
        except RuntimeError:
            time.sleep(0.1)
    executable = _powershell()
    if executable:
        windows = _windows_owners(executable, port, host)
        if windows:
            _stop_windows(executable, port, windows, emit, host)
            for _ in range(20):
                try:
                    check_port(host, port, name)
                    return
                except RuntimeError:
                    time.sleep(0.1)
        else:
            emit(f"  Windows 未发现端口 {port} 的监听进程；可能是系统保留范围或 WSL 镜像网络转发。")
    try:
        check_port(host, port, name)
    except RuntimeError as exc:
        raise RuntimeError(f"自动清理后 {name} 端口 {port} 仍不可用，尚未启动新服务。\n"
                           "无法定位的系统保留端口不能靠杀进程解除；请检查 Windows 端口保留/转发，"
                           "或确认是否有其他用户的进程占用。\n" + str(exc)) from exc


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
