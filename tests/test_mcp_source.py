"""A plain main-repository checkout includes the editable MCP and its fixes."""
import json
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_mcp_has_no_nested_git_and_retains_licensed_source():
    source = ROOT / 'freecad/mcp'
    assert not (source / '.git').exists()
    assert not (ROOT / '.gitmodules').exists()
    provenance = json.loads((ROOT / 'freecad/mcp-source.json').read_text())
    assert provenance['upstream']['commit'] == '751974609a401660a58a1772ef16f3afbeba9ba1'
    assert 'MIT License' in (source / 'LICENSE').read_text()
    assert 'Copyright (c) 2025 Shirokuma' in (source / 'LICENSE').read_text()
    for relative in ('src/freecad_mcp/server.py', 'addon/FreeCADMCP/rpc_server/python_execution.py',
                     'tests/test_rpc_python_diagnostics.py', 'pyproject.toml', 'uv.lock'):
        assert (source / relative).is_file()


def test_install_missing_source_fails_before_downloading_or_replacing_it(tmp_path):
    component = tmp_path / 'project/freecad'
    component.mkdir(parents=True)
    shutil.copy2(ROOT / 'freecad/install.sh', component / 'install.sh')
    result = subprocess.run(['bash', str(component / 'install.sh')], capture_output=True, text=True)
    assert result.returncode == 1
    assert '项目 MCP 源码缺失' in result.stderr
    assert not (component / 'mcp').exists()
