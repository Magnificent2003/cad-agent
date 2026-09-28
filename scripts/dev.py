"""Start and stop FreeCAD, the backend, the agent and the web UI together."""
import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit
import xmlrpc.client

from dotenv import dotenv_values

from scripts.ports import UnownedPortError, available_alternative, check_port, port_diagnostics, reclaim_port

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Service:
    name: str
    command: list[str]
    port: int
    log: Path | None = None
    host: str = "127.0.0.1"


def port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


class RPCTransport(xmlrpc.client.Transport):
    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = 2
        return connection


def freecad_ready(port=9875):
    try:
        with xmlrpc.client.ServerProxy(f"http://127.0.0.1:{port}", transport=RPCTransport()) as rpc:
            return rpc.ping() is True
    except (OSError, xmlrpc.client.Error):
        return False


def check_processes(processes):
    for service, process in processes:
        if process.poll() is not None:
            detail = f"；日志：{service.log}" if service.log else "；请查看上方日志"
            # Capture state before cleanup removes the other services. Engine
            # releases may report only 'ServeError', hiding the OS bind error.
            status = port_diagnostics(service.port)
            try:
                check_port(service.host, service.port, service.name)
            except RuntimeError as exc:
                status = str(exc)
            raise RuntimeError(f"{service.name} 已退出（{process.returncode}）{detail}\n退出时端口状态：\n{status}")


