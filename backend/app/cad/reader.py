from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
from pathlib import Path

import ezdxf
from ezdxf import bbox as dxf_bbox
from ezdxf.lldxf.tagwriter import TagCollector
from ezdxf.math import Vec2, Vec3

from .errors import CadError
from .text_compat import configure_fonts, decode_text, plain_text
from ..preview_cache import DEFAULT_FONT


UNIT_NAMES = {0: "unspecified", 1: "in", 2: "ft", 3: "mi", 4: "mm", 5: "cm", 6: "m", 7: "km", 8: "microin", 9: "mil", 10: "yd", 11: "angstrom", 12: "nm", 13: "um", 14: "dm", 15: "dam", 16: "hm", 17: "Gm", 18: "au", 19: "ly", 20: "pc", 21: "us-ft", 22: "us-in", 23: "us-yd", 24: "us-mi"}


def units(doc) -> dict:
    code = int(doc.header.get("$INSUNITS", 0))
    return {"code": code, "name": UNIT_NAMES.get(code, "unsupported"), "confirmed": code in UNIT_NAMES and code != 0}


def load(source: str | Path):
    try:
        return ezdxf.readfile(source)
    except (OSError, ezdxf.DXFError, UnicodeError) as exc:
        raise CadError("Unable to read this DXF; no automatic repair was performed.", "invalid_dxf") from exc


def finite(value, name="coordinate") -> float:
    if isinstance(value, bool):
        raise CadError(f"{name} must be a finite number.", "invalid_operation")
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise CadError(f"{name} must be a finite number.", "invalid_operation") from None
    if not math.isfinite(result):
        raise CadError(f"{name} must be a finite number.", "invalid_operation")
    return result


def point(value, name="point") -> tuple[float, float, float]:
    if not isinstance(value, (tuple, list)) or len(value) not in {2, 3}:
        raise CadError(f"{name} requires two or three coordinates.", "invalid_operation")
    return (finite(value[0], name), finite(value[1], name), finite(value[2], name) if len(value) == 3 else 0.0)


def validate_bbox(value) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise CadError("bbox requires [xmin, ymin, xmax, ymax].", "invalid_bbox")
    result = [finite(v, "bbox") for v in value]
    if result[2] <= result[0] or result[3] <= result[1]:
        raise CadError("bbox must have positive width and height.", "invalid_bbox")
    return result


def native_bbox(entities, cache=None):
    try:
        bounds = dxf_bbox.extents(entities, fast=True, cache=cache)
        if bounds.has_data:
            values = [bounds.extmin.x, bounds.extmin.y, bounds.extmax.x, bounds.extmax.y]
            return values if all(math.isfinite(v) for v in values) else None
    except (ezdxf.DXFError, ValueError, TypeError, ZeroDivisionError, RecursionError):
        pass
    return None


