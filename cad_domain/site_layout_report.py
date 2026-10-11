"""Read-only figures regenerated from the delivered DXF, not planning geometry.

New road/facility geometry comes from CAD_AGENT_SITE-tagged DXF entities. Source
obstacles are reread through their exact transformed instance paths. The report
supplies interpretation, decisions and checks, never substitute plot coordinates.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyBboxPatch, Patch
from matplotlib.lines import Line2D
from shapely.affinity import scale, translate
from shapely.geometry import shape
from shapely.ops import unary_union

from .geometry import _metadata, _read, occupied_polygon
from .spatial_extract import extract_drawing
from .spatial_report import INK, BLUE, ORANGE, _draw_geometry, _panel_text

RC = {"font.family": ["Noto Sans CJK SC", "DejaVu Sans"],
      "axes.unicode_minus": False, "svg.fonttype": "none", "pdf.fonttype": 42,
      "ps.fonttype": 42, "font.size": 10, "figure.facecolor": "white",
      "savefig.facecolor": "white"}
COLORS = ["#8255a8", "#b57521", "#39769c", "#b85d7b", "#5f78b8", "#a06b51"]
FILLS = ["#ece2f4", "#f7ead5", "#dfedf5", "#f6e0e8", "#e4eafb", "#f1e5dc"]
STATUS_NAMES = {"passed": "通过", "pass": "通过", "failed": "未通过", "fail": "未通过",
                "warning": "提示", "unknown": "未核实", "not_evaluated": "未核实",
                "not_checked": "未检查", "partial": "部分满足", "assumed": "采用假设"}
CHECK_NAMES = {
    "inside_boundary": "完整占地位于作业范围内", "boundary": "作业范围约束",
    "obstacle_collision": "已确认 / 保留轮廓避让", "obstacles": "建筑占用避让",
    "facility_collisions": "设施之间的占地关系", "pairwise": "设施之间的占地关系",
    "road_facility_collision": "道路与设施占地关系", "road_axis": "道路正交参照轴",
    "orientation": "主轴方向约束", "areas": "设施面积核查", "area_targets": "设施面积目标",
    "road_connectivity": "路网连通性", "facility_access": "设施道路连接",
    "source_preservation": "原生实体保留", "unchanged_source": "源文件字节保持不变",
    "saved_geometry": "重新读取实际 DXF 几何", "road_width": "道路实际宽度",
    "free_reserve": "剩余调整空间", "uncertain_outlines": "待定轮廓关系",
    "uncertain_keepouts": "待核实区域临时避让",
    "baseline_preservation": "原图实体与坐标保持",
    "spatial_references": "场界与分级避让来源",
    "actual_component_geometry": "实际新增实体重读",
    "occupied_geometry_constraints": "完整占地与净距约束",
    "facility_program": "六类设施面积与拆分",
    "road_and_frontage": "路网连通与设施接近距离",
}


def _groups(program):
    value = program.get("facilities", [])
    if isinstance(value, dict):
        return [{"id": key, **item} for key, item in value.items()]
    return value


def _number(value, fallback=0):
    try:
        result = float(value)
    except (ValueError, TypeError):
        return fallback
    return result if math.isfinite(result) else fallback


def _wrapped(value, width):
    """Wrap CJK at the same apparent width as Latin, without global font hacks."""
    import unicodedata
    lines, line, used = [], "", 0
    for char in str(value):
        size = 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        if char == "\n" or used + size > width:
            lines.append(line)
            line, used = "", 0
        if char != "\n":
            line += char
            used += size
    if line:
        lines.append(line)
    return "\n".join(lines)


def _read_actual(dxf_path, report):
    doc, checksum = _read(dxf_path)
    expected = report.get("target_sha256", report.get("file_sha256"))
    if expected and checksum != expected:
        raise ValueError("Report target hash differs from the delivered DXF; refusing a stale figure.")
    extracted = extract_drawing(dxf_path)
    if extracted["source_sha256"] != checksum:
        raise ValueError("The DXF changed while its actual geometry was being reread.")
    polygons = {str(item["id"]): item for item in extracted["polygons"]}
    scene = report["scene"]
    boundary_id = str(scene["boundary_id"])
    if boundary_id not in polygons:
        raise ValueError(f"Boundary {boundary_id} is absent from the actual output DXF.")
    boundary = shape(polygons[boundary_id]["geometry"])
    origin = scene.get("origin_wcs", scene.get("origin", boundary.bounds[:2]))
    factor = float(scene["metres_per_unit"])
    if factor <= 0 or not math.isfinite(factor):
        raise ValueError("scene.metres_per_unit must be positive and finite.")
    def local(geometry):
        return scale(translate(geometry, -origin[0], -origin[1]), factor, factor, origin=(0, 0))
    obstacles = []
    for item in scene.get("obstacles", scene.get("obstacle_ids", [])):
        item = {"id": item} if isinstance(item, str) else item
        identifier = str(item["id"])
        if identifier not in polygons:
            raise ValueError(f"Obstacle {identifier} is absent from the actual output DXF.")
        obstacles.append((item, local(shape(polygons[identifier]["geometry"]))))
    components = []
    for entity in doc.modelspace():
        meta = _metadata(entity)
        if not meta or meta.get("role") not in {"road", "facility"}:
            continue
        actual = occupied_polygon(doc, entity.dxf.handle, meta)
        centerline = None
        if meta["role"] == "road" and not entity.closed:
            from shapely.geometry import LineString
            centerline = local(LineString([(p.x, p.y) for p in entity.vertices_in_wcs()]))
        components.append({"metadata": meta, "geometry": local(actual),
                           "centerline": centerline, "area_m2": actual.area * factor ** 2})
    obstacle_ids = {str(item["id"]) for item, _ in obstacles}
    occupied_handles = {item["metadata"]["handle"] for item in components}
    context = [local(shape(item["geometry"])) for item in extracted["polygons"]
               if item["id"] != boundary_id and item["id"] not in obstacle_ids
               and item.get("source", {}).get("root_handle") not in occupied_handles
               and shape(item["geometry"]).intersects(boundary.envelope)]
    facilities = _groups(report.get("program", {}))
    if not facilities:
        seen = {}
        for item in components:
            meta = item["metadata"]
            if meta["role"] == "facility":
                identifier = str(meta.get("facility_id", meta.get("kind", meta.get("id"))))
                seen.setdefault(identifier, {"id": identifier, "name": meta.get("name", identifier)})
        facilities = list(seen.values())
    areas, numbers = defaultdict(float), defaultdict(int)
    for item in components:
        meta = item["metadata"]
        if meta["role"] == "facility":
            identifier = str(meta.get("facility_id", meta.get("kind", meta.get("id"))))
            areas[identifier] += item["area_m2"]
            numbers[identifier] += 1
    road_geometries = [item["geometry"] for item in components if item["metadata"]["role"] == "road"]
    retained = [(item, geom) for item, geom in obstacles if item.get("constraint_tier") != "uncertain_keepout"]
    uncertain = [(item, geom) for item, geom in obstacles if item.get("constraint_tier") == "uncertain_keepout"]
    retained_union = unary_union([geom for _, geom in retained]).intersection(local(boundary))
    uncertain_increment = unary_union([geom for _, geom in uncertain]).intersection(local(boundary)).difference(retained_union)
    return {"boundary": local(boundary), "obstacles": obstacles, "components": components,
            "context": context, "origin": origin, "scale": factor, "sha256": checksum,
            "facilities": facilities, "areas": areas, "numbers": numbers,
            "road_area_m2": unary_union(road_geometries).area if road_geometries else 0,
            "boundary_area_m2": boundary.area * factor ** 2,
            "obstacle_area_m2": unary_union([geom for _, geom in obstacles]).intersection(local(boundary)).area,
            "retained_building_area_m2": retained_union.area,
            "uncertain_reserved_area_m2": uncertain_increment.area,
            "retained_count": len(retained), "uncertain_keepout_count": len(uncertain)}


def _header(fig, title, subtitle, right="白桥 · v0009 / 部分 B"):
    fig.text(.045, .958, title, fontsize=23, weight="bold", color=INK, va="top")
    fig.text(.045, .917, subtitle, fontsize=10.5, color="#526b83", va="top")
    fig.text(.956, .953, right, fontsize=12, color=BLUE, ha="right", va="top")


def _footer(fig, report, actual, page):
    fig.add_artist(plt.Line2D([.045, .957], [.106, .106], transform=fig.transFigure,
                             color="#dbe5ef", lw=1))
    fig.text(.045, .09,
             f"同源输出 DXF：SHA-256 {actual['sha256'][:24]}…  |  所有新增占地均由交付 DXF 重读量测  |  {page}",
             fontsize=9, color="#526b83", va="top")
    fig.text(.045, .064,
             f"单位采用 {actual['scale']:.8g} m / 原生单位；原坐标与朝向保留，显示平移原点 ({actual['origin'][0]:.3f}, {actual['origin'][1]:.3f})。",
             fontsize=9, color="#526b83", va="top")
    fig.text(.045, .037,
             "二维几何研究方案：实体占地与正交道路已量测；未验收车辆转弯、消防、三维空间或吊装服务。",
             fontsize=9.7, color=INK, weight="bold", va="top")


def _overview(report, actual, page_count=2):
    fig = plt.figure(figsize=(18, 11))
    _header(fig, "部分 B · 道路与临建设施布置", "规则配置 → 空间事实 → 实际 DXF → 同源几何复核  |  SITE LAYOUT STUDY")
    fig.text(.956, .923, "原世界方向 · 六类设施 · 不含塔吊", fontsize=10, color="#526b83", ha="right", va="top")
    ax = fig.add_axes([.055, .165, .605, .69])
    ax.set_facecolor("#f8fafc")
    for geom in actual["context"]:
        _draw_geometry(ax, geom, edgecolor="#aab8c6", linewidth=.35, alpha=.4, zorder=1)
    for item, geom in actual["obstacles"]:
        if item.get("constraint_tier") == "uncertain_keepout":
            # These may be buildings or other objects. Reserving their footprint
            # does not confirm identity; do not give them building labels.
            _draw_geometry(ax, geom, edgecolor="#c79a43", facecolor="#fff1d6", linewidth=.85,
                           linestyle=(0, (4, 3)), alpha=.70, zorder=2)
            continue
        confirmed = item.get("human_confirmed", item.get("status") == "confirmed")
        _draw_geometry(ax, geom, edgecolor="#316da3" if confirmed else "#7895ad",
                       facecolor="#dbe9f6" if confirmed else "#e7edf2", linewidth=1.2, zorder=3)
        if geom.area > 1500 or confirmed:
            point = geom.representative_point()
            label = str(item.get("label", item.get("name", item["id"].split("/")[-1])))
            label = label.replace("标准厂房", "厂房")
            if "未分类" in label:
                label = f"保留 {item['id'].split('/')[-1]}"
            ax.text(point.x, point.y, label, ha="center", va="center", fontsize=8.8,
                    color="#215584", weight="bold", zorder=10,
                    bbox={"boxstyle": "round,pad=.22", "fc": "white", "ec": "#bbcfdf", "lw": .6, "alpha": .93})
    styles = {str(item["id"]): (COLORS[i % len(COLORS)], FILLS[i % len(FILLS)], f"F{i+1:02}")
              for i, item in enumerate(actual["facilities"])}
    for item in actual["components"]:
        meta, geom = item["metadata"], item["geometry"]
        if meta["role"] == "road":
            _draw_geometry(ax, geom, edgecolor="#567286", facecolor="#b9cbd7", linewidth=.65, zorder=5)
            _draw_geometry(ax, item["centerline"], edgecolor="white", linewidth=.6, linestyle=(0, (5, 3)), zorder=6)
        else:
            identifier = str(meta.get("facility_id", meta.get("kind", meta.get("id"))))
            color, fill, short = styles.get(identifier, ("#8255a8", "#ece2f4", str(meta.get("id", "F"))))
            _draw_geometry(ax, geom, edgecolor=color, facecolor=fill, linewidth=1.35, zorder=7)
            point = geom.representative_point()
            name = str(meta.get("short_name", meta.get("name", identifier)))
            if len(name) > 10:
                name = name[:9] + "…"
            ax.text(point.x, point.y, f"{short}\n{name}", ha="center", va="center", fontsize=8.2,
                    color=color, weight="bold", zorder=12,
                    bbox={"boxstyle": "round,pad=.19", "fc": "white", "ec": "none", "alpha": .87})
    _draw_geometry(ax, actual["boundary"], edgecolor=ORANGE, linewidth=1.7,
                   linestyle=(0, (6, 3)), zorder=9)
    bounds = actual["boundary"].bounds
    span = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
    pad = span * .055
    ax.set_xlim(bounds[0] - pad, bounds[2] + pad)
    ax.set_ylim(bounds[1] - pad, bounds[3] + pad)
    ax.set_aspect("equal", adjustable="box")
    ax.grid(color="#d8e1eb", linewidth=.5, alpha=.8)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_color("#d3dfeb")
    ax.tick_params(labelsize=9, colors="#61758a", length=3)
    ax.set_xlabel("X − X₀  [m，采用单位假设]", fontsize=10, color="#526b83", labelpad=9)
    ax.set_ylabel("Y − Y₀  [m，采用单位假设]", fontsize=10, color="#526b83", labelpad=9)
    ax.set_title("彩色为实际新增实体；蓝灰轮廓约束占地，琥珀区域临时避让", loc="left", fontsize=10.5, color=INK, pad=13)
    legend = ax.legend(handles=[
        Patch(facecolor="#dbe9f6", edgecolor="#316da3", label="人工确认建筑"),
        Patch(facecolor="#e7edf2", edgecolor="#7895ad", label="保留轮廓候选"),
        Patch(facecolor="#fff1d6", edgecolor="#c79a43", linestyle="--", label="待核实临时避让"),
        Line2D([], [], color=ORANGE, linestyle="--", label="采用的作业范围"),
    ], loc="upper left", ncol=2, fontsize=8.2, framealpha=.95, facecolor="white",
        edgecolor="#dbe5ef", columnspacing=1.4, borderpad=.7, handlelength=1.8)
    legend.set_zorder(20)
    angle = float(report["scene"].get("angle_deg", 0))
    base = (bounds[0] + span * .11, bounds[3] - span * .18)
    for i, theta in enumerate((angle, angle + 90)):
        rad = math.radians(theta)
        end = (base[0] + span * .085 * math.cos(rad), base[1] + span * .085 * math.sin(rad))
        ax.annotate("", xy=end, xytext=base, arrowprops={"arrowstyle": "-|>", "lw": 1.8, "color": BLUE}, zorder=15)
        ax.annotate(f"{theta % 180:.3f}°", xy=end, xytext=(4 if i == 0 else -4, 4), textcoords="offset points",
                    color=BLUE, fontsize=9, ha="left" if i == 0 else "right", zorder=15)
    ax.text(base[0], base[1] - span * .025, "道路正交参照轴", ha="center", va="top", fontsize=9, color="#526b83")
    panel = fig.add_axes([.704, .166, .253, .69])
    panel.set_axis_off()
    _panel_text(panel, 0, 1, "01  六类设施 · 实际占地", size=13, weight="bold")
    _panel_text(panel, .03, .942, "类别", size=9, color="#63768a")
    _panel_text(panel, .61, .942, "目标 / 实际 [m²]", size=9, color="#63768a")
    for i, item in enumerate(actual["facilities"]):
        y = .900 - i * .086
        identifier = str(item["id"])
        color, fill, short = styles[identifier]
        panel.add_patch(FancyBboxPatch((0, y - .064), 1, .07, boxstyle="round,pad=.007",
                                      transform=panel.transAxes, facecolor=fill, edgecolor="none"))
        name = item.get("name", identifier)
        target = item.get("target_area_m2", item.get("selected_area_m2", item.get("area_m2")))
        target_label = "—" if target is None else f"{_number(target):,.0f}"
        _panel_text(panel, .025, y - .009, f"{short}  {name}", size=9.8, color=color, weight="bold")
        _panel_text(panel, .63, y - .010, f"{target_label} / {actual['areas'][identifier]:,.0f}", size=9.5, color=INK)
        _panel_text(panel, .04, y - .036, f"实际 CAD 组件：{actual['numbers'][identifier]} 块", size=8.0, color="#64778a")
    _panel_text(panel, 0, .353, "02  道路与范围", size=13, weight="bold")
    road_target = report.get("program", {}).get("road_target_area_m2", report.get("program", {}).get("road", {}).get("target_area_m2"))
    road_target_text = "" if road_target is None else f" / 软参考 {_number(road_target):,.0f} m²"
    _panel_text(panel, 0, .304,
                f"道路实际并集：{actual['road_area_m2']:,.0f} m²{road_target_text}\n"
                f"作业范围面积：{actual['boundary_area_m2']:,.0f} m²\n"
                f"建筑及保留轮廓：{actual['retained_building_area_m2']:,.0f} m² · {actual['retained_count']} 项\n"
                f"另计临时避让：{actual['uncertain_reserved_area_m2']:,.0f} m² · {actual['uncertain_keepout_count']} 项",
                size=9.1, color="#38516c", linespacing=1.65)
    _panel_text(panel, 0, .141, "03  本轮采用的条件", size=13, weight="bold")
    _panel_text(panel, 0, .094,
                "采用面积证据支持的单位比例；保留头单位冲突。\n"
                f"{report['scene']['boundary_id']} 作为研究范围，不证明法定用地属性。\n"
                "临时避让不确认建筑身份，也不计入建筑面积。\n"
                "面积比例为场地估算规则；不足项不静默缩减。\n"
                "塔吊 / 提升设备不在本轮范围，核查详见第 2 页。",
                size=8.1, color="#63768a", linespacing=1.5)
    _footer(fig, report, actual, f"1 / {page_count}")
    return fig


def _check_detail(key, item, report):
    """Readable summaries of measured check data, with failures kept explicit."""
    if not isinstance(item, dict):
        return None
    if key == "baseline_preservation":
        return (f"重读对照 {item.get('baseline_snapshot_count', '—')} 个原生对象；核对坐标与实体一致性。"
                f"源 / 输出头单位代码：{item.get('source_insunits', '—')} / {item.get('target_insunits', '—')}。")
    if key == "spatial_references":
        obstacles = report.get("scene", {}).get("obstacles", [])
        uncertain = sum(o.get("constraint_tier") == "uncertain_keepout" for o in obstacles)
        return (f"作业范围 {item.get('boundary_id', '—')}；确认及保留轮廓 {len(obstacles)-uncertain} 项，"
                f"待核实临时避让 {uncertain} 项。各轮廓均追溯到实际 DXF。")
    if key == "actual_component_geometry":
        return f"从输出 DXF 重新读取 {item.get('component_count', '—')} 个新增道路 / 设施实体，检查实际几何与元数据。"
    if key == "occupied_geometry_constraints":
        return "检查完整占地越界、建筑及临时避让区、设施相交与配置净距；道路交会按通行关系处理。"
    if key == "facility_program":
        facilities = item.get("facilities", [])
        total = sum(_number(f.get("actual_area_m2")) for f in facilities)
        target = sum(_number(f.get("target_area_m2")) for f in facilities)
        parts = sum(int(_number(f.get("part_count"))) for f in facilities)
        return f"{len(facilities)} 类设施，{parts} 个实际分块；目标 / 实际总面积 {target:,.2f} / {total:,.2f} m²。逐类核对，目标未缩减。"
    if key == "road_and_frontage":
        frontage = item.get("frontage", [])
        gap = max((_number(f.get("gap_m")) for f in frontage), default=0)
        maximum = report.get("program", {}).get("planning", {}).get("frontage_max_gap_m", 0)
        failed = [str(f.get("id", "?")) for f in frontage
                  if not f.get("straight_connector_clear", False) or _number(f.get("gap_m")) > maximum + 1e-5]
        result = (f"路网{'连通' if item.get('road_connected') else '未连通'}；{len(frontage)} 处分块连接，"
                  f"最大间距 {gap:.2f} m / 限值 {maximum:.2f} m。")
        return result + (f" 未满足：{', '.join(failed)}。" if failed else "直连接线无占地遮挡；未验证车辆转弯。")
    return None


def _decision_text(item):
    if not isinstance(item, dict):
        return str(item)
    key = item.get("item")
    if key == "units":
        conflict = "，同时记录文件头单位冲突" if item.get("header_conflict") else ""
        return f"单位：依据面积证据自主采用 {item.get('value', '—')} m / 原生单位{conflict}；保留原图坐标。"
    if key == "working_boundary":
        return f"范围：采用 {item.get('value', '—')} 作为本轮作业边界，不据此证明法定用地属性。"
    if key == "obstacles":
        return f"占地：{item.get('count', '—')} 项确认轮廓与保留候选作为实际避让面，场界外面积不纳入预算。"
    if key == "uncertain_keepouts":
        return (f"待核实区域：{item.get('count', '—')} 项暂时保护，增量面积另计；"
                "不确认建筑身份，也不增加道路服务目标。")
    if key == "other_objects":
        return "研究范围：停车标线等不作为建筑障碍；剩余未知对象不等于已证明的可用空地。"
    value = item.get("message", item.get("reason", item.get("decision", item.get("description"))))
    return value if value is not None else json.dumps(item, ensure_ascii=False)


def _check_rows(report):
    checks = report.get("checks", {})
    if isinstance(checks, dict) and "items" in checks:
        checks = checks["items"]
    if isinstance(checks, dict):
        entries = list(checks.items())
    else:
        entries = [(str(item.get("id", item.get("code", f"check_{i+1}"))), item) for i, item in enumerate(checks)]
    rows = []
    for key, item in entries:
        if isinstance(item, bool):
            status, details = ("passed" if item else "failed"), ""
        elif isinstance(item, dict):
            status = item.get("status", "unknown")
            detail = item.get("message", item.get("summary", item.get("detail", ""))) or _check_detail(key, item, report)
            if not detail:
                interesting = {k: v for k, v in item.items() if k not in {"status", "id", "code", "name", "label", "issues"}}
                detail = json.dumps(interesting, ensure_ascii=False) if interesting else ""
            details = str(detail)
        else:
            status, details = "unknown", str(item)
        name = item.get("name", item.get("label", CHECK_NAMES.get(key, key))) if isinstance(item, dict) else CHECK_NAMES.get(key, key)
        rows.append([name, STATUS_NAMES.get(str(status), str(status)), details])
    return rows


def _audit(report, actual, rows=None, page_number=2, page_count=2):
    fig = plt.figure(figsize=(18, 11))
    _header(fig, "部分 B · 实际文件检查与决策记录", "检查绑定当前输出文件；几何通过不等同于施工规范或施工组织验收  |  DELIVERABLE AUDIT")
    rows = _check_rows(report) if rows is None else rows
    panel = fig.add_axes([.046, .38, .912, .46])
    panel.set_axis_off()
    _panel_text(panel, 0, 1.06, "01  逐项检查", size=13, weight="bold")
    if not rows:
        rows = [["实际 DXF 重读", "完成", "图形与面积来自保存后的 CAD 文件，独立检查结果请见配套 JSON。"]]
    rows = [[_wrapped(a, 37), b, "\n".join(_wrapped(c, 127).splitlines()[:2]) + (" …" if len(_wrapped(c, 127).splitlines()) > 2 else "")] for a, b, c in rows[:16]]
    table = panel.table(cellText=rows, colLabels=["检查项", "结果", "量测 / 说明"],
                        colWidths=[.235, .085, .68], cellLoc="left", colLoc="left", bbox=[0, 0, 1, 1])
    table.auto_set_font_size(False)
    table.set_fontsize(8.9)
    for (row, col), cell in table.get_celld().items():
        cell.set_edgecolor("#d9e4ee")
        cell.set_linewidth(.6)
        cell.PAD = .06
        if row == 0:
            cell.set_facecolor("#e6eff8")
            cell.set_text_props(color=INK, weight="bold")
        else:
            cell.set_facecolor("white" if row % 2 else "#f6f9fc")
            cell.set_text_props(color=INK)
            if col == 1:
                text = rows[row - 1][1]
                cell.set_text_props(color="#246b89" if text == "通过" else "#a06b27", weight="bold")
    _panel = fig.add_axes([.045, .145, .913, .185])
    _panel.set_axis_off()
    _panel_text(_panel, 0, 1, "02  决策与边界", size=13, weight="bold")
    decisions = report.get("scene", {}).get("decisions", report.get("decisions", []))
    if isinstance(decisions, dict):
        decisions = [f"{key}：{value}" for key, value in decisions.items()]
    lines = [_decision_text(item) for item in decisions]
    if not lines:
        lines = ["面积比例按本次场地估算，不代替建筑面积口径；实际尺寸由输出 DXF 重读。",
                 "单位与范围由系统采用并记录，确认轮廓与保留候选均作为保守避让范围。",
                 "这是规则和几何计算的独立试验，尚未证明全场语义完整或工程可实施。"]
    # Keep the figure readable; the accompanying JSON contains complete detail.
    joined = "\n".join(f"• {value}" for value in lines[:5])
    wrapped = _wrapped(joined, 164).splitlines()
    if len(wrapped) > 8:
        wrapped = wrapped[:7] + ["完整决策与未满足项见配套 JSON 检查报告。"]
    _panel_text(_panel, 0, .74, "\n".join(wrapped), size=9.4, color="#526b83", linespacing=1.6)
    _footer(fig, report, actual, f"{page_number} / {page_count}")
    return fig


def render_site_layout_report(dxf_path: str | Path, report: dict, output_dir: str | Path,
                              prefix: str = "v0009_site_layout") -> dict[str, str]:
    """Save a vector PDF audit plus editable-text SVG and overview PNG.

    The usual report has two pages; longer check lists receive extra audit pages.

    No CAD write operation occurs here. A hash mismatch, missing source instance
    or unsupported tagged geometry aborts rather than drawing the planned shape.
    """
    if Path(prefix).name != prefix:
        raise ValueError("prefix must be a filename stem, not a path")
    actual = _read_actual(dxf_path, report)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {ext: output_dir / f"{prefix}.{ext}" for ext in ("svg", "png", "pdf")}
    with matplotlib.rc_context(RC):
        check_rows = _check_rows(report)
        chunks = [check_rows[i:i + 16] for i in range(0, len(check_rows), 16)] or [[]]
        first = _overview(report, actual, len(chunks) + 1)
        audit_pages = [_audit(report, actual, rows, i + 2, len(chunks) + 1)
                       for i, rows in enumerate(chunks)]
        try:
            first.savefig(paths["svg"], format="svg", metadata={"Creator": "CAD-Agent; geometry reread from delivered DXF"})
            first.savefig(paths["png"], dpi=180)
            with PdfPages(paths["pdf"], metadata={"Title": "CAD-Agent / v0009 / site layout and file audit",
                                                   "Subject": f"Actual DXF SHA-256: {actual['sha256']}"}) as pdf:
                pdf.savefig(first)
                for page in audit_pages:
                    pdf.savefig(page)
        finally:
            plt.close(first)
            for page in audit_pages:
                plt.close(page)
    return {key: str(path) for key, path in paths.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dxf")
    parser.add_argument("report")
    parser.add_argument("--output", default="dev_log_0929")
    parser.add_argument("--prefix", default="v0009_site_layout")
    args = parser.parse_args()
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    print(json.dumps(render_site_layout_report(args.dxf, report, args.output, args.prefix), ensure_ascii=False))


if __name__ == "__main__":
    main()
