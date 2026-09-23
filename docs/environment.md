# 实际环境记录

核验日期：2026-09-22。Ubuntu 24.04.4 / WSL2，x86_64；现有系统中文字体可用。依赖安装范围为本项目 `.tools/`、`.venv/`、`agent/`、`cad_converter/runtime/`、`freecad/`、`frontend/node_modules/`，没有替换系统解释器或 Windows Node。

| 组件 | 实际版本 |
| --- | --- |
| Python | 3.12.3，来自 `/usr/bin/python3.12` |
| uv | 0.12.17 |
| Node.js | 24.21.0，Linux x64 |
| 智能体引擎（OpenCode 来源） | 1.18.31；本地源码显示 local |
| Bun（源码运行） | 1.3.14 |
| DeepSeek API 模型 | deepseek-flash |
| MCP Python SDK | 2.2.0，MCPServer |
| FastAPI / Uvicorn | 0.141.1 / 0.53.0 |
| ezdxf（后端 Python 3.12 / FreeCAD Python 3.11） | 两个独立安装均为 1.4.4 |
| Shapely | 2.1.2 |
| Matplotlib / Pillow | 3.11.2 / 12.3.0 |
| ODA File Converter | 27.1.0.0，官方 Qt6 AppImage |
| FreeCAD（独立文件实验） | 1.1.3，官方 Linux x86_64 AppImage，自带 Python 3.11 |
| FreeCAD MCP | 0.1.24，源码提交 `751974609a401660a58a1772ef16f3afbeba9ba1` |
| FreeCAD MCP 的 Python / SDK | 独立 Python 3.12.3 / MCP 1.30.0，按上游 uv.lock |
| Draft DXF 可选库 | 1.42，固定提交及逐文件哈希见 `freecad/dxf-library.lock.json` |
| Vite | 7.3.6 |

FreeCAD 额外 Python 库以 `freecad/requirements-python.txt` 和 `freecad/python-deps.lock.json` 为准，安装在 `freecad/runtime/python-deps/`；当前仅补充 ezdxf，复用官方 NumPy 1.26.4 等基础库。GUI 和无界面入口统一加载并检查。

后端 Python 依赖以 `uv.lock` 为准；前端以 `frontend/package-lock.json` 为准；OpenCode 以 `agent/package-lock.json`；源码模式见 `agent/source.lock.json` 与 `agent/engine/bun.lock` 为准。`scripts/env.sh` 将本地工具加入当前 shell PATH，不会加载 `.env` 或输出密钥。

Node 官方下载包已按同目录 `SHASUMS256.txt` 核对。ODA 下载文件的 SHA-256 为 `cb77a106354af98a1d355ba0191160aca3f90cb020946803e7c8132cba434dfc`；该记录用于此次下载产物复核，不冒充独立官方签名。

FreeCAD 下载包按官方 GitHub Release 的 SHA-256 校验，记录见 `freecad/runtime.lock.json`。插件通过软链接加载 `freecad/mcp/addon/FreeCADMCP` 源码，实际插件目录为 `.runtime/freecad/data/FreeCAD/v1-1/Mod/`。运行方法见 [FreeCAD 实验说明](../freecad/README.md)。

日常使用 `bash scripts/dev.sh` 统一启动 FreeCAD（9875）、唯一智能体（4096）、后端（8000）和网页（5173），按一次 Ctrl+C 统一停止。原 4097 实验入口和旧 CAD MCP 已移除。

### 连续重启的端口检查修复（2026-09-23）

本机复现了“首次启动成功，立即重启则 FreeCAD 已退出（1）”：FreeCAD 结束后，RPC 健康检查留下的 TCP 连接仍处于 `TIME_WAIT`；旧 `freecad/start.sh` 使用不带 `SO_REUSEADDR` 的临时 socket 检查 9875，因此把已关闭连接误报为端口占用。此时没有监听进程，FreeCAD 的 XML-RPC 服务器自身却允许重用该端口。

