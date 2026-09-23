# FreeCAD MCP 接入与实测

> 以下是独立实验时期的历史记录。当前网页已接入 FreeCAD，统一启动和最新范围见 [自主执行接入](autonomous-agent.md)。本文的 4097 实验命令不再适用；文件兼容性问题仍待处理。

日期：2026-09-22。结论：**现成 MCP 已接入，AI 可以自主编程修改并另存图纸；当前 FreeCAD 文件往返尚未达到真实施工图保真要求。**

## 安装和连接

- 根目录 `freecad/mcp/` 是上游 Git 子模块，固定提交 `751974609a401660a58a1772ef16f3afbeba9ba1`，包版本 0.1.24。原版插件和 MCP 工具源码没有修改。
- FreeCAD 1.1.3 官方 Linux AppImage 已下载、校验并安装到 `freecad/runtime/`；插件目录链接到源码，方便后续开发。
- 安装了 FreeCAD 官方兼容 DXF 导出选项使用的 Draft DXF 库 1.42，固定来源及哈希；未启用运行时自动下载。
- 原版 MCP 的 GUI Python 执行、工具发现、截图反馈均已实际调用。headless 工具独立计算 `Part.makeBox(2,3,4).Volume`，返回约 24。
- 当前智能体以独立实验入口连接 FreeCAD MCP，使用真实 DeepSeek 模型；状态位于 `.runtime/freecad-agent/`，服务端口 4097。原网页流程未接入这些输出文件。
- 不需要手动补充下载。安装、运行和自然语言对话命令见 [组件说明](../freecad/README.md)。

安装和诊断时修正了连接配置：禁止导入弹窗阻塞远程调用；使用带有 `ODAFileConverter` 名称的启动入口满足 FreeCAD 检测规则；为可选 DXF 库使用官方 `-P` 搜索路径；诊断客户端使用本项目 MCP SDK 2.x 的参数和字段。上述工作没有重写 MCP 的 CAD 能力。

## AI 自主编辑测试

输入是脚本生成的 6 实体 DXF：绿色矩形、图内文字和圆、两个完全框外实体，以及一条跨界线。只给模型自然语言任务，没有预写修改脚本或增加专门的删除/裁剪工具。

模型调用原版 `execute_code` 查询对象、检查 API、执行修改并自行调试导出。一次完整会话约 5 分 29 秒、64 次工具调用，说明通用编程路线已跑通，但效率仍有优化空间。

| 独立回读检查 | DXF | DWG 经 ODA 回读 |
| --- | --- | --- |
| `ORIGINAL` 改为 `UPDATED` | 通过 | 通过 |
| 圆心从 (30,30) 变为 (40,30)，半径仍为 5 | 通过 | 通过 |
| 两个完全框外实体被删除 | 通过 | 通过 |
| 绿色矩形和跨边界线保留 | 通过 | 通过 |
| 剩余 4 个实体，文字高度仍为 4 | 通过 | 通过 |
| 显式毫米单位标记保留 | **未通过，单位标记变为 0** | **未通过，单位标记变为 0** |

文件位于本机 `.runtime/freecad-agent/files/output/`：`simple-edited.dxf`、`simple-edited.dwg`、`simple-edited.FCStd`。模型实际重新导入并检查了结果；本项目另外使用 ezdxf 和独立 ODA 调用回读核验，没有只依据模型自述。

默认 C++ DXF 导出器在这份样图上丢失文字。切换官方兼容导出器后保留了文字，但生成 R12 DXF（AC1009），未保留 `$INSUNITS`。模型还发现并处理了 DWG 中转 DXF 的代码页问题，最终 DWG 可读回。**因此功能性修改通过，包含单位元数据的严格验收仍未通过。**

本机证据：

