"""Start the pinned local OpenCode with the project's CAD-only configuration."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def build_config(env: dict[str, str]) -> dict:
    config = json.loads((ROOT / "opencode.json").read_text(encoding="utf-8"))
    model = env.get("DEEPSEEK_MODEL", "deepseek-flash").strip() or "deepseek-flash"
    config["model"] = config["small_model"] = f"deepseek-cad/{model}"
    provider = config["provider"]["deepseek-cad"]
    definition = copy.deepcopy(provider["models"]["deepseek-flash"])
    definition["name"] = model
    provider["models"] = {model: definition}
    provider["options"]["baseURL"] = env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    config["agent"]["cad"]["prompt"] = (ROOT / "prompts" / "cad-agent.txt").read_text(encoding="utf-8")
    config["mcp"]["cad"]["command"] = [str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "cad_mcp" / "launcher.py")]
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="validate configuration without printing secrets or starting the server")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    env = dict(os.environ)
    required = [name for name in ("DEEPSEEK_API_KEY", "CAD_INTERNAL_TOKEN", "OPENCODE_SERVER_PASSWORD") if not env.get(name)]
    if required:
        raise SystemExit("缺少环境变量：" + ", ".join(required))
    env.setdefault("CAD_API_BASE_URL", "http://127.0.0.1:8000")
    env.setdefault("OPENCODE_SERVER_USERNAME", "opencode")
    endpoint = urlparse(env.get("OPENCODE_BASE_URL", "http://127.0.0.1:4096"))
    if endpoint.scheme != "http" or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise SystemExit("首期 OpenCode 必须绑定本机 HTTP 地址")
    local_binary = ROOT / ".tools" / "opencode" / "node_modules" / ".bin" / "opencode"
    executable = str(local_binary) if local_binary.is_file() else shutil.which("opencode")
    if not executable:
        raise SystemExit("未安装 OpenCode，请先运行项目环境安装步骤")
    if not (ROOT / ".venv" / "bin" / "python").is_file():
        raise SystemExit("缺少 .venv，请先运行 uv sync --frozen")
    config = build_config(env)
    if args.check:
        print("OpenCode 配置有效；模型=" + config["model"] + "；CAD MCP 已限定工具权限")
        return
    runtime = ROOT / ".runtime" / "opencode"
    for name, subdir in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
        location = runtime / subdir
        location.mkdir(parents=True, exist_ok=True)
        env[name] = str(location)
    workspace = runtime / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, ensure_ascii=False)
    env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "true"
    env["OPENCODE_DISABLE_CLAUDE_CODE"] = "true"
    env["OPENCODE_DISABLE_DEFAULT_PLUGINS"] = "true"
    env["PATH"] = str(ROOT / ".tools" / "node" / "bin") + os.pathsep + env.get("PATH", "")
    os.chdir(workspace)
    print(f"启动 OpenCode：{endpoint.hostname}:{endpoint.port or 4096}；模型={config['model']}", flush=True)
    os.execve(executable, [executable, "serve", "--hostname", endpoint.hostname or "127.0.0.1", "--port", str(endpoint.port or 4096)], env)


if __name__ == "__main__":
    main()
