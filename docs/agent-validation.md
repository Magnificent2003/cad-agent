# OpenCode 与 CAD MCP 验收

验收日期：2026-09-21。实际环境：OpenCode **1.18.31**、MCP Python SDK **2.2.0**、DeepSeek **deepseek-flash**。记录见 [agent-validation.json](agent-validation.json)。

## 已通过的真实调用

1. **图片传递与后续工具调用**：独立 OpenCode 实例从 MCP `ImageContent` 收到一张 600×240 的合成 PNG，识别仅存在于图片中的随机四位数字和红色圆圈数量，再调用校验工具核对成功。随机答案未写入提示词、工具描述或文字结果。这证明当前固定 release 的图片附件处理可用，无须额外视觉模型循环。
2. **完整 CAD 主会话**：通过 FastAPI 上传合成 DXF，OpenCode 调用 `cad_summary` → `cad_query_entities` → `cad_apply_operations` → `cad_read_entities` → `cad_render_region`。版本由 `r0001` 更新为 `r0002`；下载后用 ezdxf 回读确认 `DEMO ORIGINAL` 改为 `DEMO UPDATED`，非目标折线保持不变。
3. **正式渲染图片附件**：核对 CAD 会话中的 `cad_render_region`，输入 bbox 为 `[-5,-5,105,65]`；完成结果附有 `mime=image/png`、`data:image/png;base64,...` 图片，文字结果仅 152 字符。工具未把图片伪装成磁盘路径或大段 JSON。
4. **事件与预览一致性**：订阅实际 FastAPI 会话 SSE，接收到文字增量、工具状态、任务状态及携带 `r0002` 的 `drawing.updated`；新版本 PNG 可下载且 PNG 签名有效。
5. **鉴权与工具权限**：正式 OpenCode 健康接口显示 1.18.31，唯一连接 provider 为 `deepseek-cad`，CAD MCP 状态为 connected。最终权限规则为 `* = deny`、`cad_* = allow`。MCP launcher 重新执行解释器时仅保留内部 CAD 服务令牌及基础运行变量，不向 CAD MCP 传递 DeepSeek 密钥或 OpenCode 密码。

测试只将自动生成的测试图片和 CAD 数据发送到模型。用户提供的真实工程样图未用于云端模型验收。真实样图的本地转换和渲染属于单独的 CAD 验收范围。

## 复现

先按 README 启动 FastAPI 与正式 OpenCode，再运行：

```bash
.venv/bin/python scripts/start_opencode.py --check
.venv/bin/python scripts/verify_agent.py
.venv/bin/python scripts/verify_agent.py --vision
.venv/bin/python scripts/verify_agent.py --cad
.venv/bin/python -m pytest tests/test_opencode.py -q
```

默认诊断只访问本机接口。`--vision` 和 `--cad` 使用真实 DeepSeek API，会产生少量调用费用。`--vision` 使用独立的 4097 端口并在结束后关闭该实例；`--cad` 使用已运行的 8000/4096 服务，在应用中保留可查看的合成图纸及版本记录。

`tests/test_opencode.py` 的 **18 个测试**覆盖 HTTP 与 SSE BasicAuth、异步提交、会话归属过滤、多行 SSE 与心跳处理、断连释放上游连接、错误信息避免回显上游正文，以及 MCP PNG 字节、局部图 URL 参数保留、即时 DXF 导出和所有排队工具的会话绑定。

产物保存在被 Git 忽略的 `.runtime/vision-smoke/` 与 `.runtime/agent-cad-smoke/`。服务凭据只从本地环境读取；报告不记录凭据、鉴权头或模型推理正文。

## 接口依据与边界

- [OpenCode Server](https://opencode.ai/docs/server/)：使用会话、`prompt_async`、历史、abort 和 SSE 接口；模型历史、推理和工具循环由 OpenCode 管理。
- [OpenCode MCP 配置](https://opencode.ai/docs/mcp-servers/) 与 [权限配置](https://opencode.ai/docs/permissions/)：自建 CAD 工具通过 stdio 接入并按工具名授权。
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 与 [图片返回说明](https://py.sdk.modelcontextprotocol.io/servers/media/)：使用已安装的 2.2.0 `MCPServer` 与标准 `ImageContent`。

验收证明当前固定版本和合成用例的链路可运行，不代表任意复杂 DWG 已完成语义识别或无损往返。规范 RAG 尚未配置，规范审查未运行。
