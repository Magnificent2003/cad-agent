# FreeCAD 文件操作实验

这个组件直接接入 [neka-nat/freecad-mcp](https://github.com/neka-nat/freecad-mcp) 的原版工具。智能体可以通过 `execute_code` 编写和执行 FreeCAD Python，查询、修改并导出图纸；本项目没有另写一套裁剪或删除 MCP。

网页 → 后端的任务副本 → 智能体（4096）→ 原版 MCP（stdio）→ FreeCAD 插件（9875）。生成文件由后端自动收集，网页提供下载与“打开并继续”。

当前重点是自主执行循环。历史实测已证实基本编程修改可行，也发现真实施工图保真问题；这些兼容性修复暂缓，详见 [验收记录](../docs/freecad-validation.md)。

## 目录与修改入口

| 路径 | 用途 |
| --- | --- |
| `mcp/` | 完整上游 Git 子模块，可编辑源码；许可证为 MIT |
| `mcp/src/freecad_mcp/` | 模型调用的 MCP 工具与 FreeCAD 通信 |
| `mcp/addon/FreeCADMCP/` | FreeCAD 内运行的插件；安装目录软链接到这里 |
| `mcp/.venv/` | 按上游 `uv.lock` 安装的独立 Python 环境 |
| `runtime/` | 官方 FreeCAD 发行程序，不提交 Git |
| `runtime.lock.json` | 官方包版本、下载地址、大小与 SHA-256 |
| `requirements-python.txt` | FreeCAD 执行环境的额外依赖清单，固定包版本 |
| `python-deps.lock.json` | 解析后的兼容 wheel、SHA-256 和运行环境标识 |
| `runtime/python-deps/` | 额外 Python 库的实际安装目录，不提交 Git |
| `install_python.sh`、`python_deps.py` | 安装、验证专用依赖；复用官方已有库 |
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

## 给 FreeCAD 补充 Python 依赖

FreeCAD GUI 和 MCP 的无界面执行都加载 `runtime/python-deps/`。当前安装 `ezdxf==1.4.4`，用于直接读写 DXF；NumPy、fontTools、pyparsing、typing_extensions 复用官方运行环境中已满足要求的版本。后端 `.venv` 与 MCP `.venv` 继续独立管理。

今后增加库时：

1. 在启动终端按 Ctrl+C 停止服务。
2. 在 `freecad/requirements-python.txt` 添加一行固定版本的依赖，格式为 `包名==版本号`；需要额外功能时也支持 `包名[extra]==版本号`。
3. 运行安装命令，再用原来的一键命令启动：

```bash
bash freecad/install_python.sh
bash scripts/dev.sh
```

只检查已有安装、不联网下载：

```bash
bash freecad/install_python.sh --check
```

完整安装 `bash freecad/install.sh` 已包含上述依赖安装。安装器使用 FreeCAD 自带 Python，自动解析缺少的间接依赖；固定官方基础库作为版本约束，避免覆盖 NumPy、Qt 等原有组件。依赖版本与官方库冲突，或没有兼容 wheel 时会报错，需再评估该库的版本或构建方式。

库先安装到临时目录，验证版本、间接依赖与实际导入后才替换正式目录；失败保留旧安装。安装成功会更新 `python-deps.lock.json`，请把它和依赖清单一起提交 Git。相同清单和环境会复用锁文件中的 wheel 与 SHA-256；已安装且检查通过时无需再次下载。删除或改版依赖时也通过清单和安装命令重建目录，旧版本不会混留。

日常启动只做离线检查，缺项会在打开 FreeCAD 窗口前明确报错。目录加载通过 FreeCAD 的 `-P` 参数完成，GUI 与 headless 共用 `run.sh`；没有修改官方 FreeCAD 核心或 MCP 源码。

依赖可用只证明 Python 文件操作环境就绪，不等于修复 FreeCAD 原生导入器的块、文字和坐标保真问题。

## 运行与对话

在项目根目录只需执行：

```bash
bash scripts/dev.sh
```

打开 http://127.0.0.1:5173，在网页上传图纸并新建对话。无需准备实验输入清单，也无需启动 4097；图纸副本由后端按每轮请求自动准备。在启动终端按 Ctrl+C 一起停止。

FreeCAD 日志位于 `.runtime/freecad/gui.log`，模型服务日志在 `.runtime/agent/server.log`。修改上游源码后重启对应服务即可。`start.sh`、`mcp.sh` 和下方诊断脚本保留用于组件开发，不是另一条网页编辑流程。

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

工具显示“已返回”只说明调用返回；上游部分 CAD 错误也以普通文本返回，必须检查实际内容和文件。默认开启的是已验证合成图可编辑的导入设置；真实工程图的具体损失及失败见验收记录。原生 FreeCAD 导入导出的历史验收以 GUI 进程为主，基础 Part 计算成功不能代表完整工程图导入导出成功。

2026-09-23 专用依赖验收：通过原版 GUI 和 headless MCP 分别使用 ezdxf 读取实际施工图的 14,217 个模型空间实体，在独立副本新增一个标记点，保存并回读得到 14,218 个实体；单位、标记坐标及后端独立回读检查通过，原始图纸 SHA-256 不变。FreeCAD 原生 Part 几何计算同时通过。记录见本机 `.runtime/python-deps-validation/report.json`。这验证了两种执行环境中的 Python 文件读写能力；原生导入器的对象保真仍按已有记录单独评估。

本地 RPC 仅监听回环地址。输入/输出目录的限定是智能体的操作约定；原版 `execute_code` 具有 FreeCAD 进程的 Python 执行能力，不提供操作系统级文件沙箱。网页通过每轮副本和串行任务管理运行；当前仍是本机开发服务。

配置、安装信息、运行日志放在 `.runtime/freecad/`；智能体状态放在 `.runtime/agent/`，网页图纸副本放在 `data/agent-tasks/`。早期诊断样本及记录仍在 `.runtime/freecad-agent/`。这些数据及 `runtime/` 均不提交 Git。

原版 headless 工具按上游实现将临时脚本写在 `~/.cache/freecad-mcp/headless/`，执行后删除脚本；这里属于运行缓存，不是开发源码。
