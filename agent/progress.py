"""Bounded, public action observations; never expose model reasoning or scripts.

The same projection is used for live events and restored conversation history.
A completed MCP invocation can still contain a failed operation or an inconclusive
review. ``outcome`` describes that result, not delivery or engineering approval.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any


TITLES = {
    "cad_inspect": "读取图纸与场地信息",
    "cad_layout": "布置施工设施与道路",
    "cad_review": "复核当前候选图纸",
    "cad_spatial_facts": "识别当前图纸的空间占地",
    "cad_spatial_constraints": "计算主轴与检查候选占地",
    "cad_site_rules": "读取场布规则与面积估算",
    "execute_code": "执行图纸脚本",
    "execute_code_headless": "执行无界面图纸脚本",
    "execute_code_async": "提交图纸脚本任务",
    "get_async_status": "查询脚本任务进度",
    "get_job_status": "查询脚本任务进度",
    "get_job_result": "读取脚本任务结果",
    "get_rpc_status": "检查 FreeCAD 执行状态",
    "get_view": "查看图纸预览",
    "get_screenshot": "查看图纸预览",
    "get_objects": "查询图纸对象",
    "get_object": "读取图纸对象",
    "list_documents": "查询已打开的图纸",
    "open_document": "打开图纸",
    "save_document": "保存图纸",
    "create_document": "新建图纸",
    "create_object": "新增图纸对象",
    "edit_object": "修改图纸对象",
    "delete_object": "删除图纸对象",
    "reload_document": "刷新图纸",
}
# Arbitrary stdout is not a public transcript. Retain only a few CAD result lines.
_RESULT_LINE = re.compile(
    r"^(?:components?(?: created| declared| handles)?|entities|saved(?: entities)?|"
    r"removed|moved|restored road width|zeroed|road(?: segments| pts)?|"
    r"frame(?: origin/angle)?|site local size|area(?:_mm2|_m2)?|"
    r"preview|output files|output dir now|layout layers|widened LWPOLYLINE|"
    r"INPUT widened polylines|type|any width/bulge|verified handle|"
    r"设施|构件|实体|保存|删除|移动|复核|碰撞|道路|图层|输出文件)\b", re.I,
)
_SECRET_LINE = re.compile(
    r"(?:api[_ -]?key|authorization|password|passwd|secret|credential|"
    r"access[_ -]?token|refresh[_ -]?token|bearer\s|\bsk-[\w-]+|"
    r"system[_ -]?prompt|reasoning|os\.environ|data:[^\s,]+;base64)", re.I,
)


def _safe(value: Any, limit: int = 230) -> str:
    """Sanitize only the already selected public fields, not whole outputs."""
    if not isinstance(value, (str, int, float, bool)):
        return ""
    text = str(value).replace("\x00", "")[:3000]
    if _SECRET_LINE.search(text):
        return "[已省略包含内部信息的反馈]"
    text = re.sub(r"https?://[^\s\"'<>]+", "[服务地址]", text)
    text = re.sub(r"(?:[A-Za-z]:[\\/]|(?<!\w)/)[^\s\"'<>，；]+", "[本地路径]", text)
    text = re.sub(r"(?<!\w)[A-Za-z0-9+/_=-]{48,}(?!\w)", "[长数据已省略]", text)
    text = re.sub(r"[\x01-\x08\x0b-\x1f\x7f]", "", text)
    return text[:limit] + ("…" if len(text) > limit else "")


def _payload(output: Any) -> dict[str, Any]:
    if isinstance(output, dict):
        return output
    if isinstance(output, str) and len(output) <= 2_000_000:
        try:
            value = json.loads(output)
            return value if isinstance(value, dict) else {}
        except (ValueError, RecursionError):
            pass
    return {}


def _error_summary(text: str, data: dict[str, Any] | None = None) -> tuple[str, str]:
    lower = text.lower()
    data = data or {}
    recovery = any(token in lower for token in ("document recovery", "recovery_dialog", "recovery window"))
    modal = recovery or data.get("code") == "GUI_MODAL_BLOCKED" or any(
        token in lower for token in ("modal", "gui_blocked", "gui blocked")
    )
    if modal:
        summary = "FreeCAD 恢复窗口阻挡了执行" if recovery else "FreeCAD 窗口阻挡了执行"
        if data.get("operation_started") is True or "operation already started" in lower:
            return summary, "当前操作已经开始，未被取消；关闭窗口后可能继续执行。请先核对文件结果，再决定是否重试写入。"
        if data.get("cancelled") is True or "not executed and has been cancelled" in lower:
            return summary, "当前请求尚未执行，已取消排队；处理 FreeCAD 弹窗后可重新尝试。"
        return summary, "FreeCAD 正等待窗口操作；请先处理弹窗并核对任务状态，再决定是否重试。"
    if "gui dispatch" in lower and "still running" in lower:
        return "FreeCAD 执行超时，原任务仍在运行", "操作已经开始且无法安全取消；请先核对执行结果，不要重复提交写入。"
    if "queue_timeout" in lower or ("gui dispatch" in lower and "to start" in lower):
        return "FreeCAD 等待执行超时", "脚本尚未开始执行，在 GUI 队列中等待超时；可能有未关闭的对话框或前序任务阻塞。"
    if "timeout" in lower or "timed out" in lower or "deadline exceeded" in lower:
        return "工具响应超时", "未在规定时间内取得结果，不能据此判断绘图是否完成。"
    if any(token in lower for token in ("connection refused", "connection closed", "transport closed")):
        return "工具连接中断", "未取得可靠的执行结果。"
    match = re.search(r"(?:\b\w+(?:Error|Exception)|\bError):[^\n]*", text)
    return "工具执行异常", _safe(match.group(0)) if match else "未取得有效结果，请查看助手后续说明。"


def _issue_lines(issues: list[Any]) -> list[str]:
    groups: dict[str, list[dict]] = {}
    for issue in issues:
        if isinstance(issue, dict):
            groups.setdefault(str(issue.get("code", "issue")), []).append(issue)
    lines = []
    for code, rows in list(groups.items())[:5]:
        handles = list(dict.fromkeys(
            h for row in rows for h in (row.get("handles") or []) if isinstance(h, str)
        ))[:5]
        label = _safe(rows[0].get("message") or code, 160)
        suffix = f"（{len(rows)} 处）" if len(rows) > 1 else ""
        if handles:
            suffix += "；实体 " + ", ".join(_safe(handle, 18) for handle in handles)
        lines.append(label + suffix)
    if len(groups) > 5:
        lines.append(f"另有 {len(groups) - 5} 类问题。")
    return lines


def _domain_result(tool: str, data: dict[str, Any]) -> tuple[str, str, bool] | None:
    if tool == "cad_inspect" and isinstance(data.get("overview"), dict):
        overview = data["overview"]
        count = overview.get("modelspace_entity_count")
        summary = f"读取到 {count} 个模型空间实体" if isinstance(count, int) else "已读取图纸信息"
        units = data.get("units") if isinstance(data.get("units"), dict) else {}
        lines = []
        types = overview.get("entity_types")
        if isinstance(types, dict):
            lines.append("实体类型：" + "，".join(
                f"{_safe(k, 30)} {v}" for k, v in list(types.items())[:6] if isinstance(v, int)
            ))
        if units.get("metres_per_unit") is not None:
            lines.append(f"文件声明单位：每单位 {_safe(units['metres_per_unit'], 24)} 米；"
                         + ("已确认。" if units.get("confirmed") else "尚未确认实际尺寸。"))
        return summary, "\n".join(lines), False
    if tool not in {"cad_layout", "cad_review"} or not data:
        return None
    status = data.get("status")
    issues = data.get("issues") if isinstance(data.get("issues"), list) else []
    bad = status in {"failed", "error", "unknown", "blocked"}
    if tool == "cad_review":
        summary = {"passed": "候选图纸通过当前检查", "failed": "候选图纸复核未通过",
                   "unknown": "候选图纸复核无法完成"}.get(status, "已返回图纸复核结果")
    else:
        components = data.get("components")
        summary = {"failed": "场布模块未能完成布置", "unknown": "场布模块遇到不支持的条件"}.get(status,
                    f"场布模块返回 {len(components)} 个构件" if isinstance(components, list) else "已返回场布结果")
    lines = _issue_lines(issues)
    checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    if checks.get("site_dependent_geometry") == "skipped_unconfirmed_facts":
        lines.append("场地事实未确认，红线与障碍物相关检查已跳过。")
    geometry = checks.get("site_geometry") if isinstance(checks.get("site_geometry"), dict) else {}
    if geometry.get("construction_norms") == "not_tested":
        lines.append("本次未检查施工规范。")
    if isinstance(data.get("file_sha256"), str) and re.fullmatch(r"[a-fA-F0-9]{64}", data["file_sha256"]):
        lines.append("检查文件指纹：" + data["file_sha256"][:12])
    return summary, "\n".join(lines), bad


def tool_progress(tool_name: str, state: dict[str, Any]) -> dict[str, Any]:
    tool = tool_name.removeprefix("freecad_")
    title = TITLES.get(tool, "执行 CAD 工具")
    status = state.get("status")
    result: dict[str, Any] = {
        "status": status, "title": title, "error": None,
        "summary": f"正在{title}" if status in {"pending", "running"} else f"{title}已返回",
        "details": "", "time": {},
        "outcome": "running" if status in {"pending", "running"} else "returned",
    }
    for key, value in (state.get("time") or {}).items():
        if key in {"start", "end"} and isinstance(value, (int, float)) and math.isfinite(value):
            result["time"][key] = value
    if result["outcome"] == "running":
        return result
    output = state.get("output") or ""
    data = _payload(output)
    text = output if isinstance(output, str) else ""
    error = state.get("error")
    if status == "error" or error or data.get("success") is False or data.get("isError") is True or text.startswith(("Failed to ", "Headless FreeCAD script FAILED:", "Cannot get screenshot")) or re.match(r"Async job [^\n]+: failed\b", text):
        summary, details = _error_summary(str(error or data.get("error") or text)[:10000],
                                          error if isinstance(error, dict) else data)
        result.update(summary=summary, details=details, error=summary, outcome="error")
        return result
    if tool == "execute_code_async" and text.startswith("Code execution started in background"):
        result.update(summary="脚本已提交后台，等待执行结果", details="已提交不代表绘图完成，需继续查询任务状态。")
        return result
    if tool == "get_async_status":
        match = re.match(r"Async job [^\n]+: (queued|running|done|cancelled)\b", text)
        if match:
            job_state = match.group(1)
            result.update(summary={"queued": "后台脚本正在排队", "running": "后台脚本仍在执行",
                                   "done": "后台脚本已返回", "cancelled": "后台脚本已取消"}[job_state],
                          outcome="error" if job_state == "cancelled" else "returned")
            return result
    domain = _domain_result(tool, data)
    if domain:
        summary, details, bad = domain
        result.update(summary=summary, details=details[:1800], outcome="error" if bad else "returned",
                      error=summary if bad else None)
    elif tool == "get_rpc_status" and isinstance(data.get("gui_dispatch"), dict):
        dispatch = data["gui_dispatch"]
        blocked = dispatch.get("state") in {"blocked", "modal_blocked", "unresponsive", "stalled"}
        if dispatch.get("state") == "stuck":
            result.update(summary="FreeCAD 原任务超时后仍在执行", outcome="error",
                          error="FreeCAD 执行任务超时",
                          details="原任务已经开始，无法安全取消；请先核对执行结果，不要重复提交写入。")
            return result
        lines = ["GUI 状态：" + _safe(dispatch.get("state", "unknown"))]
        if blocked:
            if dispatch.get("modal_title"):
                lines.append("阻塞窗口：" + _safe(dispatch["modal_title"], 90))
            seconds = dispatch.get("blocked_for_seconds")
            if isinstance(seconds, (int, float)) and math.isfinite(seconds):
                lines.append(f"窗口已阻塞 {seconds:.0f} 秒；处理窗口后请先核对任务状态。")
        result.update(summary="FreeCAD 正在等待窗口处理" if blocked else "已取得 FreeCAD 执行状态",
                      details="\n".join(lines), error="FreeCAD 窗口阻塞" if blocked else None,
                      outcome="error" if blocked else "returned")
    elif "execute_code" in tool and "Output:" in text:
        stdout = text.split("Output:", 1)[1].split("Traceback:", 1)[0]
        lines = [_safe(line.strip()) for line in stdout.splitlines() if _RESULT_LINE.match(line.strip())]
        result["details"] = "\n".join(lines[:6])[:1500]
        if lines:
            result["summary"] = lines[0][:110]
    return result