def json_value(value):
    if isinstance(value, (Vec2, Vec3)):
        return list(value)
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def entity_record(entity, layout_name: str, cache=None) -> dict:
    kind = entity.dxftype()
    attrs = json_value(entity.dxf.all_existing_dxf_attribs())
    result = {"handle": entity.dxf.handle, "entity_id": entity.dxf.handle, "type": kind, "layer": entity.dxf.get("layer", "0"), "layout": layout_name, "bbox": native_bbox([entity], cache), "geometry": {}, "dxf_attributes": attrs, "editable": kind in {"TEXT", "MTEXT", "LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "INSERT", "POINT", "ELLIPSE", "SPLINE", "HATCH", "SOLID", "TRACE", "3DFACE"}}
    geometry = result["geometry"]
    for name in ("start", "end", "center", "radius", "insert", "height", "char_height", "rotation", "extrusion", "elevation", "start_angle", "end_angle", "major_axis", "ratio", "xscale", "yscale", "zscale"):
        if name in attrs:
            geometry[name] = attrs[name]
    if kind in {"TEXT", "ATTRIB", "ATTDEF"}:
        result["text"] = plain_text(entity)
        result["raw_text"] = entity.dxf.text
    elif kind == "MTEXT":
        result["text"] = plain_text(entity)
        result["raw_text"] = entity.text
    elif kind == "LWPOLYLINE":
        geometry.update({"points": [list(p) for p in entity.get_points("xyseb")], "closed": entity.closed, "coordinate_system": "OCS"})
    elif kind == "POLYLINE":
        geometry.update({"points": [list(v.dxf.location) for v in entity.vertices], "closed": entity.is_closed})
    elif kind == "INSERT":
        result["block_name"] = entity.dxf.name
        result["attributes"] = [{"handle": attrib.dxf.handle, "tag": attrib.dxf.tag, "text": plain_text(attrib), "raw_text": attrib.dxf.text, "insert": list(attrib.dxf.insert), "parent_handle": entity.dxf.handle, "layout": layout_name} for attrib in entity.attribs]
        result["transform"] = list(entity.matrix44())
        result["block_path"] = [entity.dxf.name]
        result["block_entities_expanded"] = False
    return result


def layout_entities(doc):
    for layout in doc.layouts:
        for entity in layout:
            yield layout.name, entity


def snapshots(doc) -> dict[str, str]:
    """Fingerprint serialized graphical data, including blocks and attributes."""
    from ezdxf.entities import DXFGraphic
    result = {}
    for handle, entity in doc.entitydb.items():
        if not entity.is_alive or not isinstance(entity, DXFGraphic):
            continue
        # ODA writes SEQEND markers even for attribute-free INSERTs; ezdxf
        # recreates these delimiters with new handles on load/save. They carry
        # no geometry. Keep markers with application data in strict checking.
        if entity.dxftype() == "SEQEND" and not (entity.xdata or entity.appdata or entity.has_extension_dict):
            continue
        # Match the file writer's omission of optional default-valued tags.
        # A translation may explicitly set elevation=0/extrusion=(0,0,1).
        collector = TagCollector(dxfversion=doc.dxfversion, optional=False)
        entity.export_dxf(collector)
        tags = collector.tags
        payload = [[tag.code, json_value(tag.value)] for tag in tags]
        result[handle] = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return result


def inspect_audit(source) -> dict:
    # ezdxf.audit() can mutate: inspect a separate read, never the published copy.
    auditor = load(source).audit()
    return {"errors": [{"code": int(error.code), "message": error.message} for error in auditor.errors], "suggested_fixes": [{"code": int(error.code), "message": error.message} for error in auditor.fixes], "repair_applied": False}


def dependencies(doc, settings: dict) -> dict:
    from ezdxf.fonts import fonts
    configure_fonts(settings)
    font_reports = []
    for style in doc.styles:
        for font_kind in ("font", "bigfont"):
            requested = style.dxf.get(font_kind, "")
            if not requested:
                continue
            try:
                resolved = fonts.find_font_face(requested)
                resolved_name = resolved.filename
            except Exception:
                resolved_name = ""
            explicit = any((Path(directory) / Path(requested).name).is_file() for directory in settings.get("font_dirs", []))
            substituted = not explicit and Path(requested).name.casefold() != Path(resolved_name).name.casefold()
            preview_font = fonts.find_font_face(settings.get("fallback_font", DEFAULT_FONT)).filename if substituted else resolved_name
            font_reports.append({"style": decode_text(style.dxf.name), "kind": font_kind, "requested": requested, "resolved": Path(resolved_name).name or None, "preview_font": Path(preview_font).name or None, "substituted": substituted})
    xrefs = [{"block": block.name, "path": block.block.dxf.get("xref_path", ""), "status": "not_loaded"} for block in doc.blocks if block.block.is_xref]
    images = []
    proxies = []
    for entity in doc.entitydb.values():
        if not entity.is_alive:
            continue
        if entity.dxftype() == "IMAGEDEF":
            images.append({"filename": entity.dxf.get("filename", ""), "status": "not_loaded"})
        if entity.dxftype() in {"ACAD_PROXY_ENTITY", "ACAD_PROXY_OBJECT", "3DSOLID", "BODY", "REGION", "OLE2FRAME"} or getattr(entity, "proxy_graphic", None):
            proxies.append({"handle": entity.dxf.get("handle"), "type": entity.dxftype(), "has_proxy_graphic": bool(getattr(entity, "proxy_graphic", None))})
    tianzheng_classes = sorted({item.dxf.name for item in doc.classes if item.dxf.name.upper().startswith("TCH_")})
    return {"fonts": font_reports, "xrefs": xrefs, "images": images, "proxies": proxies, "external_resources_loaded": False,
            "tianzheng": {"detected": bool(tianzheng_classes), "classes": tianzheng_classes, "version": None, "editable": False}}


def metadata(source, settings: dict | None = None) -> dict:
    settings = settings or {}
    doc = load(source)
    cache = dxf_bbox.Cache()
    layouts = [{"name": layout.name, "entity_count": len(layout), "bbox": native_bbox(layout, cache)} for layout in doc.layouts]
    report = dependencies(doc, settings)
    audit = inspect_audit(source)
    warnings = []
    if not units(doc)["confirmed"]:
        warnings.append("图纸未指定单位；执行尺寸修改或测量前需要明确确认单位。")
    if any(font["substituted"] for font in report["fonts"]):
        warnings.append("部分字体缺失，预览已使用回退字体；字宽和换行可能与原始出图不同。")
    if report["xrefs"] or report["images"]:
        warnings.append("图纸包含外部参照或图片，依赖已列入报告，预览暂未加载这些外部文件。")
    if report["proxies"]:
        warnings.append("代理对象、ACIS 或 OLE 对象在预览中可能被省略或简化。")
    if report["tianzheng"]["detected"]:
        warnings.append("检测到天正 TCH 对象；仅按可用代理图形显示，无法据此确定天正版本。缺少代理图形时需在天正中另存兼容普通 CAD 的副本。")
    if audit["errors"] or audit["suggested_fixes"]:
        warnings.append("DXF 审计发现问题；系统未对原图执行自动修复。")
    return {"units": units(doc), "dxf_version": doc.dxfversion, "layouts": layouts, "layers": [{"name": layer.dxf.name, "color": layer.dxf.color, "linetype": layer.dxf.linetype, "off": layer.is_off(), "frozen": layer.is_frozen(), "locked": layer.is_locked()} for layer in doc.layers], "entity_counts": dict(Counter(entity.dxftype() for _, entity in layout_entities(doc))), "bbox": native_bbox(doc.modelspace(), cache), "dependencies": report, "audit": audit, "warnings": warnings}


def query_drawing(source: str, query: dict) -> dict:
    doc = load(source)
    name = query.get("layout", "Model")
    if name != "*" and name not in doc.layouts:
        raise CadError("Unknown layout.", "unknown_layout")
    try:
        offset, limit = int(query.get("offset", 0)), int(query.get("limit", 50))
    except (ValueError, TypeError):
        raise CadError("Pagination values must be integers.", "invalid_query") from None
    if offset < 0 or not 1 <= limit <= 200:
        raise CadError("Query offset must be nonnegative and limit must be 1–200.", "invalid_query")
    kinds = {str(v).upper() for v in query.get("types", [])}
    layers = set(query.get("layers", []))
    handles = {str(v).upper() for v in query.get("handles", query.get("entity_ids", []))}
    region = validate_bbox(query["bbox"]) if query.get("bbox") is not None else None
    text_filter = str(query.get("text", "")).casefold()
    records, total = [], 0
    cache = dxf_bbox.Cache()
    for layout_name, entity in layout_entities(doc):
        if name != "*" and name != layout_name:
            continue
        if kinds and entity.dxftype() not in kinds or layers and entity.dxf.get("layer", "0") not in layers or handles and entity.dxf.handle not in handles:
            continue
        record = entity_record(entity, layout_name, cache)
        searchable = record.get("text", "") + " " + " ".join(item["text"] for item in record.get("attributes", []))
        if text_filter and text_filter not in searchable.casefold():
            continue
        bounds = record["bbox"]
        if region and (bounds is None or bounds[2] < region[0] or bounds[0] > region[2] or bounds[3] < region[1] or bounds[1] > region[3]):
            continue
        if offset <= total < offset + limit:
            records.append(record)
        total += 1
    return {"entities": records, "total": total, "offset": offset, "limit": limit, "units": units(doc), "coordinate_system": "WCS unless entity geometry explicitly marks OCS", "bbox_accuracy": "conservative preview bounds; use native geometry for measurements"}
