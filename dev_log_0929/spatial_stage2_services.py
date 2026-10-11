"""Own an isolated instance of the real backend, agent, and FreeCAD for stage 2.

The backend serves the built web UI on its own port. No production database,
model configuration or occupied port is replaced. Ctrl+C stops only owned groups.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import dotenv_values  # noqa: E402
from agent.launcher import build_config  # noqa: E402
from agent.network import prepare_model_network  # noqa: E402
from agent.runtime import engine_command, engine_environment, normalize_environment  # noqa: E402
from dev_log_0929.spatial_agent_stage1 import free_port, sanitizer, stop_owned_process  # noqa: E402
from scripts.dev import freecad_ready, port_open  # noqa: E402


async def run(args):
    runtime = args.runtime.resolve()
    runtime.mkdir(parents=True, exist_ok=False)
    env = normalize_environment({**{k: v for k, v in dotenv_values(ROOT / '.env').items() if v is not None}, **os.environ})
    clean = sanitizer(env)
    ports = set()
    while len(ports) < 3:
        ports.add(free_port())
    rpc_port, agent_port, backend_port = sorted(ports)
    workspace = runtime / 'engine/workspace'
    workspace.mkdir(parents=True)
    env.update(CAD_DATA_DIR=str(runtime / 'data'), FREECAD_RPC_PORT=str(rpc_port),
               AGENT_BASE_URL=f'http://127.0.0.1:{agent_port}',
               CAD_API_BASE_URL=f'http://127.0.0.1:{backend_port}',
               CAD_STAGE2_AGENT_WORKSPACE=str(workspace))
    metadata = {'runtime': str(runtime), 'data_dir': env['CAD_DATA_DIR'], 'agent_workspace': str(workspace),
                'agent_url': env['AGENT_BASE_URL'], 'backend_url': env['CAD_API_BASE_URL'],
                'rpc_port': rpc_port, 'status': 'starting', 'pids': {}}
    path = runtime / 'services.json'
    path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    processes = []
    loop, active = asyncio.get_running_loop(), asyncio.current_task()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, active.cancel)
    try:
        env, _note = await asyncio.to_thread(prepare_model_network, env)
        config = build_config(env)
        agent_env = engine_environment(env, config, runtime=runtime / 'engine')
        gui_env = {k: v for k, v in env.items() if not k.startswith(('DEEPSEEK_', 'AIAPI_'))
                   and k not in {'CAD_INTERNAL_TOKEN', 'AGENT_SERVER_PASSWORD', 'OPENCODE_SERVER_PASSWORD'}}
        backend_code = (
            'import os,uvicorn; from backend.app.main import create_app; '
            'from backend.app.config import get_settings; from agent.client import AgentClient; '
            's=get_settings(); a=create_app(s,agent_client=AgentClient(s,directory=os.environ["CAD_STAGE2_AGENT_WORKSPACE"])); '
            f'uvicorn.run(a,host="127.0.0.1",port={backend_port},log_level="info")'
        )
        services = [
            ('freecad', ['bash', 'freecad/start.sh'], gui_env, ROOT, rpc_port),
            ('agent', [*engine_command(env), 'serve', '--hostname', '127.0.0.1', '--port', str(agent_port)], agent_env, workspace, agent_port),
            ('backend', [str(ROOT / '.venv/bin/python'), '-c', backend_code], env, ROOT, backend_port),
        ]
        for name, command, child_env, cwd, port in services:
            with (runtime / f'{name}.log').open('w') as log:
                process = subprocess.Popen(command, cwd=cwd, env=child_env, start_new_session=True, stdout=log, stderr=subprocess.STDOUT)
            processes.append(process)
            metadata['pids'][name] = process.pid
            deadline = time.monotonic() + 100
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f'{name} exited: {process.returncode}; inspect {runtime / (name + ".log")}')
                ready = await asyncio.to_thread(freecad_ready, port) if name == 'freecad' else port_open(port)
                if ready:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(f'{name} readiness timed out')
                await asyncio.sleep(.5)
            print(json.dumps({'service': name, 'port': port, 'status': 'ready'}, ensure_ascii=False), flush=True)
        metadata['status'] = 'ready'
        path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        print(json.dumps(metadata, ensure_ascii=False), flush=True)
        while True:
            if any(process.poll() is not None for process in processes):
                raise RuntimeError('An owned service exited; shutting down the remaining experiment services')
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        metadata['status'] = 'stopped'
    except Exception as exc:
        metadata.update(status='error', error=clean(str(exc)))
        print(json.dumps({'status': 'error', 'error': metadata['error']}, ensure_ascii=False), flush=True)
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: None)
        for process in reversed(processes):
            await stop_owned_process(process)
        metadata['owned_processes_stopped'] = all(p.poll() is not None for p in processes)
        path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        print(json.dumps({'status': metadata['status'], 'owned_processes_stopped': metadata['owned_processes_stopped']}), flush=True)
    return 1 if metadata['status'] == 'error' else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=ROOT / '.runtime/spatial-stage2' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]))
    sys.exit(asyncio.run(run(parser.parse_args())))
