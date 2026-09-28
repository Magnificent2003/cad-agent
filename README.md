# CAD Agent

在网页提出图纸任务，智能体通过项目维护的 FreeCAD MCP 编写、执行和修正 Python，生成新的图纸文件。原图保留，成果作为同一项目的新版本显示。没有旧的固定操作备用链路。

## 启动

```bash
bash scripts/dev.sh
```

打开 **http://127.0.0.1:5173**。这个命令一起启动 FreeCAD（9875）、后端（8000）、智能体（4096）和网页（5173）；按 **Ctrl+C** 一起停止。

启动前会关闭占用这些地址与端口的旧服务，包括 WSL 对应的 Windows 监听进程；因此其他软件占用这些端口时也可能被关闭。终端会列出处理对象。系统保留或权限不足的占用会明确报错，记录在 `.runtime/startup-errors.log`。

模型连接由 `.env` 配置。`DEEPSEEK_NETWORK_MODE=auto` 表示启动时先尝试直连，失败再尝试已有代理；也可设为 `direct` 或 `proxy`。不修改 API 地址与密钥，终端／智能体日志会说明选用的连接方式。

## 代码在哪里，分别做什么

| 位置 | 作用 |
| --- | --- |
| `frontend/` | 工程项目栏、对话、图纸版本、SVG 预览和文件下载 |
| `backend/app/` | 接收图纸、准备任务副本、收集成果和生成预览 |
| `agent/` | 模型配置、提示词、任务上下文和自主调用工具的循环；引擎源码在 `agent/engine/` |
| `freecad/mcp/` | 本仓库维护的 MCP 服务和 FreeCAD 插件，执行模型编写的代码并返回结果或错误 |
| `cad_domain/` | 实验性场布辅助：图纸读取、参数化布局、实际 DXF 几何复核；由同一 MCP 提供 |
| `cad_converter/` | ODA 格式转换，供 DWG 导入和导出使用 |
| `scripts/dev.py`、`scripts/ports.py` | 一键启动、回收冲突端口、退出清理 |
| `agent/network.py` | 检查并选择模型连接路径，避免失效的本地代理阻断模型 |

一次任务的路径是：**网页提问 → 后端准备文件 → 模型选择动作 → MCP 执行 → 返回结果／错误 → 模型继续操作或答复 → 后端收集新文件并预览**。

`agent/prompts/cad.txt` 决定工作方式；`agent/config.json` 管理模型与 MCP 配置。修改 MCP 源码后重新一键启动即可。修改引擎源码时使用 `AGENT_RUNTIME=source bash scripts/dev.sh`，首次需执行 `bash agent/install.sh --source`。

场布辅助模块已恢复为实验版本：`cad_inspect` 读取实际 DXF，`cad_layout` 按明确条件布局，`cad_review` 回读成果并复核；后端发布前也执行检查。随 `bash scripts/dev.sh` 自动加载，无需自然语言激活或新增端口。模型仍可选用通用 Python；若要专测参数化布局，可明确要求“使用 cad_layout 布置，并用 cad_review 的 site_layout 模式复核”。

已有真实场布对照显示局部改善，但复杂建筑识别、检查范围完整性和塔吊覆盖尚不可靠，检查通过不代表工程验收通过。恢复范围和测试用法见 [模块恢复记录](dev_log/场布模块恢复0928.md)，原始对照及失败证据见 [工程复测记录](dev_log/工程复测与回滚0928.md)。试验和失败记录统一留在 `dev_log/`。

## 文件与日志在哪里

- 原图、历史版本、成果：`data/drawings/`、`data/artifacts/`。
- 每轮任务：`data/agent-tasks/<任务ID>/`，包括 `context.json`、`input/`、`scratch/`、`output/`。
- 智能体启动与模型连接日志：`.runtime/agent/server.log`。
- 完整会话、执行代码和工具反馈：`.runtime/agent/data/opencode/opencode.db`（SQLite）；引擎文本日志在同目录的 `log/opencode.log`。
- FreeCAD 日志：`.runtime/freecad/gui.log`；后端日志在启动终端。

`data/` 和 `.runtime/agent/` 是工作数据，应保留。`.cache/` 可以重新生成；`.tools/`、`.venv/` 和组件 `runtime/` 是运行环境，删除后需要重新安装。真实图纸、字体和凭据不提交 Git。

## 新主机安装

需要 Linux x86_64／WSL2，并有可用的桌面或 WSLg；只安装 Python 包不够。

```bash
bash scripts/bootstrap.sh
bash cad_converter/install.sh
bash freecad/install.sh
```

按 `.env.example` 在 `.env` 填入模型连接和认证配置，再运行启动命令。新增环境变量时同步维护这两份文件，不提交 `.env`。

FreeCAD 专用 Python 依赖写在 `freecad/requirements-python.txt`，安装到 `freecad/runtime/python-deps/`。新增后运行 `bash freecad/install_python.sh`，再重启全部服务。不要将后端 `.venv` 的包复制到 FreeCAD 自带 Python。

[FreeCAD 安装说明](freecad/README.md) · [环境说明](docs/environment.md) · [论文与研究计划](manuscript/README.md)
