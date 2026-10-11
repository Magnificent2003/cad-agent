"""Independent, read-only figures for the phase-A spatial-fact audit.

The figures are explanatory overlays, never CAD deliverables or buildable-space
maps. Geometry stays in its original world orientation. A local XY translation
is used only for legible plot axes; native drawing units are not rescaled.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import PathPatch
from matplotlib.path import Path as PlotPath
from shapely.affinity import translate
from shapely.geometry import shape


INK = "#16324f"
BLUE = "#1766c3"
LIGHT_BLUE = "#dcecff"
ORANGE = "#ca7309"
MAGENTA = "#a63e81"
GRAY = "#a4b3c2"


def _polygon_path(polygon):
    """Keep holes instead of filling every ring as an independent polygon."""
    vertices, codes = [], []
    for ring in [polygon.exterior, *polygon.interiors]:
        coords = list(ring.coords)
        if not coords:
            continue
        vertices.extend(coords)
        codes.extend([PlotPath.MOVETO] + [PlotPath.LINETO] * (len(coords) - 2) + [PlotPath.CLOSEPOLY])
    return PlotPath(vertices, codes)


def _draw_geometry(ax, geometry, *, edgecolor, facecolor="none", linewidth=1,
                   linestyle="-", alpha=1, zorder=2):
    if geometry is None or geometry.is_empty:
        return
    if geometry.geom_type == "Polygon":
        # Polygon holes need opposite winding for matplotlib's nonzero fill rule.
        from shapely.geometry.polygon import orient
        patch = PathPatch(_polygon_path(orient(geometry, sign=1.0)),
                          edgecolor=edgecolor, facecolor=facecolor,
                          linewidth=linewidth, linestyle=linestyle,
                          alpha=alpha, zorder=zorder)
        ax.add_patch(patch)
    elif geometry.geom_type in {"LineString", "LinearRing"}:
        coords = list(geometry.coords)
        ax.plot([p[0] for p in coords], [p[1] for p in coords],
                color=edgecolor, linewidth=linewidth, linestyle=linestyle,
                alpha=alpha, zorder=zorder)
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            _draw_geometry(ax, part, edgecolor=edgecolor, facecolor=facecolor,
                           linewidth=linewidth, linestyle=linestyle,
                           alpha=alpha, zorder=zorder)


def _short(value: Any, maximum: int = 58) -> str:
    value = str(value)
    return value if len(value) <= maximum else value[:maximum - 1] + "…"


def _panel_text(ax, x, y, text, *, size=10, color=INK, weight="normal", **kwargs):
    return ax.text(x, y, text, transform=ax.transAxes, va="top", ha="left",
                   fontsize=size, color=color, weight=weight, **kwargs)


def _geojson(value):
    if not value:
        return None
    return shape(value)



def render_spatial_report(facts: dict, output_dir: str | Path,
                          prefix: str = "v0009_spatial") -> dict[str, str]:
    """Render traceable geometry candidates without engineering approval claims.

    Supported means source text supports the building association, not that its
    design phase or no-build status is confirmed. Uses this fact snapshot only;
    never silently opens a newer CAD revision than the accompanying report.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    if Path(prefix).name != prefix:
        raise ValueError("prefix must be a filename stem, not a path")
    scope, source = facts["scope"], facts["source"]
    origin_x, origin_y = [float(v) for v in scope["origin"]]
    bounds = [float(v) for v in scope["bounds_native"]]
    if len(bounds) != 4 or not all(math.isfinite(v) for v in bounds):
        raise ValueError("scope.bounds_native must contain four finite coordinates")
    features = []
    for feature in facts["features"]:
        geometry = _geojson(feature.get("geometry"))
        if geometry is not None and not geometry.is_empty:
            features.append((feature, translate(geometry, -origin_x, -origin_y)))
    buildings = [(f, g) for f, g in features if f["properties"]["kind"] == "building_candidate"]
    supported = [(f, g) for f, g in buildings if f["properties"].get("status") == "supported"]
    candidates = [(f, g) for f, g in buildings if f["properties"].get("status") != "supported"]
    boundaries = [(f, g) for f, g in features if f["properties"]["kind"] == "boundary_candidate"]
    wipes = [(f, g) for f, g in features if f["properties"]["kind"] == "wipeout"]
    primary = [(f, g) for f, g in buildings if f["properties"].get("primary", f["properties"].get("status") == "supported")]
    unresolved = facts.get("unresolved", [])
    rc = {"font.family": ["Noto Sans CJK SC", "DejaVu Sans"],
          "axes.unicode_minus": False, "svg.fonttype": "none",
          "pdf.fonttype": 42, "ps.fonttype": 42, "font.size": 10,
          "figure.facecolor": "white", "savefig.facecolor": "white"}
    with matplotlib.rc_context(rc):
        fig = plt.figure(figsize=(18, 11))
        fig.text(.045, .957, "部分 A · 空间事实核对图", fontsize=24,
                 color=INK, weight="bold", va="top")
        fig.text(.045, .915,
                 "原生几何 → 来源追溯 → 编号关联 → 待核实项  |  RESEARCH GEOMETRY AUDIT",
                 fontsize=11, color="#526b83", va="top")
        fig.text(.956, .952, "白桥 · v0009 开发案例", fontsize=13,
                 color=BLUE, ha="right", va="top")
        fig.text(.956, .922, "独立核对图 · 不是场布成果或验收结论", fontsize=10,
                 color="#526b83", ha="right", va="top")
        ax = fig.add_axes([.055, .165, .605, .69])
        ax.set_facecolor("#f8fafc")
        for feature, geometry in features:
            if feature["properties"]["kind"] == "context_outline":
                _draw_geometry(ax, geometry, edgecolor=GRAY,
                               linewidth=.48, alpha=.7, zorder=1)
        for feature, geometry in candidates:
            _draw_geometry(ax, geometry, edgecolor="#6387ab" if feature["properties"].get("primary") else GRAY,
                           facecolor="none", linewidth=.8, alpha=.85, zorder=2)
        for feature, geometry in supported:
            _draw_geometry(ax, geometry, edgecolor=BLUE, facecolor=LIGHT_BLUE,
                           linewidth=1.75, zorder=4)
        for feature, geometry in boundaries:
            _draw_geometry(ax, geometry, edgecolor=ORANGE, linewidth=1.9,
                           linestyle=(0, (6, 3)), zorder=5)
        for feature, geometry in wipes:
            _draw_geometry(ax, geometry, edgecolor=MAGENTA, linewidth=1.25,
                           linestyle=(0, (2, 2)), alpha=.9, zorder=6)
        span_x, span_y = bounds[2] - bounds[0], bounds[3] - bounds[1]
        pad = max(span_x, span_y) * .055
        ax.set_xlim(bounds[0] - origin_x - pad, bounds[2] - origin_x + pad)
        ax.set_ylim(bounds[1] - origin_y - pad, bounds[3] - origin_y + pad)
        ax.set_aspect("equal", adjustable="box")
        ax.grid(color="#d8e1eb", linewidth=.5, alpha=.8)
        ax.set_axisbelow(True)
        for spine in ax.spines.values():
            spine.set_color("#d3dfeb")
        ax.tick_params(labelsize=9, colors="#61758a", length=3)
        ax.set_xlabel("X − X₀  [原生坐标单位]", fontsize=10, color="#526b83", labelpad=9)
        ax.set_ylabel("Y − Y₀  [原生坐标单位]", fontsize=10, color="#526b83", labelpad=9)
        ax.set_title("原世界方向；E 号对应场界边段清单；空白区域未经可用性判定", loc="left",
                     fontsize=10.5, color=INK, pad=13)
        for feature, geometry in primary:
            p = feature["properties"]
            point = geometry.representative_point()
            handle = p.get("source", {}).get("root_handle", "?")
            label = str(p.get("label", feature["id"]))
            if p.get("status") != "supported":
                label += "（待核实）"
            ax.text(point.x, point.y, f"{label}\n源 {handle}",
                    ha="center", va="center", fontsize=10, color="#0d4e93",
                    weight="bold", zorder=9,
                    bbox={"boxstyle": "round,pad=.28", "fc": "white",
                          "ec": "#abc9ec", "lw": .65, "alpha": .96})
        features_by_id = {feature["id"]: (feature, geometry) for feature, geometry in features}
        for relation in facts.get("spatial_relations", []):
            outside_fraction = float(relation.get("outside_fraction", 0))
            if outside_fraction <= 0 or relation.get("building_id") not in features_by_id:
                continue
            feature, geometry = features_by_id[relation["building_id"]]
            boundary_item = features_by_id.get(relation.get("boundary_id"))
            outside = geometry.difference(boundary_item[1]) if boundary_item else geometry
            if outside.is_empty:
                continue
            _draw_geometry(ax, outside, edgecolor=ORANGE, facecolor="#fff0d9",
                           linewidth=1.1, alpha=.85, zorder=7)
            point = outside.representative_point()
            name = str(feature["properties"].get("label", feature["id"])).split()[0]
            ax.annotate(f"{name}：{outside_fraction:.2%} 位于候选界外 · 待核实\n仅为几何关系，不据此判定工程违规",
                        xy=(point.x, point.y), xytext=(75, -3),
                        textcoords="offset points", ha="left", va="center",
                        fontsize=9, color="#8d4d09", zorder=12,
                        arrowprops={"arrowstyle": "-", "color": ORANGE, "lw": .8},
                        bbox={"boxstyle": "round,pad=.38", "fc": "#fffaf1",
                              "ec": "#dbb577", "lw": .75, "alpha": .98})
        for feature, geometry in boundaries[:3]:
            if geometry.geom_type != "Polygon":
                continue
            coords = list(geometry.exterior.coords)
            first, second = max(zip(coords, coords[1:]),
                                key=lambda pair: math.dist(*pair))
            x, y = (first[0] + second[0]) / 2, (first[1] + second[1]) / 2
            handle = feature["properties"].get("source", {}).get("root_handle", "?")
            ax.annotate(f"场界候选 · {handle}", xy=(x, y), xytext=(-15, 25),
                        textcoords="offset points", fontsize=10, color=ORANGE,
                        bbox={"boxstyle": "round,pad=.28", "fc": "white",
                              "ec": "#edce9f", "lw": .7},
                        arrowprops={"arrowstyle": "-", "color": ORANGE, "lw": .8},
                        zorder=10)
        # Label only the four longest sampled edges to keep the overview legible.
        # All edge identities, endpoints and angles remain in facts.boundary_edges.
        sampled_edges = sorted(facts.get("boundary_edges", []),
                               key=lambda e: float(e.get("length_native", 0)), reverse=True)[:4]
        for edge in sampled_edges:
            a, b = edge["start"], edge["end"]
            x = (float(a[0]) + float(b[0])) / 2 - origin_x
            y = (float(a[1]) + float(b[1])) / 2 - origin_y
            edge_number = str(edge["id"]).rsplit(":", 1)[-1]
            dx, dy = float(b[0]) - float(a[0]), float(b[1]) - float(a[1])
            length = math.hypot(dx, dy)
            nx, ny = (-dy / length, dx / length) if length else (0, 1)
            center_x = (bounds[0] + bounds[2]) / 2 - origin_x
            center_y = (bounds[1] + bounds[3]) / 2 - origin_y
            if nx * (x - center_x) + ny * (y - center_y) < 0:
                nx, ny = -nx, -ny
            ax.annotate(f"E{edge_number}", xy=(x, y), xytext=(18 * nx, 18 * ny),
                        textcoords="offset points", color=ORANGE, fontsize=9,
                        ha="center", va="center", zorder=10,
                        arrowprops={"arrowstyle": "-", "color": ORANGE, "lw": .55},
                        bbox={"boxstyle": "round,pad=.2", "fc": "#fffaf1", "ec": "#edce9f", "lw": .6})
        if wipes:
            ids = ", ".join(str(f["properties"].get("source", {}).get("root_handle", "?"))
                            for f, _ in wipes[:3])
            ax.text(.02, .98, f"WIPEOUT 遮罩覆盖范围：{ids}\n仅核对 XY 覆盖；不等同最终显示顺序",
                    transform=ax.transAxes, va="top", fontsize=9, color=MAGENTA,
                    bbox={"boxstyle": "round,pad=.4", "fc": "white",
                          "ec": "#dbb5cd", "lw": .65, "alpha": .95}, zorder=11)

        panel = fig.add_axes([.706, .167, .251, .69])
        panel.set_axis_off()
        _panel_text(panel, 0, 1, "01  几何与语义分别核实", size=13, weight="bold")
        legend = [
            (BLUE, "建筑候选：编号 / 名称 / 面积表支持", "-"),
            ("#6387ab", "其他建筑候选：用途或轮廓待核实", "-"),
            (ORANGE, "场界候选：法律 / 工程属性未确认", "--"),
            (MAGENTA, "WIPEOUT：遮罩范围，不等于空地", ":"),
            (GRAY, "其他闭合轮廓：保留背景，不推定角色", "-"),
        ]
        y = .943
        for color, text, linestyle in legend:
            panel.plot([.005, .07], [y - .012, y - .012], transform=panel.transAxes,
                       color=color, linewidth=2.5, linestyle=linestyle,
                       solid_capstyle="butt", clip_on=False)
            _panel_text(panel, .095, y, text, size=9.2, color="#38516c")
            y -= .045
        _panel_text(panel, 0, .694, "02  建筑编号与轮廓关联", size=13, weight="bold")
        _panel_text(panel, 0, .65, "编号 / 名称", size=9, color="#63768a")
        _panel_text(panel, .39, .65, "源句柄", size=9, color="#63768a")
        _panel_text(panel, .7, .65, "面积 [原生²]", size=9, color="#63768a")
        y = .616
        for feature, geometry in primary[:7]:
            p = feature["properties"]
            _panel_text(panel, 0, y, _short(p.get("label", feature["id"]), 12) + (" *" if p.get("status") != "supported" else ""), size=9.5)
            _panel_text(panel, .39, y, p.get("source", {}).get("root_handle", "?"), size=9.5, color=BLUE)
            _panel_text(panel, .7, y, f"{float(p.get('area_native', geometry.area)):,.1f}", size=9.5)
            y -= .033
        if len(primary) > 7:
            _panel_text(panel, 0, y, f"另有 {len(primary) - 7} 个关联对象，见完整来源清单", size=9, color="#63768a")
            y -= .032
        _panel_text(panel, 0, y - .006,
                    f"其他轮廓候选 {len(buildings) - len(primary)} 个 · 待核实记录 {len(unresolved)} 条\n"
                    "* 轮廓/量测有歧义；编号关联不等于工程确认。",
                    size=9, color="#63768a", linespacing=1.65)
        axis_y = min(.356, y - .095)
        _panel_text(panel, 0, axis_y, "03  方向证据（仅候选）", size=13, weight="bold")
        axes = facts.get("axes", {})
        def angle_text(value):
            return "尚无可靠候选" if value is None else f"{float(value) % 90:.3f}° / {float(value) % 90 + 90:.3f}°"
        difference = "待核实" if axes.get("difference_deg") is None else f"{float(axes['difference_deg']):.3f}°"
        axis_text = (f"建筑正交轴对   {angle_text(axes.get('building_angle_deg'))}\n"
                     f"场界包络轴对   {angle_text(axes.get('boundary_angle_deg'))}\n"
                     f"正交轴对角差   {difference}")
        _panel_text(panel, 0, axis_y - .049, axis_text, size=10, linespacing=1.65)
        _panel_text(panel, 0, axis_y - .163, "长短边另行区分；并非统一布置角度或已核实北向。", size=9, color="#63768a")
        unit_y = axis_y - .214
        _panel_text(panel, 0, unit_y, "04  单位与完成边界", size=13, weight="bold")
        units = facts.get("units", {})
        declared = units.get("declared_metres_per_unit")
        candidate = units.get("candidate_metres_per_unit")
        unit_note = "单位冲突待核实；图中面积保留原生单位。" if units.get("status") == "conflict" else "图中面积保留原生单位，未经工程尺度确认。"
        _panel_text(panel, 0, unit_y - .047,
                    f"INSUNITS 声明：{format(float(declared), '.6g') if declared is not None else '未知'} m / 单位\n"
                    f"量测候选：{format(float(candidate), '.6g') if candidate is not None else '尚未建立'} m / 单位\n"
                    f"{unit_note}",
                    size=9, color="#526b83", linespacing=1.65)
        area_disagreements = [item for item in unresolved
                              if item.get("code") == "boundary_area_disagreement"]
        if area_disagreements:
            item = area_disagreements[0]
            table_area = float(item["table_area_m2"])
            candidate_area = float(item["candidate_area_m2"])
            _panel_text(panel, 0, unit_y - .142,
                        f"建设用地表：{table_area:,.0f} m²\n"
                        f"候选场界按候选尺度换算：{candidate_area:,.0f} m²\n"
                        "二者不一致；范围含义需核实，不能再次缩放。",
                        size=8.7, color="#8d4d09", linespacing=1.5)

        fig.add_artist(plt.Line2D([.045, .957], [.105, .105], transform=fig.transFigure,
                                 color="#dbe5ef", linewidth=1))
        fig.text(.045, .088,
                 f"源文件：{_short(source.get('name', 'DXF'), 54)}   |   "
                 f"模型空间实体：{source.get('modelspace_entity_count', '?')}   |   "
                 f"SHA-256：{source.get('sha256', '?')[:20]}…",
                 fontsize=9, color="#526b83", va="top")
        fig.text(.045, .062,
                 f"绘图局部原点 X₀={origin_x:,.3f}, Y₀={origin_y:,.3f}；仅为显示平移，原图坐标与方向均未修改。",
                 fontsize=9, color="#526b83", va="top")
        fig.text(.045, .035,
                 "本阶段只构建可追溯空间候选；尚未推导可用场地、布置设施或进行施工规范验收。未显示 / 未识别对象仍需核实。",
                 fontsize=10, color=INK, weight="bold", va="top")
        paths = {}
        for extension in ("svg", "png", "pdf"):
            path = output_dir / f"{prefix}.{extension}"
            fig.savefig(path, dpi=160, metadata={"Creator": "cad_domain.spatial_report"}
                        if extension in {"svg", "pdf"} else None)
            paths[extension] = str(path)
        plt.close(fig)
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("facts", type=Path, help="Spatial facts JSON from the same source revision")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prefix", default="v0009_spatial")
    args = parser.parse_args()
    facts = json.loads(args.facts.read_text(encoding="utf-8"))
    print(json.dumps(render_spatial_report(facts, args.output_dir, args.prefix), ensure_ascii=False))


if __name__ == "__main__":
    main()

