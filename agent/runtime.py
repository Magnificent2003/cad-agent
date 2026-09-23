"""Project paths and the adapter to the current engine's CLI contract."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "agent"
STATE = ROOT / ".runtime" / "agent"
WORKSPACE = STATE / "workspace"


def normalize_environment(env: dict[str, str]) -> dict[str, str]:
    result = dict(env)
    for name in ("BASE_URL", "SERVER_USERNAME", "SERVER_PASSWORD"):
        if not result.get("AGENT_" + name) and result.get("OPENCODE_" + name):
            result["AGENT_" + name] = result["OPENCODE_" + name]
    result.setdefault("AGENT_SERVER_USERNAME", "cad-agent")
    return result


def engine_command(env: dict[str, str]) -> list[str]:
    mode = env.get("AGENT_RUNTIME", "release")
    if mode == "release":
        executable = COMPONENT / "node_modules" / ".bin" / "opencode"
        if not executable.is_file():
            raise RuntimeError("智能体发行程序未安装，请运行 bash agent/install.sh")
        return [str(executable)]
    if mode == "source":
        bun = ROOT / ".tools" / "bun" / "bin" / "bun"
        source = COMPONENT / "engine" / "packages" / "opencode"
        if not bun.is_file() or not (source / "src/index.ts").is_file():
            raise RuntimeError("源码运行环境未安装，请运行 bash agent/install.sh --source")
        return [str(bun), "run", "--cwd", str(source), "src/index.ts"]
    raise ValueError("AGENT_RUNTIME 只能是 release 或 source")


def engine_environment(env: dict[str, str], config: dict, runtime: Path = STATE) -> dict[str, str]:
    result = normalize_environment(env)
    data = Path(result.get("CAD_DATA_DIR") or ROOT / "data").expanduser()
    result["CAD_AGENT_CONTEXT_DIR"] = str(((data if data.is_absolute() else ROOT / data) / "agent-contexts").resolve())
    # Keep upstream-required names in this boundary, out of business modules.
    for key in ("OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_CONFIG_CONTENT", "OPENCODE_PERMISSION", "CAD_INTERNAL_TOKEN"):
        result.pop(key, None)
    # Isolate user config discovery so removed CAD tools cannot return via ~/.opencode.
    result["OPENCODE_TEST_HOME"] = str(runtime / "home")
    (runtime / "home").mkdir(parents=True, exist_ok=True)
    result["OPENCODE_SERVER_USERNAME"] = result["AGENT_SERVER_USERNAME"]
    result["OPENCODE_SERVER_PASSWORD"] = result.get("AGENT_SERVER_PASSWORD", "")
    result["OPENCODE_CONFIG_CONTENT"] = json.dumps(config, ensure_ascii=False)
    # Source builds use the 'local' channel; share the existing session database
    # with the release engine instead of silently opening an empty channel DB.
    result["OPENCODE_DISABLE_CHANNEL_DB"] = "true"
    for flag in ("PROJECT_CONFIG", "CLAUDE_CODE", "DEFAULT_PLUGINS"):
        result["OPENCODE_DISABLE_" + flag] = "true"
    for name, subdir in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
        location = runtime / subdir
        location.mkdir(parents=True, exist_ok=True)
        result[name] = str(location)
    result["PATH"] = os.pathsep.join([str(ROOT / ".tools/bun/bin"), str(ROOT / ".tools/node/bin"), result.get("PATH", "")])
    return result
