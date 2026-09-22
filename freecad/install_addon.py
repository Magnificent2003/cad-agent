"""Run using FreeCAD's Python to link the editable addon into its own profile."""
import json
import os
from pathlib import Path

import FreeCAD

component = Path(os.environ["CAD_FREECAD_ROOT"]).resolve()
source = component / "mcp" / "addon" / "FreeCADMCP"
profile = Path(FreeCAD.getUserAppDataDir()).resolve()
state = component.parent / ".runtime" / "freecad"
if not profile.is_relative_to(state.resolve()):
    raise RuntimeError(f"FreeCAD 配置未隔离到项目目录，停止安装插件：{profile}")
if not (source / "InitGui.py").is_file():
    raise RuntimeError("缺少原版插件源码，请初始化 freecad/mcp 子模块")
destination = profile / "Mod" / "FreeCADMCP"
destination.parent.mkdir(parents=True, exist_ok=True)
if destination.is_symlink():
    if destination.resolve() != source:
        raise RuntimeError(f"插件目录已指向其他源码，请检查：{destination}")
elif destination.exists():
    raise RuntimeError(f"插件目录已存在，未覆盖：{destination}")
else:
    destination.symlink_to(source, target_is_directory=True)
report = {"version": FreeCAD.Version(), "user_data": str(profile), "addon_link": str(destination), "addon_source": str(source)}
(state / "installation.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
