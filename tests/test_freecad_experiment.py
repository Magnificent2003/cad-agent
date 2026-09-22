"""The general FreeCAD Python experiment must stay separate from the CAD app."""
import json

import pytest

from agent import freecad_experiment as experiment
from agent import launcher, runtime


@pytest.fixture
def experiment_paths(tmp_path, monkeypatch):
    state = tmp_path / ".runtime/freecad-agent"
    for key, path in {
        "STATE": state,
        "WORKSPACE": state / "workspace",
        "FILES": state / "files",
        "INPUTS": state / "files/input",
        "OUTPUTS": state / "files/output",
    }.items():
        monkeypatch.setattr(experiment, key, path)
    experiment.INPUTS.mkdir(parents=True)
    source = experiment.INPUTS / "copy.dxf"
    source.write_text("test fixture")
    return source


def test_experiment_uses_original_freecad_mcp_and_replaces_old_tool_restrictions(experiment_paths):
    env = {"DEEPSEEK_MODEL": "test-model", "DEEPSEEK_BASE_URL": "https://model.example.invalid/v1/"}
    config = experiment.build_config(env, [experiment_paths])
    assert config["permission"] == {"*": "deny", "freecad_*": "allow"}
    assert list(config["mcp"]) == ["freecad"]
    assert config["mcp"]["freecad"]["command"] == ["bash", str(runtime.ROOT / "freecad/mcp.sh")]
    assert config["model"] == "deepseek-cad/test-model"
    assert config["provider"]["deepseek-cad"]["options"]["apiKey"] == "{env:DEEPSEEK_API_KEY}"
    prompt = config["agent"]["cad"]["prompt"]
    assert str(experiment_paths) in prompt
    assert str(experiment.OUTPUTS) in prompt
    assert "execute_code" in prompt
    assert "expected_revision" not in prompt
    assert "{{" not in prompt
    # Building an experiment must not mutate the normal service configuration.
    normal = launcher.build_config(env)
    assert list(normal["mcp"]) == ["cad"]
    assert normal["permission"] == {"*": "deny", "cad_*": "allow"}


def test_experiment_rejects_original_paths_and_symlinks_to_originals(experiment_paths, tmp_path):
    original = tmp_path / "original.dwg"
    original.write_text("original")
    link = experiment.INPUTS / "original-link.dwg"
    link.symlink_to(original)
    for path in (original, link):
        with pytest.raises(ValueError, match="不能直接使用原图"):
            experiment.experiment_inputs([path])
    assert experiment.experiment_inputs([experiment_paths, experiment_paths]) == [experiment_paths]


def test_experiment_does_not_inherit_external_config_permissions_or_cad_token(experiment_paths):
    env = {
        "DEEPSEEK_API_KEY": "fake-model-key",
        "AGENT_SERVER_PASSWORD": "fake-password",
        "AGENT_BASE_URL": "http://127.0.0.1:4096",
        "CAD_INTERNAL_TOKEN": "fake-cad-token",
        "OPENCODE_CONFIG": "/external/config.json",
        "OPENCODE_CONFIG_DIR": "/external/config",
        "OPENCODE_CONFIG_CONTENT": '{"mcp":{"cad":{}}}',
        "OPENCODE_PERMISSION": '{"*":"allow"}',
    }
    config = experiment.build_config(env, [experiment_paths])
    result = experiment.experiment_environment(env, config, 4097)
    assert result["AGENT_BASE_URL"] == "http://127.0.0.1:4097"
    assert result["DEEPSEEK_API_KEY"] == "fake-model-key"
    assert result["OPENCODE_SERVER_PASSWORD"] == "fake-password"
    assert result["OPENCODE_TEST_HOME"] == str(experiment.STATE / "home")
    for name in ("CAD_INTERNAL_TOKEN", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_PERMISSION"):
        assert name not in result
    assert json.loads(result["OPENCODE_CONFIG_CONTENT"]) == config
    for name, suffix in (("CONFIG", "config"), ("DATA", "data"), ("CACHE", "cache"), ("STATE", "state")):
        assert result[f"XDG_{name}_HOME"] == str(experiment.STATE / suffix)
    assert env["AGENT_BASE_URL"].endswith(":4096")
    assert "XDG_DATA_HOME" not in env


def test_check_does_not_start_agent_or_require_normal_backend_token(experiment_paths, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(experiment, "ROOT", tmp_path)
    for relative in ("freecad/mcp.sh", "freecad/mcp/.venv/bin/freecad-mcp"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    monkeypatch.setattr(experiment, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(experiment, "engine_command", lambda env: ["/fake/engine"])
    monkeypatch.setattr(experiment.os, "environ", {
        "DEEPSEEK_API_KEY": "fake-model-key",
        "AGENT_SERVER_PASSWORD": "fake-password",
    })
    monkeypatch.setattr("sys.argv", ["agent.freecad_experiment", "--input", str(experiment_paths), "--check"])
    experiment.main()
    output = capsys.readouterr().out
    assert "端口=4097" in output
    assert "fake-model-key" not in output
    assert "fake-password" not in output
    assert not experiment.WORKSPACE.exists()
    assert not experiment.OUTPUTS.exists()


def test_launcher_rejects_existing_agent_port(experiment_paths, monkeypatch):
    monkeypatch.setattr(experiment, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(experiment.os, "environ", {
        "DEEPSEEK_API_KEY": "fake-model-key",
        "AGENT_SERVER_PASSWORD": "fake-password",
        "AGENT_BASE_URL": "http://127.0.0.1:4097",
    })
    monkeypatch.setattr("sys.argv", ["agent.freecad_experiment", "--input", str(experiment_paths)])
    with pytest.raises(SystemExit, match="不能与现有智能体端口相同"):
        experiment.main()