- `.runtime/freecad-agent/reports/ses_f38308027ffejcTg28JjFlUm25.json`：完整模型会话及代码。
- `.runtime/freecad-agent/reports/simple-independent-check/result.json`：独立回读、几何和哈希检查。
- `simple-20260922T062224-0cc79948`：默认 C++ 导出丢文字的对照记录。
- `simple-20260922T063012-2439be85`：兼容导出保留文字的记录。

## 真实工程图测试

两份工程图及已有 ODA 转换 DXF 均先复制到实验输入目录。比较以已有 DXF 为基线，不能据此宣称原 DWG 全部专业语义已被解析。

| 样本和设置 | 实际结果 |
| --- | --- |
| 入口标识 DWG，原生导入器 + Draft 对象 | 导入出现图块处理错误，GUI 执行超过 300 秒。仅重启本次实验 FreeCAD 恢复 |
| 入口标识 DWG，兼容导入器 + 独立 Part 形状 | 生成 DXF/DWG/FCStd，但信息和坐标未保真 |
| 白桥施工总图 DWG，兼容导入器 + Draft 图层组 | 导入报 `FeaturePython` 没有 `addObject` |
| 同一总图，改用官方普通分组选项 | 继续处理后，DXF 导出报中文 `UnicodeEncodeError`（latin-1），未完成交付 |

入口图输出只剩 4047 条 LINE 和 1 条 POLYLINE；块、代理对象、图片、纸空间实体和文字没有保留。逐条匹配确认，4047 条 LINE 的端点坐标全部变为基线的 **25.4 倍**。源图没有指定单位，因此不能直接认定它原本应为 mm；但可以确认往返改变了原始坐标。

入口图源模型空间引用块中有 154 个 MTEXT 实例，纸空间引用中有 82 个 TEXT 和 30 个 ATTRIB 实例；这些引用实例不等同于全部实际可见文字。输出没有文字，是结构检查所得结论。仅凭实体数量或包围盒相近不能证明保真。

总图失败时留下的部分 DXF 已标记为 `.dxf.incomplete`，不能作为有效成果。没有对原始工程图执行删除或裁剪。

本机证据目录（都位于 `.runtime/freecad-agent/reports/`）：

- `entrance-20260922T062354-f405e426`：300 秒 GUI 超时，原始响应在 `response.json`。
- `entrance-20260922T063718-0186f1ea`：兼容往返、结构比较，以及 `geometry-check.json` 中的逐线缩放验证。
- `site-plan-20260922T063853-9afc0b38`：图层组 API 异常。
- `site-plan-20260922T064158-439718a1`：中文导出异常和不完整输出标记。

## 复验和下一步

默认启动配置恢复为已验证简单图可用的“原生导入 + Draft 对象 + 兼容导出”。兼容性试验由诊断脚本显式选择，结束后恢复原设置：

```bash
.venv/bin/python freecad/roundtrip.py simple.dxf --dwg --fcstd
.venv/bin/python freecad/roundtrip.py entrance.dwg --legacy-import --dwg --fcstd --timeout 300
.venv/bin/python freecad/roundtrip.py site-plan.dwg --legacy-import --plain-groups --fcstd --timeout 300
```

脚本现在先记录导入配置和对象统计，并优先保存 FCStd，再尝试 DXF/DWG 导出；导出失败仍返回 `completed=false`。这方便下阶段检查已导入的文档，不把原生文件保存成功当成 DWG/DXF 交付成功。

实验入口及会话 CLI 的 11 项测试、Ruff 和 shell 语法检查通过。最后一轮真实 MCP 合成图往返也通过，记录在 `simple-20260922T064819-68c784e9`。准备的输入副本及其原始来源文件哈希均保持一致。

下一阶段先处理单位、代码页/中文导出、图块与天正对象保留，并复跑无修改往返。再让模型执行真实绿色图框任务，最后将成功文件接到网页版本管理。此次结果支持继续使用通用代码执行环境；当前主要限制已具体定位到文件兼容性，尚不能宣称 FreeCAD 可无损编辑这批施工图。
