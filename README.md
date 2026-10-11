# CAD Agent

在网页提出图纸任务，智能体通过项目维护的 FreeCAD MCP 编写、执行和修正 Python，生成新的图纸文件。原图保留，成果作为同一项目的新版本显示。没有旧的固定操作备用链路。

## 启动

```bash
bash scripts/dev.sh
```

打开 **http://127.0.0.1:5173**。这个命令一起启动 FreeCAD（默认 9875）、后端（8000）、智能体（4096）和网页（5173）；按 **Ctrl+C** 一起停止。

启动前会关闭占用这些地址与端口的旧服务，包括 WSL 对应的 Windows 监听进程；因此其他软件占用这些端口时也可能被关闭。终端会列出处理对象。FreeCAD 或业务后端的占用无法定位时，自动选择后续空闲内部端口，并同步插件、MCP、后端及网页代理；网页仍为 5173。其他无法清理的占用会记录在 `.runtime/startup-errors.log`。`FREECAD_RPC_PORT` 可设置首选内部端口，通常无需修改。

项目专用 FreeCAD 启动宏会跳过 Document Recovery 恢复弹窗，保留恢复文件和自动保存。其他模态弹窗会返回具体阻塞原因，避免长时间空等；修改后需重启服务。

模型连接由 `.env` 配置，修改后重启服务生效；做模型对比时建议新建对话：

| `LLM_PROVIDER` | 使用的配置 |
| --- | --- |
| `deepseek` | `DEEPSEEK_API_KEY`、`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL` |
| `aiapi` | `AIAPI_API_KEY`、`AIAPI_BASE_URL`、`AIAPI_MODEL` |

只调用所选厂商，旧会话续聊也采用当前选择，不自动回退。对应的 `DEEPSEEK_NETWORK_MODE` / `AIAPI_NETWORK_MODE` 可设为 `auto`（先直连，再尝试已有代理）、`direct` 或 `proxy`。选择逻辑在 `agent/provider.py`。

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

对话现在按每轮显示一个“处理过程”：执行时展开动作摘要、工具反馈、耗时和异常，结束后自动折叠，可点击回看。界面在 `frontend/src/components/ExecutionTrace.tsx`，反馈摘要在 `agent/progress.py`；过程文字来自助手公开的进度说明，不增加额外模型请求。验证记录见 [恢复弹窗与执行过程](dev_log_0928/恢复弹窗与执行过程0928.md)。

一次任务的路径是：**网页提问 → 后端准备文件 → 模型选择动作 → MCP 执行 → 返回结果／错误 → 模型继续操作或答复 → 后端收集新文件并预览**。

`agent/prompts/cad.txt` 决定工作方式；`agent/config.json` 管理模型与 MCP 配置。修改 MCP 源码后重新一键启动即可。修改引擎源码时使用 `AGENT_RUNTIME=source bash scripts/dev.sh`，首次需执行 `bash agent/install.sh --source`。

场布新增了空间事实 A、几何约束 B 和中文 Skill，均由现有 MCP 提供，不增加端口：

- `cad_domain/spatial_autonomy.py`、`spatial_constraints.py`：从当前 DXF 提取占地和参照轴，检查候选位置。
- `cad_domain/site_layout_rules.yaml`：中文面积规则，经 `cad_site_rules` 交给模型；不提供预设布局。
- `agent/skills/site-layout/SKILL.md`：模型遇到场布任务时自主加载，使用反馈修正，再由 `cad_layout/cad_review` 保存和检查实际 DXF。

已用原始 v0009 和一句“请你进行场地布置”验证真实 DeepSeek → Skill → MCP → DXF → 后端复核 → 网页新版本与下载。未输入人工建筑名单或历史场布答案。此结果证明调用与交付链路已接通，**不代表完整施工方案合格**；效果、耗时、未达标项及原始成果见 [阶段二实验](dev_log_0929/空间能力Agent接入阶段二0929.md)。启动仍用 `bash scripts/dev.sh`，无需另外激活模块。


## 文件与日志在哪里

- 原图、历史版本、成果：`data/drawings/`、`data/artifacts/`。
- 每轮任务：`data/agent-tasks/<任务ID>/`，包括 `context.json`、`input/`、`scratch/`、`output/`。
- **每轮可读记录：`data/agent-runs/<任务ID>.md`**；打开 `data/agent-runs/latest.md` 可跳到最近一轮。执行时持续更新，含实际模型输出、供应商返回的可见推理、工具完整参数／返回值、异常和自动续轮；实现位于 `agent/trace.py`。图片另存，凭据脱敏；引擎先行截断的输出会保留其文件位置，未返回的内部推理无法记录。
- 智能体启动与模型连接日志：`.runtime/agent/server.log`。
- 完整会话、执行代码和工具反馈：`.runtime/agent/data/opencode/opencode.db`（SQLite）；引擎文本日志在同目录的 `log/opencode.log`。
- FreeCAD 日志：`.runtime/freecad/gui.log`；后端日志在启动终端。

`data/` 和 `.runtime/agent/` 是工作数据，应保留。`.cache/` 可以重新生成；`.tools/`、`.venv/` 和组件 `runtime/` 是运行环境，删除后需要重新安装。真实图纸、字体和凭据不提交 Git。

修改 `CAD_DATA_DIR` 后，上述 `data/` 路径随之改变。`dev_log_*/` 只同步 Markdown 记录和复现实验的脚本，生成的图纸、图片、PDF、事实 JSON 与原始运行数据留在本机。

模型切换与同题工程复测见 [1011 开发记录](dev_log_1011/模型切换与阶段二复测1011.md)。

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
