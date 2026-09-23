"""Start the project's agent service with its CAD tools and model settings."""
from __future__ import annotations

import argparse
import copy
import json
import os
from urllib.parse import urlparse

from dotenv import load_dotenv

from .runtime import COMPONENT, ROOT, STATE, engine_command, engine_environment, normalize_environment


def build_config(env: dict[str, str]) -> dict:
    config = json.loads((COMPONENT / "config.json").read_text(encoding="utf-8"))
    model = env.get("DEEPSEEK_MODEL", "deepseek-flash").strip() or "deepseek-flash"
    config["model"] = config["small_model"] = f"deepseek-cad/{model}"
    provider = config["provider"]["deepseek-cad"]
    definition = copy.deepcopy(provider["models"]["deepseek-flash"])
    definition["name"] = model
    provider["models"] = {model: definition}
    provider["options"]["baseURL"] = env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    config["agent"]["cad"]["prompt"] = (COMPONENT / "prompts/cad.txt").read_text(encoding="utf-8")
    config["mcp"]["freecad"]["command"] = ["bash", str(ROOT / "freecad/mcp.sh")]
    config["plugin"] = [(COMPONENT / "plugins/task-context.mjs").resolve().as_uri()]
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate configuration without printing secrets or starting the server")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    env = normalize_environment(dict(os.environ))
    required = [name for name in ("DEEPSEEK_API_KEY", "AGENT_SERVER_PASSWORD") if not env.get(name)]
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
        print("智能体配置有效；运行方式=" + env.get("AGENT_RUNTIME", "release") + "；模型=" + config["model"] + "；仅启用原版 FreeCAD MCP")
        return
    env = engine_environment(env, config)
    workspace = STATE / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    os.chdir(workspace)
    print(f"启动智能体：{endpoint.hostname}:{endpoint.port or 4096}；模型={config['model']}", flush=True)
    os.execve(command[0], [*command, "serve", "--hostname", endpoint.hostname or "127.0.0.1", "--port", str(endpoint.port or 4096)], env)


if __name__ == "__main__":
    main()