统一入口与 FreeCAD 独立入口现在共用 `scripts/ports.py`，以 `SO_REUSEADDR`、`bind` 和 `listen` 检查实际监听能力。真正的监听或不可重用的绑定仍会报错。启动失败会记录系统错误码和 TCP 状态到 `.runtime/startup-errors.log`；不会通过杀其他进程或自动换端口绕过冲突。检查只是启动前检查，不能消除检查后其他程序抢占端口的竞争。

同一天曾出现智能体 `ServeError` 和网页 5173 冲突，原日志没有保留足够信息来确认当时的占用者，不能据此认定它们与 FreeCAD 的误判完全同源。本机 WSL 使用镜像网络；若再次出现“Linux 无监听但无法绑定”，还应核对 Windows 侧对应端口。

## 环境变量

| 字段 | 用途 |
| --- | --- |
| DEEPSEEK_API_KEY / BASE_URL / MODEL | 后端模型配置；模型调用只由 OpenCode 负责 |
| CAD_API_BASE_URL | MCP 访问本地 API，默认 8000 |
| CAD_INTERNAL_TOKEN | 历史字段，旧内部 CAD API 已删除；现有值可保留但不使用 |
| AGENT_BASE_URL | 本地 OpenCode 地址，默认 4096 |
| AGENT_SERVER_USERNAME / PASSWORD | OpenCode HTTP/SSE BasicAuth |
| AGENT_RUNTIME | release 为发行程序，source 为本项目引擎源码 |
| CAD_DATA_DIR | 图纸与 SQLite 目录，相对路径以项目根目录解析 |
| ODA_EXECUTABLE | 转换器入口；相对路径由服务解析成绝对路径 |
| ODA_USE_XVFB | 无显示环境时通过 xvfb-run 启动转换器 |
| CAD_FONT_DIRS / CAD_FALLBACK_FONT | 字体搜索目录与预览回退字体 |
| CAD_CONVERSION_TIMEOUT / CAD_JOB_TIMEOUT | 转换子进程 / 完整 CAD 任务超时，秒 |
| CAD_MAX_UPLOAD_MB / CAD_PREVIEW_DPI | 上传大小上限与 PNG 概览分辨率 |
| CAD_EXPORT_TOLERANCE | DWG 回读数值容差，默认 1e-7 原生单位 |

当前 ODA 包含 xcb 显示插件，未包含 offscreen 插件；不能直接假设 `QT_QPA_PLATFORM=offscreen` 可用。本机验证使用 WSLg；无桌面主机安装 `xvfb xauth` 后设置 `ODA_USE_XVFB=true`。运行时不要将 `DISPLAY` 清空后仍使用 WSLg 模式。

部署服务只绑定本机。若修改 API 端口，同时调整 `frontend/vite.config.ts` 的开发代理目标；构建后由 API 同源提供前端，不需要开发代理。当前受限工具沙箱不能访问本地代理/显示套接字，因此依赖下载、真实 ODA、API 与浏览器测试在授权主机上下文运行；普通终端启动不需要此沙箱操作。

## 与初始指南的实施差异

- 通过项目内 portable Node、uv、OpenCode 和 ODA 安装，减少对系统环境的修改。
- CAD 每项任务启动独立工作进程、全局串行执行；短 SQLite 事务使用标准库 sqlite3。任务和发布状态持久化，但不提供分布式队列保证。
- 实体查询在工作进程按需解析并分页，尚未引入持久化搜索索引。
- 默认显示填充轮廓；所有图纸默认 SVG，可手动切换 PNG，SVG 加载失败时回退 PNG，局部预览按 CAD 范围重新渲染。
- ODA 的具体文件名按实际官方链接固定为 27.1；没有依据网页其他产品版本号推断转换器版本。

官方来源：[Node 下载清单](https://nodejs.org/dist/index.json)、[ODA 转换器](https://www.opendesign.com/guestfiles/oda_file_converter)、[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)、[OpenCode Server](https://opencode.ai/docs/server/)、[DeepSeek 模型能力](https://api-docs.deepseek.com/quick_start/pricing/)。实际兼容性以本项目验收记录为准。
