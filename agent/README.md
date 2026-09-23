# 智能体组件

网页通过后端连接本组件。唯一执行工具集是原版 FreeCAD MCP，模型通过 Python 自行实现用户要求，不再提供旧 `cad_*` 固定操作工具。

- `config.json`：模型、MCP 和工具权限。
- `prompts/cad.txt`：自主探索、执行、纠错、回读以及成果目录约定。
- `launcher.py`：启动服务，复用 `.env` 的模型连接，默认 4096。
- `client.py`：后端 HTTP/SSE 客户端。
- `session.py`：区分上一轮回复、工具调用和本轮最终结束。
- `runtime.py`：隔离运行配置，避免用户目录中的旧 MCP 配置重新进入项目。
- `engine/`：纳入版本控制的可修改引擎源码；来源见 `source.lock.json`。

日常运行 `bash scripts/dev.sh` 一起启动全部程序。单独执行 `bash agent/start.sh` 只供组件调试，需先有 FreeCAD 服务。原 4097 实验入口已经移除。

后端将当前图纸的副本和输出目录随每一轮请求交给模型。生成文件由后端收集；智能体不直接写项目数据库和图纸版本目录。工作方式约定不能替代操作系统沙箱。

修改 `engine/` 后使用 `bash agent/install.sh --source` 安装依赖，再用 `AGENT_RUNTIME=source bash scripts/dev.sh` 测试。上游所需的 OPENCODE 环境变量映射集中在 `runtime.py`；业务代码使用 AGENT 命名。
