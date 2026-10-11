"""Start the project's agent service with its CAD tools and model settings."""
from __future__ import annotations

import argparse
import copy
import json
import os
from urllib.parse import urlparse

from dotenv import load_dotenv

from .network import ModelNetworkError, prepare_model_network
from .provider import required_credentials, select_provider
from .runtime import COMPONENT, ROOT, STATE, engine_command, engine_environment, normalize_environment


def build_config(env: dict[str, str]) -> dict:
    config = json.loads((COMPONENT / "config.json").read_text(encoding="utf-8"))
    selected = select_provider(env)
    config["model"] = config["small_model"] = f"{selected.engine_id}/{selected.model}"
    # Agent-level selection takes precedence over a session's previous model.
    # Changing providers must also work when continuing an existing conversation.
    config["agent"]["cad"]["model"] = config["model"]
    provider = copy.deepcopy(config["provider"][selected.engine_id])
    definition = copy.deepcopy(next(iter(provider["models"].values())))
    definition["name"] = selected.model
    definition["limit"] = {"context": selected.context_limit, "output": selected.output_limit}
    provider["models"] = {selected.model: definition}
    provider["options"]["baseURL"] = selected.base_url
    provider["options"]["apiKey"] = "{env:" + selected.key_name + "}"
    config["provider"] = {selected.engine_id: provider}
    config["enabled_providers"] = [selected.engine_id]
    config["agent"]["cad"]["prompt"] = (COMPONENT / "prompts/cad.txt").read_text(encoding="utf-8")
    config["mcp"]["freecad"]["command"] = ["bash", str(ROOT / "freecad/mcp.sh")]
    config["plugin"] = [(COMPONENT / "plugins/task-context.mjs").resolve().as_uri()]
    # The service runs in .runtime/workspace; discover the project skill
    # directory via an absolute path instead of resolving against that CWD.
    config["skills"] = {"paths": [str((COMPONENT / "skills").resolve())]}
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate configuration without printing secrets or starting the server")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    env = normalize_environment(dict(os.environ))
    try:
        required = required_credentials(env)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    if required:
        raise SystemExit("缺少环境变量：" + ", ".join(required))
    env.setdefault("CAD_API_BASE_URL", "http://127.0.0.1:8000")
    endpoint = urlparse(env.get("AGENT_BASE_URL", "http://127.0.0.1:4096"))
    if endpoint.scheme != "http" or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("首期智能体必须绑定本机 HTTP 地址")
    try:
        command = engine_command(env)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    if not (ROOT / ".venv/bin/python").is_file():
        raise SystemExit("缺少 .venv，请先运行 uv sync --frozen")
    if not (ROOT / "freecad/mcp/.venv/bin/freecad-mcp").is_file():
        raise SystemExit("请先运行 bash freecad/install.sh 安装 FreeCAD MCP")
    config = build_config(env)
    if args.check:
        print("智能体配置有效；运行方式=" + env.get("AGENT_RUNTIME", "release") + "；模型=" + config["model"] + "；启用项目 FreeCAD MCP 与 site-layout 技能")
        return
    try:
        env, network = prepare_model_network(env)
    except ModelNetworkError as exc:
        raise SystemExit(str(exc)) from None
    print(network, flush=True)
    env = engine_environment(env, config)
    workspace = STATE / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    os.chdir(workspace)
    print(f"启动智能体：{endpoint.hostname}:{endpoint.port or 4096}；模型={config['model']}", flush=True)
    os.execve(command[0], [*command, "serve", "--hostname", endpoint.hostname or "127.0.0.1", "--port", str(endpoint.port or 4096)], env)


if __name__ == "__main__":
    main()
