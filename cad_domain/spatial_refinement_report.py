"""Read-only vector review of direction evidence and footprint classification.

Plots preserve source orientation and translate coordinates only for display.
The PDF includes a source-by-source audit sheet; figures never modify the DXF.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import FancyBboxPatch
from shapely.affinity import translate
from shapely.geometry import shape

from .spatial_report import INK, BLUE, GRAY, ORANGE, _draw_geometry, _panel_text

TEAL = "#087f94"
PURPLE = "#8255a8"
RUST = "#bb6140"
PALETTE = {"retained": TEAL, "suppressed": RUST, "uncertain": PURPLE}
CATEGORY_NAMES = {"retained": "保留候选", "suppressed": "抑制轮廓", "uncertain": "待定轮廓"}
RC = {"font.family": ["Noto Sans CJK SC", "DejaVu Sans"],
      "axes.unicode_minus": False, "svg.fonttype": "none", "pdf.fonttype": 42,
      "ps.fonttype": 42, "font.size": 10, "figure.facecolor": "white",
      "savefig.facecolor": "white"}


def _value(value, precision=3):
    return "—" if value is None else f"{float(value):.{precision}f}"


def _is_anchor(item):
    return bool(item.get("human_confirmed"))


def _short_id(value):
    return str(value).split("/")[-1]


def _review_items(refinement):
    return [item for item in refinement.get("classifications", []) if not _is_anchor(item)]


def _priority(item):
    value = item.get("display_priority", 1)
    if isinstance(value, (int, float)):
        return float(value)
    return {"major": 2, "minor": 0, "high": 3, "medium": 2, "low": 1, "background": 0}.get(str(value), 1)


def _focus_items(refinement):
    candidates = [item for item in _review_items(refinement) if _priority(item) > 0
                  and "PUB_TAB" not in item.get("layer", "")]
    retained = sorted((item for item in candidates if item.get("category") == "retained"), key=lambda item: item["id"])
    suppressed = sorted((item for item in candidates if item.get("category") == "suppressed"),
                        key=lambda item: (-len(item.get("enclosing_candidates", [])), item["id"]))[:8]
    uncertain = sorted((item for item in candidates if item.get("category") == "uncertain"),
                       key=lambda item: (item.get("original_layer_candidate", False),
                                         "partial_overlap_candidate" not in item.get("reason_codes", []),
                                         item["id"]))
    return (retained + suppressed + uncertain)[:48]


def _overview(facts, refinement):
    origin = facts["scope"]["origin"]
    bounds = facts["scope"]["bounds_native"]
    classes = refinement.get("classifications", [])
    layer_items = _review_items(refinement)
    counts = Counter(item.get("category", "uncertain") for item in layer_items)
    anchors = [item for item in classes if _is_anchor(item)]
    axis = refinement.get("axis_frame", {})
    axis_deg = axis.get("axis_deg")
    orthogonal = axis.get("orthogonal_axis_deg")
    axis_status = {"supported": "方向证据一致", "conflict": "方向冲突待核实", "unresolved": "方向尚未建立"}.get(axis.get("status"), "方向候选")
    fig = plt.figure(figsize=(18, 11))
    fig.text(.045, .957, "部分 A 完善 / B 前置 · 方向与轮廓核对", fontsize=23,
             color=INK, weight="bold", va="top")
    fig.text(.045, .915, "已审查轮廓 → 正交参照轴 → 空间关系 → 分级约束  |  AXIS & FOOTPRINT AUDIT",
             fontsize=10.5, color="#526b83", va="top")
    fig.text(.956, .953, "白桥 · v0009", fontsize=13, color=BLUE, ha="right", va="top")
    fig.text(.956, .923, "原坐标 / 原方向 · 未修改源 DXF", fontsize=10,
             color="#526b83", ha="right", va="top")
    ax = fig.add_axes([.055, .165, .605, .69])
    ax.set_facecolor("#f8fafc")
    for feature in facts["features"]:
        props = feature["properties"]
        if props["kind"] not in {"context_outline", "boundary_candidate"}:
            continue
        geom = translate(shape(feature["geometry"]), -origin[0], -origin[1])
        boundary = props["kind"] == "boundary_candidate"
        _draw_geometry(ax, geom, edgecolor=ORANGE if boundary else GRAY,
                       linewidth=1.55 if boundary else .4,
                       linestyle=(0, (6, 3)) if boundary else "-",
                       alpha=.85 if boundary else .48, zorder=3 if boundary else 1)
    for item in sorted(classes, key=lambda item: not _is_anchor(item)):
        geom = translate(shape(item["geometry"]), -origin[0], -origin[1])
        category = item.get("category", "uncertain")
        anchor = _is_anchor(item)
        color = BLUE if anchor else PALETTE.get(category, PURPLE)
        fill = "#e3effc" if anchor else {"retained": "#c8ecee", "suppressed": "#f9ddd0", "uncertain": "#e7dff2"}.get(category, "none")
        priority = _priority(item)
        if not anchor and (priority <= 0 or "PUB_TAB" in item.get("layer", "")):
            color, fill, priority = "#a8b7c5", "none", 0
        if category == "uncertain":
            fill = "none"
        _draw_geometry(ax, geom, edgecolor=color, facecolor=fill if anchor or priority > 1 else "none",
                       linewidth=1.6 if anchor else (1.05 if priority > 0 else .35),
                       linestyle=(0, (3, 1.5)) if category == "suppressed" else "-",
                       alpha=.92 if anchor or priority > 0 else .45, zorder=4 if anchor else (6 if priority > 0 else 2))
        if anchor:
            point = geom.representative_point()
            label = str(item.get("label", item["id"]))
            ax.text(point.x, point.y, f"{label}\n轮廓已人工确认", ha="center", va="center",
                    fontsize=9.3, color="#114e90", weight="bold", zorder=10,
                    bbox={"boxstyle": "round,pad=.28", "fc": "white", "ec": "#abc9ec", "lw": .65, "alpha": .94})
        elif category == "retained" and float(item.get("area_native2", 0)) > 1000:
            point = geom.representative_point()
            ax.text(point.x, point.y, f"{_short_id(item['id'])}\n几何支持 · 身份待核实", ha="center", va="center",
                    fontsize=8.5, color=TEAL, zorder=10,
                    bbox={"boxstyle": "round,pad=.25", "fc": "white", "ec": "#81bcc6", "lw": .6, "alpha": .94})
    # Label the longest supporting edges, not every segment of the boundary.
    supporting = [edge for edge in refinement.get("boundary_edges", []) if edge.get("supports_frame")]
    for edge in sorted(supporting, key=lambda edge: edge.get("length_native", 0), reverse=True)[:3]:
        a, b = edge["start"], edge["end"]
        x, y = (a[0] + b[0]) / 2 - origin[0], (a[1] + b[1]) / 2 - origin[1]
        ax.plot([a[0] - origin[0], b[0] - origin[0]], [a[1] - origin[1], b[1] - origin[1]],
                color="#2279a8", lw=2.5, zorder=7, solid_capstyle="round")
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy)
        nx, ny = (-dy / length, dx / length) if length else (0, 1)
        center_x, center_y = (bounds[0] + bounds[2]) / 2 - origin[0], (bounds[1] + bounds[3]) / 2 - origin[1]
        if nx * (x - center_x) + ny * (y - center_y) < 0:
            nx, ny = -nx, -ny
        ax.annotate(f"E{str(edge['id']).rsplit(':', 1)[-1]}  ·  {_value(edge.get('angle_deg'), 2)}°",
                    xy=(x, y), xytext=(30 * nx, 30 * ny), textcoords="offset points",
                    fontsize=9, color="#226990", ha="center", va="center", zorder=11,
                    arrowprops={"arrowstyle": "-", "color": "#2279a8", "lw": .65},
                    bbox={"boxstyle": "round,pad=.28", "fc": "#f5fbff", "ec": "#9bbdd2", "lw": .6})
    span = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
    pad = span * .055
    ax.set_xlim(bounds[0] - origin[0] - pad, bounds[2] - origin[0] + pad)
    ax.set_ylim(bounds[1] - origin[1] - pad, bounds[3] - origin[1] + pad)
    ax.set_aspect("equal", adjustable="box")
    ax.grid(color="#d8e1eb", linewidth=.5, alpha=.8)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_color("#d3dfeb")
    ax.tick_params(labelsize=9, colors="#61758a", length=3)
    ax.set_xlabel("X − X₀  [原生坐标单位]", fontsize=10, color="#526b83", labelpad=9)
    ax.set_ylabel("Y − Y₀  [原生坐标单位]", fontsize=10, color="#526b83", labelpad=9)
    ax.set_title("蓝色大轮廓为人工确认建筑；小框按证据分类，空白不代表可布置", loc="left",
                 fontsize=10.5, color=INK, pad=13)
    if axis_deg is not None and orthogonal is not None:
        # Data coordinates give equal-length arrows at their actual WCS angles.
        center = (bounds[0] - origin[0] + span * .12, bounds[3] - origin[1] - span * .20)
        arrow_length = span * .105
        for angle, color, offset in [(axis_deg, TEAL, (5, 4)), (orthogonal, BLUE, (-5, 4))]:
            rad = math.radians(angle)
            end = (center[0] + math.cos(rad) * arrow_length, center[1] + math.sin(rad) * arrow_length)
            ax.annotate("", xy=end, xytext=center, arrowprops={"arrowstyle": "-|>", "lw": 2.2, "color": color}, zorder=12)
            ax.annotate(f"{angle:.3f}°", xy=end, xytext=offset, textcoords="offset points",
                        fontsize=10, color=color, ha="left" if offset[0] > 0 else "right", va="bottom", zorder=12)
        ax.text(center[0], center[1] - span * .024, "正交参照轴 · 非北向", ha="center", va="top", fontsize=9, color="#526b83", zorder=12)
    # Only a few source handles are annotated; the PDF audit sheet carries all.
    examples = []
    for category in ("retained", "uncertain", "suppressed"):
        chosen = sorted((item for item in layer_items if item.get("category") == category and _priority(item) > 0
                         and "PUB_TAB" not in item.get("layer", "")
                         and float(item.get("area_native2", 0)) < (1000 if category == "retained" else 10000)),
                        key=lambda item: float(item.get("area_native2", 0)), reverse=True)
        examples.extend(chosen[:1])
    for idx, item in enumerate(examples):
        geom = translate(shape(item["geometry"]), -origin[0], -origin[1])
        point = geom.representative_point()
        category = item["category"]
        color = PALETTE[category]
        ax.annotate(f"{_short_id(item['id'])} · {CATEGORY_NAMES[category]}", xy=(point.x, point.y),
                    xytext=(-45 if idx % 2 else 35, -36 if idx % 2 else 32), textcoords="offset points",
                    fontsize=8.5, color=color, zorder=13,
                    arrowprops={"arrowstyle": "-", "color": color, "lw": .7},
                    bbox={"boxstyle": "round,pad=.28", "fc": "white", "ec": color, "lw": .6, "alpha": .96})
    panel = fig.add_axes([.703, .166, .254, .69])
    panel.set_axis_off()
    _panel_text(panel, 0, 1, "01  已确认轮廓与方向证据", size=13, weight="bold")
    _panel_text(panel, 0, .945, f"人工审查确认：{len(anchors)} 个建筑轮廓\n参照轴：{_value(axis_deg)}° / {_value(orthogonal)}°",
                size=10.5, linespacing=1.65)
    _panel_text(panel, 0, .845, f"{axis_status}；场界性质仍待确认。\n长短边和各段方向分别保留，不将全图转正。",
                size=9, color="#526b83", linespacing=1.7)
    _panel_text(panel, 0, .742, f"02  全场扫描 · {len(classes):,} 个闭合轮廓", size=13, weight="bold")
    y = .682
    for category in ("retained", "suppressed", "uncertain"):
        color = PALETTE[category]
        panel.add_patch(FancyBboxPatch((0, y - .075), 1, .065, boxstyle="round,pad=.01",
                                     transform=panel.transAxes, facecolor="#f7f9fc", edgecolor="#e0e7ef", lw=.7))
        _panel_text(panel, .032, y - .019, CATEGORY_NAMES[category], size=11, color=color, weight="bold")
        _panel_text(panel, .855, y - .015, str(counts[category]), size=17, color=color, weight="bold")
        y -= .096
    summary = refinement.get("summary", {})
    original_counts = summary.get("layer_counts", {})
    minor_count = summary.get("minor_uncertain", 0)
    _panel_text(panel, 0, .365,
                f"方向一致不等于建筑身份；灰色为低证据背景。\n待定项含 {minor_count:,} 个细节 / 符号候选，非建筑栋数。\n原图层 {summary.get('layer_candidates', '?')} 项：{original_counts.get('retained', 0)} 保留 / {original_counts.get('suppressed', 0)} 抑制 / {original_counts.get('uncertain', 0)} 待定。",
                size=8.8, color="#7d5030", linespacing=1.65)
    _panel_text(panel, 0, .265, "03  对后续场布的含义", size=13, weight="bold")
    _panel_text(panel, 0, .21,
                "已确认轮廓：提供稳定的空间参照。\n保留候选：保守避让，待核实角色与施工阶段。\n抑制轮廓：不再独立计作建筑，保留来源。\n待定轮廓：禁止直接当作空地，需追加核实。",
                size=9.5, color="#38516c", linespacing=1.8)
    _panel_text(panel, 0, .045, "仅建立方向与分类；未生成可用场地或布置成果。", size=9, color="#63768a")
    _footer(fig, facts, origin,
            "“抑制”只从派生建筑候选集中排除，不删除源图实体；精选核对清单见第 2 页，完整逐项证据见配套 JSON。")
    return fig


def _footer(fig, facts, origin, note):
    fig.add_artist(plt.Line2D([.045, .957], [.105, .105], transform=fig.transFigure,
                             color="#dbe5ef", linewidth=1))
    fig.text(.045, .089, f"固定输入：v0009  |  SHA-256：{facts['source'].get('sha256', '?')[:24]}…  |  单位声明冲突仍保留，面积均为原生单位²",
             fontsize=9, color="#526b83", va="top")
    fig.text(.045, .063, f"显示原点 X₀={origin[0]:,.3f}, Y₀={origin[1]:,.3f}；只做显示平移，未改变几何坐标、源图内容或朝向。",
             fontsize=9, color="#526b83", va="top")
    fig.text(.045, .036, note, fontsize=9.7, color=INK, weight="bold", va="top")


def _audit_note(item):
    covered = item.get("covered_by", [])
    conflict = item.get("conflicts_with", [])
    parents = item.get("enclosing_candidates", item.get("unconfirmed_parent_ids", item.get("parent_candidates", item.get("unconfirmed_parents", []))))
    if covered:
        prefix = "候选包含 " if "covered_by_provisional_outer_candidate" in item.get("reason_codes", []) else "已确认包含 "
        return prefix + ",".join(_short_id(value.get("id", "?") if isinstance(value, dict) else value) for value in covered)[:18]
    if parents:
        return "父轮廓 " + ",".join(_short_id(value.get("id", "?") if isinstance(value, dict) else value) for value in parents)[:22]
    if conflict:
        return "相交 " + ",".join(_short_id(value.get("id", "?") if isinstance(value, dict) else value) for value in conflict)[:22]
    category = item.get("category")
    if category == "retained":
        return "方向 / 形状 / 尺度支持"
    if category == "suppressed":
        return "非独立占用轮廓"
    reasons = item.get("reason_codes", [])
    for code, note in [("partial_overlap_reviewed_outline", "与确认轮廓部分相交"),
                       ("partial_overlap_candidate", "候选之间部分相交"),
                       ("axis_unresolved_or_misaligned", "与参照轴不一致"),
                       ("scale_outside_review_prior", "尺寸证据不足"),
                       ("non_building_layer_hint", "非建筑图层线索"),
                       ("shape_evidence_weak", "形状证据不足")]:
        if code in reasons:
            return note
    return "建筑身份待核实"


def _audit_pages(facts, refinement):
    all_items = _review_items(refinement)
    items = _focus_items(refinement)
    # Keep text readable even when a later drawing has more than 48 candidates.
    for page_index, first in enumerate(range(0, max(len(items), 1), 48)):
        subset = items[first:first + 48]
        fig = plt.figure(figsize=(18, 11))
        fig.text(.045, .957, "重点审查清单 · 全场候选与空间关系", fontsize=23, color=INK, weight="bold", va="top")
        fig.text(.045, .915, f"从 {len(all_items)} 个对象中选择 {len(items)} 项重点核对；完整结果保留于 JSON。置信等级未经概率校准。",
                 fontsize=10.5, color="#526b83", va="top")
        fig.text(.956, .953, f"第 {page_index + 2} 页  ·  {first + 1}–{first + len(subset)} / {len(items)}",
                 fontsize=12, color=BLUE, ha="right", va="top")
        for col in range(2):
            panel = fig.add_axes([.045 + col * .469, .167, .443, .693])
            panel.set_axis_off()
            columns = [(0, "源 ID"), (.31, "分类"), (.49, "面积"), (.62, "角差°"), (.75, "关系 / 原因")]
            for x, text in columns:
                _panel_text(panel, x, .998, text, size=9.5, weight="bold")
            panel.plot([0, 1], [.957, .957], transform=panel.transAxes, color="#b7c9dc", lw=.8)
            for row, item in enumerate(subset[col * 24:(col + 1) * 24]):
                y = .943 - row * .037
                category = item.get("category", "uncertain")
                color = PALETTE.get(category, PURPLE)
                if row % 2 == 0:
                    panel.add_patch(plt.Rectangle((-.006, y - .029), 1.018, .036,
                                                 transform=panel.transAxes, fc="#f4f7fb", ec="none", zorder=0))
                _panel_text(panel, 0, y, item["id"], size=8.8)
                _panel_text(panel, .31, y, CATEGORY_NAMES.get(category, "待定轮廓"), size=9, color=color)
                _panel_text(panel, .49, y, _value(item.get("area_native2"), 2), size=9)
                _panel_text(panel, .62, y, _value(item.get("angle_deviation_deg"), 3), size=9)
                _panel_text(panel, .75, y, _audit_note(item), size=8.1, color="#526b83")
        _footer(fig, facts, facts["scope"]["origin"],
                "清单为精选候选与歧义关系；已人工确认建筑另外保留。分类不替代施工阶段、建筑身份和规范条件核实。")
        yield fig


def render_refinement_report(facts: dict, refinement: dict, output_dir: str | Path,
                             prefix: str = "v0009_axis_review") -> dict[str, str]:
    """Render a source-aligned overview and traceable candidate audit pages."""
    output_dir = Path(output_dir)
    if not prefix or Path(prefix).name != prefix:
        raise ValueError("prefix must be a filename stem")
    ref_sha = refinement.get("source", {}).get("sha256")
    if ref_sha and ref_sha != facts["source"].get("sha256"):
        raise ValueError("refinement and facts must refer to the same source SHA-256")
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {extension: str(output_dir / f"{prefix}.{extension}") for extension in ("svg", "png", "pdf")}
    with matplotlib.rc_context(RC):
        overview = _overview(facts, refinement)
        overview.savefig(paths["svg"], metadata={"Creator": "cad_domain.spatial_refinement_report"})
        overview.savefig(paths["png"], dpi=160)
        with PdfPages(paths["pdf"], metadata={"Creator": "cad_domain.spatial_refinement_report",
                                           "Title": "v0009 direction and footprint audit"}) as pdf:
            pdf.savefig(overview)
            for page in _audit_pages(facts, refinement):
                pdf.savefig(page)
                plt.close(page)
        plt.close(overview)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("facts", type=Path)
    parser.add_argument("refinement", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prefix", default="v0009_axis_review")
    args = parser.parse_args()
    facts = json.loads(args.facts.read_text(encoding="utf-8"))
    refinement = json.loads(args.refinement.read_text(encoding="utf-8"))
    print(json.dumps(render_refinement_report(facts, refinement, args.output_dir, args.prefix), ensure_ascii=False))


if __name__ == "__main__":
    main()
