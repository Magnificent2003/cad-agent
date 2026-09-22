# FreeCAD 文件操作实验

这个组件直接接入 [neka-nat/freecad-mcp](https://github.com/neka-nat/freecad-mcp) 的原版工具。智能体可以通过 `execute_code` 编写和执行 FreeCAD Python，查询、修改并导出图纸；本项目没有另写一套裁剪或删除 MCP。

```text
自然语言 → agent 实验会话（4097）→ 上游 MCP（stdio）
                                  ↓
                         FreeCAD 插件（127.0.0.1:9875）
                                  ↓
                         FreeCAD 文档 / Python API
                                  ↓
                           DXF / DWG / FCStd
```

原网页仍通过 4096 端口使用原来的 CAD 服务。实验结果先保存为文件，不会自动发布到网页中的版本列表。

**实测状态：AI 已完成简单图的改字、移动、删除和 DXF/DWG 另存；单位元数据及真实施工图保真尚未通过。** 详细结果和失败复现见 [验收记录](../docs/freecad-validation.md)。

## 目录与修改入口

| 路径 | 用途 |
| --- | --- |
| `mcp/` | 完整上游 Git 子模块，可编辑源码；许可证为 MIT |
| `mcp/src/freecad_mcp/` | 模型调用的 MCP 工具与 FreeCAD 通信 |
| `mcp/addon/FreeCADMCP/` | FreeCAD 内运行的插件；安装目录软链接到这里 |
| `mcp/.venv/` | 按上游 `uv.lock` 安装的独立 Python 环境 |
| `runtime/` | 官方 FreeCAD 发行程序，不提交 Git |
| `runtime.lock.json` | 官方包版本、下载地址、大小与 SHA-256 |
| `install_dxf.sh`、`dxf-library.lock.json` | FreeCAD 官方兼容导出器所需的固定版本 DXF 库 |
| `install.sh` | 安装软件、MCP 依赖和插件 |
| `start.sh`、`start_mcp.FCMacro` | 启动 FreeCAD 及原版插件 RPC，配置项目 ODA 路径 |
| `run.sh`、`run_headless.sh` | GUI / 命令行运行入口，配置放在项目运行目录 |
| `mcp.sh` | 从可编辑安装启动原版 MCP |
| `prepare_samples.py` | 生成简单测试图或准备工程图副本，记录来源哈希 |
| `probe.py` | 诊断客户端：列出原版工具、调用工具或提交 Python 脚本 |
| `compare_dxf.py` | 比较导入导出前后结构，辅助检查信息丢失 |
| `roundtrip.py` | 调用原版 MCP 做无修改导入导出，记录文件和原始反馈 |

FreeCAD、MCP 服务、本项目后端分别使用自己的 Python 环境。不要将后端 `.venv` 的包复制到 FreeCAD 自带 Python 中。

## 安装

先完成根目录 README 中的项目环境安装，再在项目根目录执行：

```bash
git submodule update --init -- freecad/mcp
bash freecad/install.sh
```

安装面向 Linux x86_64 / WSL2：下载官方 AppImage、核对 SHA-256 后解包，无须 FUSE 或替换系统 Python。图形界面需要可用的 WSLg 或 Linux 桌面。DWG 转换还需已有的 `bash cad_converter/install.sh`。

安装还会下载固定版本的 Draft DXF 可选库，保留其 GPL 许可证，通过 FreeCAD 的 `-P` 参数加载。启动宏关闭交互导入弹窗、启用文字和可编辑 Draft 对象，并选择官方兼容导出器；未改写 FreeCAD 或 MCP 的导入导出源码。`ODAFileConverter` 是连接已有转换器的短启动脚本，名称用于满足 FreeCAD 的转换器检测规则。

MCP 依赖使用上游锁文件，安装为 editable 模式。更改 `mcp/src/` 后重启实验智能体；更改插件后重启 FreeCAD。插件通过软链接加载源码，不必每次手动复制。

新主机克隆父项目时可加 `--recurse-submodules`。父仓库只记录 MCP 子模块的提交号：若以后修改上游源码并需分享，应在子模块中提交、推送到自己的仓库，再更新父项目中的地址和提交号；仅提交父仓库不会上传子模块内的修改。

## 运行与对话

第一个终端启动 FreeCAD，保持窗口打开：

```bash
bash freecad/start.sh
```

第二个终端准备简单测试图并启动独立智能体：

```bash
.venv/bin/python freecad/prepare_samples.py --synthetic
.venv/bin/python -m agent.freecad_experiment \
  --input .runtime/freecad-agent/files/input/simple.dxf
```

实验智能体复用 `.env` 的模型及认证配置，默认监听 `127.0.0.1:4097`，只注册 `freecad_*` 工具。可用 `--check` 检查配置而不启动服务。

第三个终端通过自然语言调用，执行这一步会请求真实模型：

```bash
.venv/bin/python -m agent.freecad_chat \
  '打开 simple.dxf，把 ORIGINAL 改成 UPDATED，把圆向右移动 10 mm，删除完全位于绿色图框外的对象，保留跨界线。另存为 simple-edited.dxf，重新打开检查。'
```

终端会显示会话 ID，可用 `--session <ID>` 继续同一会话。完整工具调用和模型回复保存在 `.runtime/freecad-agent/reports/`。通过另一个终端的 `Ctrl+C` 停止实验智能体；关闭 FreeCAD 窗口停止插件服务。不要同时让多个会话修改同一个 FreeCAD 文档。

如果调试服务已经启动，直接运行第三个终端的对话命令即可。可用 `ss -ltnp '( sport = :4097 or sport = :9875 )'` 检查；日志分别在 `.runtime/freecad-agent/server.log` 和 `.runtime/freecad/gui.log`。再次运行 `start.sh` 会明确拒绝占用中的 9875 端口，避免误连另一实例。

真实工程图先准备副本：

```bash
.venv/bin/python freecad/prepare_samples.py \
  --drawing /path/to/drawing.dwg --name drawing.dwg
```

重启实验智能体时添加对应 `--input`，可重复指定多个文件。输入副本在 `.runtime/freecad-agent/files/input/`，输出在 `files/output/`，原文件哈希记录在输入目录的 `manifest.json`。

## 验证与范围

```bash
# 列出原版 MCP 工具，不调用模型
.venv/bin/python freecad/probe.py
.venv/bin/python freecad/probe.py --tool get_rpc_status

# 无修改导入导出；返回 completed 仅表示流程完成
.venv/bin/python freecad/roundtrip.py simple.dxf --dwg --fcstd

# 明确选择兼容导入方案，任务结束后恢复原设置
.venv/bin/python freecad/roundtrip.py drawing.dwg --legacy-import --plain-groups --fcstd

# 对比实际输出；统计相近不代表工程语义全部保真
.venv/bin/python freecad/compare_dxf.py \
  .runtime/freecad-agent/files/input/simple.dxf \
  .runtime/freecad-agent/files/output/simple-edited.dxf
```

DXF/DWG 导入后变成 FreeCAD 对象，导出时重新生成文件；DWG 还经过格式转换。图块展开会改变实体数量，专业对象、布局、字体、标注等必须另外检查。仅有成功消息、截图或 FCStd 文件，不算完成 DWG/DXF 文件交付。

工具显示“已返回”只说明调用返回；上游部分 CAD 错误也以普通文本返回，必须检查实际内容和文件。默认开启的是已验证合成图可编辑的导入设置；真实工程图的具体损失及失败见验收记录。headless 的基础 Part 计算已验证，DXF/DWG 测试使用 GUI 进程；不要把 headless 计算通过当成二维导入导出通过。

本地 RPC 仅监听回环地址。输入/输出目录的限定是智能体的操作约定；原版 `execute_code` 具有 FreeCAD 进程的 Python 执行能力，不提供操作系统级文件沙箱。实验只针对准备好的副本，不应将该入口直接开放为多用户服务。

配置、安装信息、运行日志放在 `.runtime/freecad/`；智能体状态和图纸副本放在 `.runtime/freecad-agent/`。这些数据及 `runtime/` 均不提交 Git。

原版 headless 工具按上游实现将临时脚本写在 `~/.cache/freecad-mcp/headless/`，执行后删除脚本；这里属于运行缓存，不是开发源码。
