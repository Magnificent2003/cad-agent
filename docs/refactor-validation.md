# 根目录组件重构记录

日期：2026-09-22。范围是整理代码和运行入口，并分析下一阶段 CAD 能力缺口。

## 目录迁移

| 原位置 | 新位置 |
| --- | --- |
| `scripts/start_opencode.py` | `agent/launcher.py`，入口 `bash agent/start.sh` |
| `opencode.json` | `agent/config.json` |
| `prompts/cad-agent.txt` | `agent/prompts/cad.txt` |
| `backend/app/opencode_client.py` | `agent/client.py`，业务侧名称 `AgentClient` |
| `tooling/opencode/package*.json` | `agent/package*.json` |
| `.tools/opencode/node_modules/` | `agent/node_modules/` |
| 以前没有可改引擎源码 | `agent/engine/`，固定提交 `014614d35b397775e5d397a490fc72368c894ec2` |
| `scripts/install_oda.sh` | `cad_converter/install.sh` |
| `.tools/oda/ODAFileConverter` | 纳入版本控制的 `cad_converter/run.sh` |
| `.tools/oda/squashfs-root/` | `cad_converter/runtime/squashfs-root/` |
| `.runtime/opencode/` | `.runtime/agent/`，旧路径保留兼容符号链接 |

开发工具 Node、uv、Bun 等仍在 `.tools/`，与项目组件源码区分。ODA 为第三方二进制发行程序；没有将它描述为开源源码。

项目环境变量统一为 `AGENT_*`，上游协议变量在 `agent/runtime.py` 转换。后端兼容旧 `OPENCODE_*` 设置；新值为空时与启动器一致地回退旧值，配置脚本升级旧文件时保留服务密码。本机 `.env` 的密钥和用户新增代理设置保留，字段同步到 `.env.example`。

引擎的上游名称与 MIT 许可保留，未对其内部模块做全局改名。源码完整快照可直接编辑，来源、提交、下载摘要与 Bun 版本记录在 `agent/source.lock.json`。Bun 安装只选定服务器 CLI 需要的工作区；版本 1.3.14 使用正向过滤，不能附加 `!./`，否则会选中其他工作区。

## 已验证内容

- 50 项 Python 测试通过，包含原 CAD/任务/鉴权/SSE 用例和 7 项架构/环境兼容用例。
- 5 项浏览器测试通过，TypeScript/Vite 构建、Prettier 和本项目 Ruff 检查通过。
- 发行模式实际服务返回版本 `1.18.31`，模型提供商 `deepseek-cad` 已配置，CAD MCP 已连接。
- 固定 Bun 1.3.14 可直接运行引擎源码。隔离源码服务返回版本 `local`，CAD MCP 和提供商读取通过。
- 源码模式使用迁移前数据库的独立副本验证，4 个现有会话的消息数与发行模式一致，分别为 12、2、3、6。
- 启动器显式让源码和发行版使用同一默认会话数据库，防止上游 `local` channel 打开空数据库；本次未改写会话表内容。
- 原有上传原件及版本 DXF 的 SHA-256 一致。真实样图经新 ODA 路径导入、导出并回读校验通过，验收副本保存在 `data/validation/architecture-refactor-converter/`。
- 普通开发服务使用 `release` 模式，访问 `http://127.0.0.1:5173`；源码开发用 `AGENT_RUNTIME=source bash scripts/dev.sh`。

本地原始验收结果见 `data/validation/refactor-runtime.json`。数据库迁移备份位于 `.runtime/migrations/layout-v2/`。这些业务数据与运行产物不提交 Git。

本轮没有运行新的模型计费任务，也没有新增删除/图框裁剪/独立另存能力。原因和建议见 [CAD 能力诊断](cad-roadmap.md)；已有模型编辑验收见 [先前模型链路记录](agent-validation.md)。
