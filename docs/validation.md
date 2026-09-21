# 第一阶段验收总览

核验日期：2026-09-21。真实工程图纸已迁入 `samples/private/`，20 个文件的 SHA-256 核对一致，未覆盖原图。

| 验收范围 | 结果与证据 |
| --- | --- |
| Python 后端 / CAD / MCP | 43 个自动化用例通过；包含版本冲突、幂等、源文件保留、停止和重启恢复、原生编辑、几何规则、鉴权与 SSE |
| 前端生产构建 | TypeScript 与 Vite 构建通过 |
| 依赖复现 | `uv sync --frozen --offline` 通过；前端与 OpenCode 均保存 npm lockfile |
| 真实 DWG | 两份样本导入及 DWG 导出重新导入通过；原件 SHA-256 保持不变 |
| 真实总图修改 | 单个目标 TEXT 修改成功，26,466 个非目标图形实体保持一致 |
| 真实视觉输入 | OpenCode / DeepSeek / MCP ImageContent 图片识别与后续工具调用通过 |
| 完整智能体任务 | API 上传合成 DXF → 查询 → 改字 → 新版本 → 读回 → 局部 PNG → SSE 同步通过 |
| 配置保密 | `.env` / `.env.example` 字段集合一致；源代码、报告与前端构建产物未发现真实密钥或服务密码值 |
| 规范约束 | 显式项目/演示规则的 pass/fail/unknown/not_applicable 用例通过；真实规范审查未运行 |

浏览器自动化与真实操作记录见 [frontend-validation.md](frontend-validation.md)。CAD 细节见 [cad-validation.md](cad-validation.md)，模型链路见 [agent-validation.md](agent-validation.md)。

Python 测试有 Starlette/AnyIO 与测试子进程 fork 的依赖弃用提示，不影响用例通过。这里的通过范围是上述明确样本和操作；未将代理对象、缺失外参/图片、原始字体、块内编辑或复杂工程语义识别列为全面验收通过。