def wait_ready(service, processes, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check_processes(processes)
        ready = freecad_ready(service.port) if service.name == "FreeCAD" else port_open(service.port, service.host)
        if ready:
            print(f"  已启动 {service.name}：127.0.0.1:{service.port}", flush=True)
            return
        time.sleep(0.5)
    detail = f"；日志：{service.log}" if service.log else ""
    raise RuntimeError(f"等待 {service.name} 启动超时{detail}")


def signal_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def run_services(services, env, on_ready):
    processes = []
    try:
        for service in services:
            child_env = dict(env)
            if service.name in {"网页", "FreeCAD"}:
                for key in tuple(child_env):
                    if key.startswith("DEEPSEEK_") or key in {"CAD_INTERNAL_TOKEN", "AGENT_SERVER_PASSWORD", "OPENCODE_SERVER_PASSWORD"}:
                        child_env.pop(key)
            log = None
            try:
                if service.log:
                    service.log.parent.mkdir(parents=True, exist_ok=True)
                    log = service.log.open("a", encoding="utf-8")
                    log.write(f"\n--- 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                    log.flush()
                process = subprocess.Popen(service.command, cwd=ROOT, env=child_env, start_new_session=True,
                                           stdout=log, stderr=subprocess.STDOUT if log else None)
                processes.append((service, process))
            finally:
                if log:
                    log.close()
            # FreeCAD's RPC must be ready before the agent starts.
            wait_ready(service, processes)
        on_ready()
        while True:
            check_processes(processes)
            time.sleep(0.5)
    finally:
        # A second Ctrl+C or a new launcher stopping this supervisor must not
        # interrupt its final KILL/wait and leave half the service tree alive.
        previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        for sig in previous_handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            print("正在停止本次启动的全部服务……", flush=True)
            # Signal owned groups even if a leader exited: MCP children may remain.
            for _, process in reversed(processes):
                signal_group(process, signal.SIGTERM)
            deadline = time.monotonic() + 8
            for _, process in reversed(processes):
                try:
                    process.wait(timeout=max(0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    pass
            for _, process in reversed(processes):
                signal_group(process, signal.SIGKILL)
                process.wait()
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description="统一启动 FreeCAD、后端、智能体和网页")
    parser.add_argument("--production", action="store_true", help="由后端提供已构建网页，不启动 Vite")
    parser.add_argument("--freecad", action="store_true", help=argparse.SUPPRESS)  # compatibility: now always enabled
    args = parser.parse_args()
    config = {**dotenv_values(ROOT / ".env"), **os.environ}
    api = urlsplit(config.get("CAD_API_BASE_URL") or "http://127.0.0.1:8000")
    agent = urlsplit(config.get("AGENT_BASE_URL") or config.get("OPENCODE_BASE_URL") or "http://127.0.0.1:4096")
    if api.hostname not in {"127.0.0.1", "localhost", "::1"} or agent.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("服务只允许本机绑定。")
    if args.production and not (ROOT / "frontend/dist/index.html").is_file():
        raise SystemExit("请先执行 npm run build --prefix frontend。")
    if not (ROOT / "freecad/runtime/squashfs-root/AppRun").is_file():
        raise SystemExit("请先运行 bash freecad/install.sh。FreeCAD 是必需组件，没有旧智能体备用入口。")
    try:
        rpc_port = int(config.get("FREECAD_RPC_PORT") or 9875)
    except ValueError:
        raise SystemExit("FREECAD_RPC_PORT 必须为 1～65535 的整数。")
    if not 1 <= rpc_port <= 65535:
        raise SystemExit("FREECAD_RPC_PORT 必须为 1～65535 的整数。")
    env = dict(os.environ)
    env["PATH"] = str(ROOT / ".tools/node/bin") + os.pathsep + env.get("PATH", "")
    python = str(ROOT / ".venv/bin/python")
    services = [
        Service("FreeCAD", ["bash", "freecad/start.sh"], rpc_port, ROOT / ".runtime/freecad/gui.log"),
        Service("业务后端", [python, "-m", "uvicorn", "backend.app.main:app", "--host", api.hostname,
                           "--port", str(api.port or 8000), "--workers", "1"], api.port or 8000, host=api.hostname),
        Service("FreeCAD 智能体", [python, "-m", "agent.launcher"], agent.port or 4096,
                ROOT / ".runtime/agent/server.log", host=agent.hostname or "127.0.0.1"),
    ]
    if not args.production:
        services.append(Service("网页", [str(ROOT / ".tools/node/bin/npm"), "run", "dev", "--prefix", "frontend"], 5173))
    if len({service.port for service in services}) != len(services):
        raise SystemExit("配置中有重复端口，请检查 .env。")

    def stop(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)

    def on_ready():
        print(f"\n全部服务已启动。打开 http://127.0.0.1:{(api.port or 8000) if args.production else 5173}；Ctrl+C 一起停止。", flush=True)
        print("网页已使用 FreeCAD 自主执行智能体；无需另开实验服务。\n"
              "日志：.runtime/agent/server.log、.runtime/freecad/gui.log", flush=True)

    try:
        for service in services:
            try:
                reclaim_port(service.host, service.port, service.name, project_root=ROOT,
                             emit=lambda message: print(message, flush=True))
            except UnownedPortError:
                if service.name != "FreeCAD":
                    raise
                original = service.port
                service.port = available_alternative(service.host, original, {s.port for s in services})
                print(f"FreeCAD 端口 {original} 的占用无法定位，本次改用 {service.port}；已同步后端与 MCP。", flush=True)
        env["FREECAD_RPC_PORT"] = str(services[0].port)
        subprocess.run(["bash", "freecad/install_python.sh", "--check"], cwd=ROOT, env=env, check=True)
        subprocess.run([python, "-m", "agent.launcher", "--check"], cwd=ROOT, env=env, check=True)
        run_services(services, env, on_ready)
    except KeyboardInterrupt:
        pass
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        message = f"启动失败或服务异常：{exc}"
        print(message, file=sys.stderr)
        diagnostic = ROOT / ".runtime/startup-errors.log"
        try:
            diagnostic.parent.mkdir(parents=True, exist_ok=True)
            with diagnostic.open("a", encoding="utf-8") as log:
                log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{message}\n")
            print(f"启动诊断已保存：{diagnostic}", file=sys.stderr)
        except OSError:
            print("无法写入启动诊断文件，请保留以上终端输出。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
