from __future__ import annotations

import json
import math
from pathlib import Path
import shutil

from ezdxf.lldxf.tagwriter import TagCollector
from ezdxf.math import Vec2, Vec3

from .converter import convert
from .errors import CadError
from .reader import UNIT_NAMES, finite, load, metadata, point, snapshots, units
from .renderer import render_drawing
from .text_compat import configure_fonts


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def finish(output: Path, settings: dict, extra: dict | None = None) -> dict:
    result = metadata(output / "drawing.dxf", settings)
    preview = render_drawing(str(output / "drawing.dxf"), str(output), settings=settings)
    result.update(extra or {})
    result["preview"] = preview
    result["warnings"] = list(dict.fromkeys([*result["warnings"], *preview["warnings"]]))
    result["artifacts"] = {"dxf": "drawing.dxf", **preview["artifacts"], "metadata": "metadata.json"}
    write_json(output / "metadata.json", result)
    return result


def import_drawing(source: str, output_dir: str, settings: dict) -> dict:
    configure_fonts(settings)
    original, output = Path(source), Path(output_dir)
    if original.suffix.lower() not in {".dxf", ".dwg"}:
        raise CadError("Only .dwg and .dxf drawings are supported.", "unsupported_format")
    output.mkdir(parents=True, exist_ok=True)
    target = output / "drawing.dxf"
    if original.resolve() == target.resolve() or target.exists():
        raise CadError("Import output must be a new revision directory.", "output_exists")
    conversion = {"converter": "none", "audit_repair": False}
    if original.suffix.lower() == ".dwg":
        conversion = convert(original, target, settings)
    else:
        shutil.copy2(original, target)
    load(target)
    return finish(output, settings, {"conversion": conversion})


def require_unit(doc, operation: dict):
    current = units(doc)
    if not current["confirmed"]:
        raise CadError("Drawing units are unspecified; confirm units before dimensional edits.", "unknown_units")
    if operation.get("unit") != current["name"]:
        raise CadError(f"Operation unit must match drawing units ({current['name']}).", "unit_mismatch")


def select_entities(doc, operation: dict):
    handles = operation.get("handles", operation.get("entity_ids", []))
    if operation.get("handle"):
        handles = [operation["handle"]]
    if not isinstance(handles, list) or not handles or len(handles) > 1000:
        raise CadError("Explicit handles are required (1–1000 entities).", "invalid_targets")
    layout_handles = {entity.dxf.handle: layout.name for layout in doc.layouts for entity in layout}
    result, seen = [], set()
    for handle in handles:
        handle = str(handle).upper()
        if handle in seen:
            continue
        seen.add(handle)
        if handle not in layout_handles:
            raise CadError(f"Handle {handle} is not a directly editable layout entity.", "invalid_targets")
        entity = doc.entitydb[handle]
        if operation.get("layout") and operation["layout"] != layout_handles[handle]:
            raise CadError("Target handle does not belong to the requested layout.", "invalid_targets")
        if doc.layers.get(entity.dxf.layer).is_locked():
            raise CadError("Cannot edit an entity on a locked layer.", "locked_layer")
        result.append(entity)
    return result


def text_value(operation: dict) -> str:
    text = operation.get("text")
    if not isinstance(text, str) or len(text) > 10000 or "\x00" in text:
        raise CadError("Text must be a string of at most 10000 characters without NUL.", "invalid_operation")
    return text


