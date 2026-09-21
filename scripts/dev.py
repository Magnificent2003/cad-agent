"""Own the three development processes and shut them down together."""
import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production", action="store_true", help="serve frontend/dist at :8000 instead of starting Vite")
    parser.add_argument("--without-agent", action="store_true", help="start CAD file/preview service without a model connection")
    args = parser.parse_args()
    config = dotenv_values(ROOT / ".env")
    api = urlsplit(config.get("CAD_API_BASE_URL") or "http://127.0.0.1:8000")
    oc = urlsplit(config.get("OPENCODE_BASE_URL") or "http://127.0.0.1:4096")
    if api.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("首期服务只允许本机绑定。")
    ports = [api.port or 8000] + ([] if args.without_agent else [oc.port or 4096]) + ([] if args.production else [5173])
    for port in ports:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                raise SystemExit(f"端口 {port} 已有服务运行，请使用已有服务或先停止该进程。")
    if args.production and not (ROOT / "frontend/dist/index.html").is_file():
        raise SystemExit("请先执行 npm run build --prefix frontend。")
    env = dict(os.environ)
    env["PATH"] = str(ROOT / ".tools/node/bin") + os.pathsep + env.get("PATH", "")
    # Model credentials are loaded only by the Python server/agent launcher.
    python = str(ROOT / ".venv/bin/python")
    commands = [[python, "-m", "uvicorn", "backend.app.main:app", "--host", api.hostname or "127.0.0.1", "--port", str(api.port or 8000), "--workers", "1"]]
    if not args.without_agent:
        commands.append([python, "scripts/start_opencode.py"])
    if not args.production:
        commands.append([str(ROOT / ".tools/node/bin/npm"), "run", "dev", "--prefix", "frontend"])
    processes = []
    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        for command in commands:
            child_env = dict(env)
            if "frontend" in command:
                for key in tuple(child_env):
                    if key.startswith("DEEPSEEK_") or key in {"CAD_INTERNAL_TOKEN", "OPENCODE_SERVER_PASSWORD"}:
                        child_env.pop(key)
            processes.append(subprocess.Popen(command, cwd=ROOT, env=child_env, start_new_session=True))
        print(f"打开 http://127.0.0.1:{api.port or 8000 if args.production else 5173}；Ctrl+C 停止全部服务。", flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
        print("一个服务已退出，正在停止其余服务。", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()


if __name__ == "__main__":
    main()
