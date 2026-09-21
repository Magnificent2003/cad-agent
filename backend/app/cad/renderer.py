"""Derived previews only: geometry remains in native drawing coordinates."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .errors import CadError
from .reader import load, native_bbox, units, validate_bbox
from .text_compat import FontResolver, compatible_frontend, plain_text
from ..preview_cache import preview_signature


def render_drawing(source: str, output_dir: str, layout_name="Model", bbox=None, settings: dict | None = None) -> dict:
    settings = settings or {}
    resolver = FontResolver(settings)
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from ezdxf.addons.drawing import RenderContext
    from ezdxf.addons.drawing.config import Configuration, BackgroundPolicy, ImagePolicy, HatchPolicy
    from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

    doc = load(source)
    # This document is disposable: no normalization is written back to DXF.
    style_text = {}
    for entity in doc.entitydb.values():
        if not entity.is_alive:
            continue
        resolver.normalize(entity)
        if entity.dxftype() in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
            style_text.setdefault(entity.dxf.get("style", "Standard").casefold(), set()).update(plain_text(entity))
    for style in doc.styles:
        requested = resolver.face_for_name(style.dxf.get("font", ""))
        face = resolver.choose("".join(sorted(style_text.get(style.dxf.name.casefold(), set()))), requested)
        style.dxf.font = face.filename
        # Derived preview uses Unicode outline fonts in place of SHX Bigfont.
        style.dxf.bigfont = ""
    if layout_name not in doc.layouts:
        raise CadError("Unknown layout.", "unknown_layout")
    region = validate_bbox(bbox) if bbox is not None else None
    current_layout = doc.layouts.get(layout_name)
    bounds = region or native_bbox(current_layout)
    framing_warning = None
    # Survey drawings sometimes contain an isolated origin marker tens of
    # millions of units from the actual plan. Prefer their saved CAD extents
    # only when those extents contain >=90% of the bounded entity centers.
    if not region and layout_name == "Model" and bounds:
        minimum, maximum = doc.header.get("$EXTMIN"), doc.header.get("$EXTMAX")
        if minimum is not None and maximum is not None:
            saved = [minimum[0], minimum[1], maximum[0], maximum[1]]
            span = max(bounds[2] - bounds[0], bounds[3] - bounds[1])
            saved_span = max(saved[2] - saved[0], saved[3] - saved[1])
            if all(math.isfinite(v) for v in saved) and saved[2] > saved[0] and saved[3] > saved[1] and saved_span > 0 and span > 20 * saved_span:
                entity_bounds = [value for entity in current_layout if (value := native_bbox([entity]))]
                inside = sum(saved[0] <= (value[0] + value[2]) / 2 <= saved[2] and saved[1] <= (value[1] + value[3]) / 2 <= saved[3] for value in entity_bounds)
                if entity_bounds and inside / len(entity_bounds) >= 0.9:
                    bounds = saved
                    framing_warning = "概览使用 CAD 保存的范围展示主要图形；远处实体仍保留在完整图纸中，可通过原生查询或指定范围预览查看。"
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    suffix = "" if layout_name == "Model" and region is None else "-" + hashlib.sha256(json.dumps([layout_name, region], ensure_ascii=False).encode()).hexdigest()[:16]
    svg_name, png_name = f"preview{suffix}.svg", f"preview{suffix}.png"
    dpi = min(300, max(72, int(settings.get("preview_dpi", 140))))
    figure = Figure(figsize=(12, 8), dpi=dpi, facecolor="#101827")
    FigureCanvasAgg(figure)
    axes = figure.add_axes([0.015, 0.015, 0.97, 0.97])
    axes.set_axis_off()
    # Dense native hatch line patterns can expand megabyte drawings into
    # hundreds of megabytes of SVG; keep the preview bounded and report it.
    config = Configuration(background_policy=BackgroundPolicy.CUSTOM, custom_bg_color="#101827", image_policy=ImagePolicy.IGNORE, hatch_policy=HatchPolicy.SHOW_OUTLINE)
    try:
        backend = MatplotlibBackend(axes, adjust_figure=False)
        context = RenderContext(doc)
        compatible_frontend(context, backend, config, resolver).draw_layout(current_layout, finalize=True)
        if bounds:
            width, height = max(bounds[2] - bounds[0], 1e-6), max(bounds[3] - bounds[1], 1e-6)
            padding = 0 if region else max(width, height) * 0.025
            axes.set_xlim(bounds[0] - padding, bounds[2] + padding)
            axes.set_ylim(bounds[1] - padding, bounds[3] + padding)
        else:
            axes.set_xlim(0, 100)
            axes.set_ylim(0, 100)
        axes.set_aspect("equal", adjustable="box")
        figure.savefig(output / svg_name, format="svg", facecolor=figure.get_facecolor())
        figure.savefig(output / png_name, format="png", dpi=dpi, facecolor=figure.get_facecolor())
    except Exception as exc:
        raise CadError("Preview rendering failed; no revision was published.", "render_failed") from exc
    finally:
        figure.clear()
    warnings = ["预览为二维近似；字体、代理对象和多行文字效果可能与 CAD 原始出图不同。", "轻量预览仅显示填充边界；DXF 中保留全部原始填充数据。"]
    if framing_warning:
        warnings.append(framing_warning)
    font_report = resolver.report()
    if font_report["missing_codepoints"]:
        warnings.append("现有字体仍缺少部分字符：" + ", ".join(font_report["missing_codepoints"][:20]))
    svg_bytes, png_bytes = (output / svg_name).stat().st_size, (output / png_name).stat().st_size
    return {"artifacts": {"svg": svg_name, "png": png_name}, "layout": layout_name, "bbox": bounds, "units": units(doc), "coordinate_system": "WCS", "hatch_preview": "outline", "svg_bytes": svg_bytes, "png_bytes": png_bytes, "preferred_format": "svg", "cache_signature": preview_signature(settings), "fonts": font_report, "warnings": warnings}
