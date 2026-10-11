# 智能体组件

网页通过后端连接本组件。执行工具集由项目维护的 FreeCAD MCP 提供，包括 Python 执行以及空间事实、场布约束和成果复核；模型自主选择工具、读取反馈、继续操作。

- `config.json`：模型、MCP 和工具权限。
- `provider.py`：根据 `LLM_PROVIDER` 选择 DeepSeek / AIAPI 对应配置，不混用密钥、不自动回退。
- `trace.py`：把后端实际收到的每轮模型消息、工具参数及反馈写入 `CAD_DATA_DIR/agent-runs/<任务ID>.md`；`latest.md` 指向最近运行。原始 SSE 和历史补全同时记录，工具输出被会话压缩前已捕获的内容会保留。
- `prompts/cad.txt`：自主探索、执行、纠错、回读以及成果目录约定。
- `launcher.py`：启动服务，复用 `.env` 的模型连接，默认 4096。
- `client.py`：后端 HTTP/SSE 客户端，支持当前图纸预览附件。
- `preview.py`：已有 PNG 预览的格式、完整性及大小校验。
- `session.py`：区分内部压缩消息、工具循环、截断和正式答复。
- `plugins/task-context.mjs`：每次模型调用及 Python 执行重新绑定当前任务；压缩后仍有效。
- `runtime.py`：隔离运行配置，避免用户目录中的旧 MCP 配置重新进入项目。
- `engine/`：纳入版本控制的可修改引擎源码；来源见 `source.lock.json`。

日常运行 `bash scripts/dev.sh` 一起启动全部程序。单独执行 `bash agent/start.sh` 只供组件调试，需先有 FreeCAD 服务。原 4097 实验入口已经移除。

后端将当前图纸的副本和输出目录随每一轮请求交给模型，同时在 `data/agent-contexts/` 原子更新活跃绑定。插件从绑定读取最新上下文，并在 FreeCAD 执行脚本前提供 `cad_context` 字典。每轮还会附上当前版本已有的 PNG 总览，供模型直接观察；图片缺失或无效时仍可分析 CAD 文件，精确数据以原文件为准。模型可以自行选择 ezdxf 或 FreeCAD API；插件不增加针对具体工程问题的固定工具。生成文件由后端收集，智能体不直接写项目数据库和图纸版本目录。工作方式约定及旧路径检查不能替代操作系统沙箱。

内部压缩摘要与隐藏续跑消息同时在历史 API 和 SSE 中过滤。后端将 `length`、没有正式正文以及持续空闲无答复视为未交付，在同一任务中最多自动续跑两次；这不是普通工具调用次数上限。重复失败不发布半成品，用户取消优先于恢复和发布。

DeepSeek 保留原配置（上下文预算 1,000,000，单次输出预算 32,000 token）；AIAPI 默认预算 128,000 / 32,000，可用 `AIAPI_CONTEXT_LIMIT` / `AIAPI_OUTPUT_LIMIT` 调整。这些是项目运行预算，不声明厂商上限。AIAPI 使用兼容 Chat Completions 的接口。切换供应商后重启服务，CAD Agent 的模型绑定优先于旧会话模型；做模型对比时应新建对话。记录只保存上游实际返回的推理或摘要，不补造内部推理。

修改 `engine/` 后使用 `bash agent/install.sh --source` 安装依赖，再用 `AGENT_RUNTIME=source bash scripts/dev.sh` 测试。上游所需的 OPENCODE 环境变量映射集中在 `runtime.py`；业务代码使用 AGENT 命名。
