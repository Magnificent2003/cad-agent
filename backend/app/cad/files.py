"""File import, preview and format conversion; editing belongs to the agent."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
from .converter import convert
from .errors import CadError
from .reader import finite, load, metadata, units
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