def apply_operation(doc, operation: dict) -> dict:
    kind = operation.get("type")
    before = snapshots(doc)
    authorized_handles = set()
    if kind == "set_text":
        value = text_value(operation)
        for entity in select_entities(doc, operation):
            authorized_handles.add(entity.dxf.handle)
            if entity.dxftype() not in {"TEXT", "MTEXT"}:
                raise CadError("set_text only accepts TEXT or MTEXT entities.", "invalid_targets")
            if entity.dxftype() == "MTEXT":
                if operation.get("preserve_mtext_format", False) and value != entity.text:
                    raise CadError("Preserving arbitrary MTEXT formatting requires review; provide replacement text with preserve_mtext_format=false.", "unsupported_format_preservation")
                entity.text = value
            else:
                if "\n" in value or "\r" in value:
                    raise CadError("TEXT must remain single-line; use MTEXT for multiple lines.", "invalid_operation")
                entity.dxf.text = value
    elif kind in {"translate_entities", "move"}:
        require_unit(doc, operation)
        delta = [finite(operation.get(key, 0), key) for key in ("dx", "dy", "dz")]
        for entity in select_entities(doc, operation):
            authorized_handles.add(entity.dxf.handle)
            for attribute in ("attribs", "vertices"):
                children = getattr(entity, attribute, [])
                if isinstance(children, (list, tuple)):
                    authorized_handles.update(child.dxf.handle for child in children)
            if entity.dxftype() not in {"TEXT", "MTEXT", "LINE", "LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "INSERT", "POINT", "ELLIPSE", "SPLINE", "HATCH", "SOLID", "TRACE", "3DFACE"}:
                raise CadError("This entity type is not supported for translation.", "unsupported_operation")
            entity.translate(*delta)
    elif kind in {"add_polyline", "add_rectangle", "add_text"}:
        require_unit(doc, operation)
        layout_name, layer_name = operation.get("layout", "Model"), operation.get("layer", "0")
        if layout_name not in doc.layouts:
            raise CadError("Unknown layout.", "unknown_layout")
        if layer_name not in doc.layers:
            raise CadError("New entities require an existing layer.", "unknown_layer")
        if doc.layers.get(layer_name).is_locked():
            raise CadError("Cannot add entities to a locked layer.", "locked_layer")
        layout = doc.layouts.get(layout_name)
        attributes = {"layer": layer_name}
        if kind == "add_text":
            value, position = text_value(operation), point(operation.get("position", operation.get("insert")))
            height = finite(operation.get("height"), "height")
            if height <= 0 or "\n" in value or "\r" in value:
                raise CadError("Text height must be positive and TEXT must be single-line.", "invalid_operation")
            layout.add_text(value, dxfattribs={**attributes, "height": height, "insert": position})
        else:
            if kind == "add_rectangle":
                origin = point(operation.get("origin"))
                width, height = finite(operation.get("width"), "width"), finite(operation.get("height"), "height")
                if width <= 0 or height <= 0 or origin[2] != 0:
                    raise CadError("Rectangles require positive width/height and XY coordinates.", "invalid_operation")
                points = [(origin[0], origin[1]), (origin[0] + width, origin[1]), (origin[0] + width, origin[1] + height), (origin[0], origin[1] + height)]
                closed = True
            else:
                values = operation.get("points")
                if not isinstance(values, list) or not 2 <= len(values) <= 10000:
                    raise CadError("Polyline requires 2–10000 points.", "invalid_operation")
                vertices = [point(value) for value in values]
                if any(vertex[2] != 0 for vertex in vertices):
                    raise CadError("First-phase polylines require XY coordinates.", "invalid_operation")
                points, closed = [vertex[:2] for vertex in vertices], bool(operation.get("closed", False))
                if closed and len(points) < 3:
                    raise CadError("Closed polyline requires at least three points.", "invalid_operation")
            layout.add_lwpolyline(points, close=closed, dxfattribs=attributes)
    else:
        raise CadError("Unsupported CAD operation.", "unsupported_operation")
    after = snapshots(doc)
    changed_handles = {handle for handle in before if before[handle] != after.get(handle)}
    if not changed_handles <= authorized_handles:
        raise CadError("An operation unexpectedly modified non-target entities.", "preservation_failed")
    for handle in changed_handles | (set(after) - set(before)):
        for tag in TagCollector.dxftags(doc.entitydb[handle]):
            values = list(tag.value) if isinstance(tag.value, (Vec2, Vec3)) else [tag.value]
            if any(isinstance(value, float) and not math.isfinite(value) for value in values):
                raise CadError("Operation produced a non-finite coordinate.", "invalid_operation")
    return {"type": kind, "modified_handles": sorted(handle for handle in before if before[handle] != after.get(handle)), "added_handles": sorted(set(after) - set(before)), "mtext_format": "replacement" if kind == "set_text" else None}


def edit_drawing(source: str, output_dir: str, operation: dict, settings: dict) -> dict:
    configure_fonts(settings)
    doc = load(source)
    original_snapshot = snapshots(doc)
    original_markers = {handle for handle, entity in doc.entitydb.items() if entity.is_alive and entity.dxftype() == "SEQEND"}
    values = operation.get("operations", [operation])
    if not isinstance(values, list) or not 1 <= len(values) <= 100 or any(not isinstance(value, dict) for value in values):
        raise CadError("An edit needs 1–100 operation objects.", "invalid_operation")
    confirmed_unit = operation.get("confirmed_unit")
    if confirmed_unit:
        codes = {name: code for code, name in UNIT_NAMES.items() if code != 0}
        if confirmed_unit not in codes:
            raise CadError("Unsupported unit confirmation.", "invalid_unit")
        if units(doc)["confirmed"] and confirmed_unit != units(doc)["name"]:
            raise CadError("Unit confirmation cannot reinterpret known drawing units.", "unit_mismatch")
        doc.units = codes[confirmed_unit]
    changes = [apply_operation(doc, value) for value in values]
    expected_snapshot = snapshots(doc)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    target = output / "drawing.dxf"
    if target.exists() or Path(source).resolve() == target.resolve():
        raise CadError("Edit output must be a new revision directory.", "output_exists")
    doc.saveas(target)
    reread = load(target)
    actual_snapshot = snapshots(reread)
    saved_markers = {handle for handle, entity in reread.entitydb.items() if entity.is_alive and entity.dxftype() == "SEQEND"}
    if actual_snapshot != expected_snapshot:
        differing = sorted(key for key in set(actual_snapshot) | set(expected_snapshot) if actual_snapshot.get(key) != expected_snapshot.get(key))
        raise CadError(f"DXF round-trip preservation check failed for {len(differing)} entities.", "preservation_failed")
    modified = sorted(handle for handle in original_snapshot if original_snapshot[handle] != actual_snapshot.get(handle))
    added = sorted(set(actual_snapshot) - set(original_snapshot))
    approved = {handle for change in changes for handle in change["modified_handles"]}
    if any(handle not in approved for handle in modified):
        raise CadError("Unexpected entity modifications detected.", "preservation_failed")
    change_report = {"operations": changes, "modified_handles": modified, "added_handles": added, "preservation_verified": True, "non_target_entity_count": len(original_snapshot) - len(modified), "unit_confirmation": confirmed_unit, "sequence_markers": {"removed_handles": len(original_markers - saved_markers), "created_handles": len(saved_markers - original_markers), "note": "Empty SEQEND delimiters may receive new handles on DXF serialization; markers with application data remain strictly checked."}}
    write_json(output / "changes.json", change_report)
    return finish(output, settings, {"changes": change_report})


def export_drawing(source: str, output_dir: str, format: str, settings: dict) -> dict:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    if format not in {"dxf", "dwg"}:
        raise CadError("Export supports dxf and dwg.", "unsupported_format")
    target = output / ("drawing." + format)
    if target.exists() or target.resolve() == Path(source).resolve():
        raise CadError("Export output must not overwrite a drawing.", "output_exists")
    if format == "dxf":
        shutil.copy2(source, target)
        return {"format": format, "filename": target.name, "verified": True, "verification": "byte-for-byte DXF copy"}
    convert(Path(source), target, settings)
    verification_dir = output / "verification"
    verification_dir.mkdir(exist_ok=True)
    converted = verification_dir / "drawing.dxf"
    convert(target, converted, settings)
    from .reader import entity_record, layout_entities
    tolerance = finite(settings.get("export_tolerance", 1e-7), "export verification tolerance")
    if tolerance <= 0:
        raise CadError("Export verification tolerance must be positive.", "invalid_settings")
    def summary(path):
        document = load(path)
        records = []
        for name, entity in layout_entities(document):
            record = entity_record(entity, name)
            data = {k: v for k, v in record.items() if k in {"type", "layer", "layout", "geometry", "text", "raw_text", "block_name", "transform"}}
            if "attributes" in record:
                data["attributes"] = [{k: v for k, v in attrib.items() if k in {"tag", "text", "insert"}} for attrib in record["attributes"]]
            records.append(data)
        return {"units": units(document), "records": records}
    def equivalent(first, second):
        if isinstance(first, dict) and isinstance(second, dict):
            return first.keys() == second.keys() and all(equivalent(first[key], second[key]) for key in first)
        if isinstance(first, list) and isinstance(second, list):
            return len(first) == len(second) and all(equivalent(a, b) for a, b in zip(first, second))
        if isinstance(first, (int, float)) and isinstance(second, (int, float)):
            return abs(first - second) <= tolerance
        return first == second
    if not equivalent(summary(source), summary(converted)):
        raise CadError("DWG export reimport differs in native geometry or text; export not verified.", "export_verification_failed")
    return {"format": format, "filename": target.name, "verified": True, "verification": "reimported ordered native geometry/text, attributes, units and layout/layer comparison", "numerical_tolerance": tolerance, "tolerance_unit": "native drawing units (floating-point serialization only)", "limitations": "Checks key layout entity data; does not certify all proprietary object internals."}
