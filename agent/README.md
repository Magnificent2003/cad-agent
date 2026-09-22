# 智能体组件

`agent/` 负责对话、模型连接和工具调用。这里既有本项目的配置与接入代码，也有可直接修改的引擎源码。CAD 实体的读取、修改和文件保存由项目的 CAD 服务执行。

## 先看哪些文件

| 文件或目录 | 职责 | 什么时候修改 |
| --- | --- | --- |
| [`launcher.py`](launcher.py) | 读取环境变量，组合模型、提示词和 MCP 配置，启动智能体服务 | 修改启动流程或服务配置方式 |
| [`config.json`](config.json) | 模型能力声明、CAD MCP 连接和工具权限 | 更换模型、调整已接入工具的权限 |
| [`prompts/cad.txt`](prompts/cad.txt) | CAD 助手的工作说明、图纸判断和修改规则 | 改善交互、规划步骤和结果检查 |
| [`client.py`](client.py) | 后端访问智能体的 HTTP 客户端，收发对话及事件 | 修改后端与智能体的通信方式 |
| [`runtime.py`](runtime.py) | 选择发行版或源码模式，管理路径，并适配上游环境变量 | 切换引擎入口、调整运行数据路径 |
| [`engine/`](engine/) | 固定版本的真实 TypeScript 引擎源码，纳入本项目版本控制 | 修改模型循环、会话处理或引擎内部机制 |
| `node_modules/` | 根据 `package-lock.json` 安装的发行程序，不提交 Git | 由安装脚本生成，不在这里开发 |
| [`source.lock.json`](source.lock.json) | 上游来源、固定提交、Bun 版本及校验摘要 | 有计划地升级引擎或调整源码依赖 |

`runtime.py` 是 Python 源码文件；根目录的 `.runtime/agent/` 才是运行时生成的数据目录。两者不是同一个东西。

## 日常启动与源码开发

以下命令都在项目根目录执行。Python、Node.js、`.env` 等基础环境按[项目 README](../README.md)配置。

默认使用发行版：

```bash
bash agent/install.sh
AGENT_RUNTIME=release bash scripts/dev.sh
```

它会运行 `agent/node_modules/` 中固定版本的发行程序，并一起启动本项目后端和前端。默认值也是 `AGENT_RUNTIME=release`，日常使用可直接运行 `bash scripts/dev.sh`。

要开发引擎源码，先安装源码依赖，再切换运行方式：

```bash
bash agent/install.sh --source
AGENT_RUNTIME=source bash scripts/dev.sh
```

`--source` 安装项目内的 Bun 1.3.14，并安装服务端 CLI 所需的工作区依赖，包括 CLI 引用的终端界面包；不安装整套桌面端、控制台网站。已有的 `engine/` 源码不会被下载或覆盖，`bun.lock` 采用冻结安装。修改引擎源码后，需要重启源码模式服务才能看到效果；仍使用 `release` 时，修改不会进入正在运行的发行程序。

两种模式使用相同的服务端口。切换前先在原启动终端按 `Ctrl+C` 停止服务，再执行新命令。也可在 `.env` 中设置 `AGENT_RUNTIME=source`，让后续启动默认使用源码；新增或调整环境变量时同步维护 `.env.example`，示例文件不填写真实密钥。

只检查配置、不启动服务：

```bash
AGENT_RUNTIME=source .venv/bin/python -m agent.launcher --check
```

只启动智能体组件，供后端已经单独启动时调试：

```bash
AGENT_RUNTIME=source .venv/bin/python -m agent.launcher
```

配置检查通过只说明入口和配置可用。实际修改仍需通过服务启动、MCP 连接以及具体对话验证，不能用 `--check` 代替功能测试。

## 源码入口在哪里

先从要改变的行为定位文件，不必通读整个上游仓库：

| 要研究的机制 | 源码入口 |
| --- | --- |
| 一轮会话如何调用模型、推进执行 | [`engine/packages/core/src/session/runner/`](engine/packages/core/src/session/runner/)，CLI 会话接入另见 [`engine/packages/opencode/src/session/`](engine/packages/opencode/src/session/) |
| 内置工具的注册和执行 | [`engine/packages/core/src/tool/`](engine/packages/core/src/tool/) |
| HTTP 路由与会话等请求处理 | [`engine/packages/server/src/routes.ts`](engine/packages/server/src/routes.ts) 和 [`handlers/`](engine/packages/server/src/handlers/) |
| 模型提供商及兼容接口 | [`engine/packages/llm/src/providers/`](engine/packages/llm/src/providers/)，CLI 提供商配置另见 [`engine/packages/opencode/src/provider/`](engine/packages/opencode/src/provider/) |
| MCP 工具发现、连接和调用 | [`engine/packages/opencode/src/mcp/`](engine/packages/opencode/src/mcp/) |

源码模式中，Bun 的 `--cwd` 指向 `engine/packages/opencode/`，用于加载该包的依赖和启动配置。业务对话的工作目录由后端请求中的 `directory` 指定为 `.runtime/agent/workspace/`；修改源码的目录与业务会话的目录各有用途。应用数据统一由 `runtime.py` 配置在 `.runtime/agent/` 下；适配器禁用上游按 channel 分开的默认数据库，让 source/release 都使用原会话数据库。旧路径由兼容链接保留，调试时不要手动删除已有会话数据。

## 增加 CAD 能力改哪里

例如“删除图框外实体并另存一张图”，主要需要补齐以下两层：

1. [`../backend/app/cad/`](../backend/app/cad/) 实现实体筛选、几何处理和生成结果；结合后端任务、版本及图纸管理接口完成保存。
2. [`../cad_mcp/server.py`](../cad_mcp/server.py) 把能力描述成智能体可调用的工具，明确参数、结果与失败原因；再更新提示词中的操作步骤并做实际图纸验证。

提示词能告诉模型如何使用已经存在的工具，但不能直接产生缺失的删除、裁剪或另存功能。`config.json` 中允许调用工具，也不会自动实现这些操作。只有要改变模型如何选择工具、处理上下文或推进多轮执行时，才需要深入修改 `engine/`。

## 上游版本与许可

当前源码来自 [OpenCode v1.18.31](https://github.com/anomalyco/opencode/tree/v1.18.31)，固定提交为 `014614d35b397775e5d397a490fc72368c894ec2`；来源和摘要记录在 `source.lock.json`。源码采用 [MIT 许可](engine/LICENSE)，二次开发时保留原版权与许可声明。

本项目统一使用 `agent` 作为组件名称。引擎内部的原包名、导入路径及部分环境变量仍保留上游命名，避免破坏工作区依赖与接口；这不妨碍我们在 `engine/` 中修改代码。升级上游版本时，要同时核对源码、依赖锁文件、安装清单以及接入层兼容性。

## 本机重构验证

2026-09-22：发行模式和 Bun 源码模式均已通过健康检查、提供商配置读取与 CAD MCP 连接。源码验证使用历史数据库副本，4 个已有会话的消息数量与发行服务一致。该结果不等于完整上游测试或新增 CAD 功能验收；本轮没有通过模型操作真实工程图。
