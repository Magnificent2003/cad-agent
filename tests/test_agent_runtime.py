"""Public agent settings and the boundary to the current upstream engine."""
import base64
import json
import os

import httpx
import pytest

from agent import launcher, runtime
from agent.client import AgentClient
from backend.app.config import Settings


def test_agent_settings_accept_legacy_dotenv_and_prefer_new_names(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith(("AGENT_", "OPENCODE_")):
            monkeypatch.delenv(key)
    legacy = tmp_path / "legacy-test.env"
    legacy.write_text(
        "OPENCODE_BASE_URL=http://127.0.0.1:5096\n"
        "OPENCODE_SERVER_USERNAME=legacy-test-user\n"
        "OPENCODE_SERVER_PASSWORD=fake-legacy-password\n"
    )
    settings = Settings(_env_file=legacy)
    assert settings.agent_base_url == "http://127.0.0.1:5096"
    assert settings.agent_server_username == "legacy-test-user"
    assert settings.agent_server_password.get_secret_value() == "fake-legacy-password"
    monkeypatch.setenv("AGENT_BASE_URL", "http://127.0.0.1:6096")
    monkeypatch.setenv("AGENT_SERVER_USERNAME", "new-test-user")
    monkeypatch.setenv("AGENT_SERVER_PASSWORD", "fake-new-password")
    current = Settings(_env_file=legacy)
    assert current.agent_base_url == "http://127.0.0.1:6096"
    assert current.agent_server_username == "new-test-user"
    assert current.agent_server_password.get_secret_value() == "fake-new-password"


def test_project_config_exposes_only_cad_mcp_and_preserves_model_settings():
    config = launcher.build_config({
        "DEEPSEEK_MODEL": "test-model",
        "DEEPSEEK_BASE_URL": "https://model.example.invalid/v1/",
        "DEEPSEEK_API_KEY": "fake-model-key",
    })
    assert config["permission"] == {"*": "deny", "cad_*": "allow"}
    assert list(config["mcp"]) == ["cad"]
    assert config["mcp"]["cad"]["command"] == [
        str(runtime.ROOT / ".venv/bin/python"), str(runtime.ROOT / "cad_mcp/launcher.py")
    ]
    assert config["model"] == config["small_model"] == "deepseek-cad/test-model"
    provider = config["provider"]["deepseek-cad"]
    assert list(provider["models"]) == ["test-model"]
    assert provider["options"]["baseURL"] == "https://model.example.invalid/v1"
    assert provider["options"]["apiKey"] == "{env:DEEPSEEK_API_KEY}"
    assert config["agent"]["cad"]["prompt"]


def test_release_and_source_commands_require_their_own_installation(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    monkeypatch.setattr(runtime, "COMPONENT", tmp_path / "agent")
    with pytest.raises(RuntimeError, match="agent/install.sh"):
        runtime.engine_command({})
    release = tmp_path / "agent/node_modules/.bin/opencode"
    release.parent.mkdir(parents=True)
    release.touch()
    assert runtime.engine_command({}) == [str(release)]
    with pytest.raises(RuntimeError, match="--source"):
        runtime.engine_command({"AGENT_RUNTIME": "source"})
    bun = tmp_path / ".tools/bun/bin/bun"
    bun.parent.mkdir(parents=True)
    bun.touch()
    source = tmp_path / "agent/engine/packages/opencode"
    (source / "src").mkdir(parents=True)
    (source / "src/index.ts").touch()
    assert runtime.engine_command({"AGENT_RUNTIME": "source"}) == [
        str(bun), "run", "--cwd", str(source), "src/index.ts"
    ]
    with pytest.raises(ValueError, match="release.*source"):
        runtime.engine_command({"AGENT_RUNTIME": "invalid"})


def test_engine_boundary_maps_credentials_and_isolates_runtime_directories(tmp_path):
    original = {
        "PATH": "/test/bin", "OPENCODE_SERVER_USERNAME": "legacy-test-user",
        "OPENCODE_SERVER_PASSWORD": "fake-legacy-password",
        "AGENT_SERVER_PASSWORD": "fake-new-password", "CAD_INTERNAL_TOKEN": "fake-cad-token",
        "DEEPSEEK_API_KEY": "fake-model-key",
    }
    result = runtime.engine_environment(original, {"mcp": {"cad": {}}}, runtime=tmp_path)
    assert result["AGENT_SERVER_USERNAME"] == result["OPENCODE_SERVER_USERNAME"] == "legacy-test-user"
    assert result["AGENT_SERVER_PASSWORD"] == result["OPENCODE_SERVER_PASSWORD"] == "fake-new-password"
    assert result["CAD_INTERNAL_TOKEN"] == "fake-cad-token"
    assert result["DEEPSEEK_API_KEY"] == "fake-model-key"
    assert json.loads(result["OPENCODE_CONFIG_CONTENT"]) == {"mcp": {"cad": {}}}
    for flag in ("PROJECT_CONFIG", "CLAUDE_CODE", "DEFAULT_PLUGINS"):
        assert result["OPENCODE_DISABLE_" + flag] == "true"
    for name, suffix in (("CONFIG", "config"), ("DATA", "data"), ("CACHE", "cache"), ("STATE", "state")):
        assert result[f"XDG_{name}_HOME"] == str(tmp_path / suffix)
        assert (tmp_path / suffix).is_dir()
    assert original["OPENCODE_SERVER_PASSWORD"] == "fake-legacy-password"
    assert "XDG_CONFIG_HOME" not in original


def test_launcher_source_mode_uses_engine_cwd_and_separate_session_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    monkeypatch.setattr(runtime, "COMPONENT", tmp_path / "agent")
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    state = tmp_path / ".runtime/agent"
    monkeypatch.setattr(launcher, "STATE", state)
    for relative in (".venv/bin/python", ".tools/bun/bin/bun", "agent/engine/packages/opencode/src/index.ts"):
        file = tmp_path / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch()
    monkeypatch.setattr(launcher, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(launcher, "build_config", lambda env: {"model": "test/model"})
    monkeypatch.setattr(launcher, "engine_environment", lambda env, config: runtime.engine_environment(env, config, runtime=state))
    monkeypatch.setattr("sys.argv", ["agent.launcher"])
    monkeypatch.setattr(launcher.os, "environ", {
        "AGENT_RUNTIME": "source", "AGENT_BASE_URL": "http://127.0.0.1:5096",
        "AGENT_SERVER_PASSWORD": "fake-agent-password", "CAD_INTERNAL_TOKEN": "fake-cad-token",
        "DEEPSEEK_API_KEY": "fake-model-key",
    })
    observed = {}
    monkeypatch.setattr(launcher.os, "chdir", lambda path: observed.update(workspace=path))
    monkeypatch.setattr(launcher.os, "execve", lambda executable, args, env: observed.update(executable=executable, args=args, env=env))
    launcher.main()
    assert observed["workspace"] == state / "workspace"
    assert observed["workspace"].is_dir()
    assert observed["args"][:5] == [str(tmp_path / ".tools/bun/bin/bun"), "run", "--cwd", str(tmp_path / "agent/engine/packages/opencode"), "src/index.ts"]
    assert observed["args"][5:] == ["serve", "--hostname", "127.0.0.1", "--port", "5096"]
    assert observed["env"]["OPENCODE_SERVER_PASSWORD"] == "fake-agent-password"


async def test_client_binds_http_calls_to_session_workspace(tmp_path):
    workspace = str(tmp_path / "workspace with spaces")
    expected = base64.b64encode(b"test-agent-user:fake-agent-password").decode()
    calls = []

    def upstream(request):
        assert request.url.params["directory"] == workspace
        assert request.headers["authorization"] == f"Basic {expected}"
        calls.append(request.url.path)
        if request.url.path == "/event":
            return httpx.Response(200, text='data: {"type":"session.updated","properties":{"info":{"id":"test-session"}}}\n\n')
        return httpx.Response(200, json={"id": "test-session"})

    settings = Settings(_env_file=None, agent_server_username="test-agent-user", agent_server_password="fake-agent-password")
    async with AgentClient(settings, directory=workspace, transport=httpx.MockTransport(upstream)) as client:
        await client.create_session()
        await client.messages("test-session")
        assert len([event async for event in client.events()]) == 1
    assert calls == ["/session", "/session/test-session/message", "/event"]


def test_empty_new_env_uses_existing_legacy_password(monkeypatch):
    from agent.runtime import normalize_environment
    from backend.app.config import Settings

    monkeypatch.setenv("AGENT_SERVER_PASSWORD", "")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "fake-existing-secret")
    settings = Settings(_env_file=None)
    mapped = normalize_environment({"AGENT_SERVER_PASSWORD": "", "OPENCODE_SERVER_PASSWORD": "fake-existing-secret"})
    assert settings.agent_server_password.get_secret_value() == mapped["AGENT_SERVER_PASSWORD"]
