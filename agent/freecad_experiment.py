"""Run the upstream FreeCAD MCP in a separate, local agent experiment.

The file scope in the prompt is an operating convention, not an OS sandbox:
upstream execute_code intentionally exposes the FreeCAD Python environment.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from .launcher import build_config as build_base_config
from .runtime import COMPONENT, ROOT, engine_command, engine_environment, normalize_environment

STATE = ROOT / ".runtime" / "freecad-agent"
WORKSPACE = STATE / "workspace"
FILES = STATE / "files"
INPUTS = FILES / "input"
OUTPUTS = FILES / "output"


def experiment_inputs(paths: list[str | Path]) -> list[Path]:
    """Require explicitly supplied local copies, resolving symlinks first."""
    inputs = []
    for value in paths:
        path = Path(value).expanduser().resolve()
        if not path.is_relative_to(INPUTS.resolve()):
            raise ValueError(f"请先将待测图纸复制到 {INPUTS}，不能直接使用原图")
        if not path.is_file() or path.suffix.lower() not in {".dxf", ".dwg", ".fcstd"}:
            raise ValueError(f"实验输入必须是已存在的 DXF、DWG 或 FCStd 文件：{path}")
        if path not in inputs:
            inputs.append(path)
    if not inputs:
        raise ValueError("至少通过 --input 指定一份待测图纸副本")
    return inputs


def build_config(env: dict[str, str], inputs: list[Path]) -> dict:
    config = build_base_config(env)
    prompt = (COMPONENT / "prompts/freecad-experiment.txt").read_text(encoding="utf-8")
    prompt = prompt.replace("{{INPUT_FILES}}", json.dumps([str(path) for path in inputs], ensure_ascii=False))
    prompt = prompt.replace("{{OUTPUT_DIRECTORY}}", str(OUTPUTS.resolve()))
    config["agent"] = {
        "cad": {"description": "FreeCAD 文件操作实验", "mode": "primary", "prompt": prompt}
    }
    config["mcp"] = {
        "freecad": {
            "type": "local",
            "command": ["bash", str(ROOT / "freecad/mcp.sh")],
            "enabled": True,
            "timeout": 300000,
        }
    }
    config["permission"] = {"*": "deny", "freecad_*": "allow"}
    return config


def experiment_environment(env: dict[str, str], config: dict, port: int) -> dict[str, str]:
    isolated = dict(env)
    # Explicit upstream overrides must not reintroduce the normal CAD MCP or
    # alter the experiment's tool policy. Credentials/model settings are reused.
    for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT", "OPENCODE_PERMISSION"):
        isolated.pop(key, None)
    isolated.pop("CAD_INTERNAL_TOKEN", None)
    isolated["AGENT_BASE_URL"] = f"http://127.0.0.1:{port}"
    # Upstream still discovers ~/.opencode when project config is disabled.
    # Its dedicated test-home override isolates that discovery without changing
    # the user's HOME or the FreeCAD process's own configuration.
    isolated["OPENCODE_TEST_HOME"] = str(STATE / "home")
    (STATE / "home").mkdir(parents=True, exist_ok=True)
    return engine_environment(isolated, config, runtime=STATE)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", required=True, help="copy under .runtime/freecad-agent/files/input/; repeat for multiple drawings")
    parser.add_argument("--port", type=int, default=4097, help="separate loopback agent port (default: 4097)")
    parser.add_argument("--check", action="store_true", help="validate setup without starting the agent or making model requests")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    env = normalize_environment(dict(os.environ))
    required = [name for name in ("DEEPSEEK_API_KEY", "AGENT_SERVER_PASSWORD") if not env.get(name)]
    if required:
        raise SystemExit("缺少环境变量：" + ", ".join(required))
    try:
        normal_port = urlparse(env.get("AGENT_BASE_URL", "http://127.0.0.1:4096")).port or 4096
        if not 1024 <= args.port <= 65535 or args.port == normal_port:
            raise ValueError("实验端口必须为 1024–65535，且不能与现有智能体端口相同")
        inputs = experiment_inputs(args.input)
        if not OUTPUTS.resolve().is_relative_to(FILES.resolve()):
            raise ValueError("实验输出目录不能链接到实验 files 目录之外")
        command = engine_command(env)
        for relative in ("freecad/mcp.sh", "freecad/mcp/.venv/bin/freecad-mcp"):
            if not (ROOT / relative).is_file():
                raise ValueError(f"缺少 {relative}，请先安装 FreeCAD MCP")
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    config = build_config(env, inputs)
    if args.check:
        print(f"FreeCAD 实验配置有效；端口={args.port}；模型={config['model']}；输入副本={len(inputs)}；仅启用 FreeCAD MCP")
        return
    env = experiment_environment(env, config, args.port)
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    os.chdir(WORKSPACE)
    print(f"启动 FreeCAD 实验智能体：127.0.0.1:{args.port}；输入副本={len(inputs)}；输出={OUTPUTS}", flush=True)
    print("实验目录约束由提示词约定；execute_code 不提供操作系统文件沙箱。", flush=True)
    os.execve(command[0], [*command, "serve", "--hostname", "127.0.0.1", "--port", str(args.port)], env)


if __name__ == "__main__":
    main()
