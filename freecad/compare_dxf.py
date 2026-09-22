#!/usr/bin/env python3
"""Read-only DXF inventory; differences are diagnostics, never proof of fidelity.

Usage: .venv/bin/python freecad/compare_dxf.py before.dxf [after.dxf]
Drawing text and layer/block names are deliberately omitted from reports.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

import ezdxf
from ezdxf import bbox
from ezdxf.entities import DXFGraphic


def digest_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def digest_values(values):
    payload = json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def counts(entities):
    return dict(sorted(Counter(e.dxftype() for e in entities).items()))


def extent(entities):
    try:
        box = bbox.extents(entities, fast=True)
        return [*box.extmin, *box.extmax] if box.has_data else None
    except Exception as exc:
        return {"unavailable": type(exc).__name__}


def report(path):
    original_hash = digest_file(path)
    doc = ezdxf.readfile(path)
    model = list(doc.modelspace())
    database = [e for e in doc.entitydb.values() if e.is_alive]
    graphical = [e for e in database if isinstance(e, DXFGraphic)]
    text_entities = [e for e in graphical if e.dxftype() in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}]
    raw_text = [e.text if e.dxftype() == "MTEXT" else e.dxf.get("text", "") for e in text_entities]
    proxy_entities = [e for e in graphical if e.dxftype() == "ACAD_PROXY_ENTITY"]
    paperspaces = [layout for layout in doc.layouts if layout.name != "Model"]
    proxy_bytes = [getattr(e, "proxy_graphic", None) for e in graphical]
    result = {
        "path": str(path),
        "file_bytes": path.stat().st_size,
        "sha256": original_hash,
        "dxf_version": doc.dxfversion,
        "units_code": doc.header.get("$INSUNITS", 0),
        "modelspace": {
            "entities": len(model), "types": counts(model),
            "bbox_approx_xyz_min_max": extent(model),
            "aci_explicit_counts": dict(sorted(Counter(str(e.dxf.get("color", 256)) for e in model).items())),
            "truecolor_explicit_count": sum(e.dxf.hasattr("true_color") for e in model),
        },
        "paperspace": {"layouts": len(paperspaces), "entities": sum(len(layout) for layout in paperspaces),
                       "types": counts(e for layout in paperspaces for e in layout)},
        "entity_database": {"graphical_entities": len(graphical), "graphical_types": counts(graphical)},
        "text": {"all_scopes_count": len(text_entities), "types": counts(text_entities),
                 "raw_text_multiset_sha256": digest_values(raw_text)},
        "layers": {"count": len(doc.layers),
                   "names_sha256": digest_values(layer.dxf.name for layer in doc.layers),
                   "aci_counts": dict(sorted(Counter(str(layer.dxf.color) for layer in doc.layers).items())),
                   "off": sum(layer.is_off() for layer in doc.layers),
                   "frozen": sum(layer.is_frozen() for layer in doc.layers)},
        "blocks": {"definitions_including_layouts": len(doc.blocks),
                   "names_sha256": digest_values(block.name for block in doc.blocks),
                   "entity_count_including_layouts": sum(len(block) for block in doc.blocks),
                   "xref_count": sum(block.block.is_xref for block in doc.blocks),
                   "modelspace_insert_count": sum(e.dxftype() == "INSERT" for e in model)},
        "proxy": {"all_scopes_entity_count": len(proxy_entities),
                  "modelspace_entity_count": sum(e.dxftype() == "ACAD_PROXY_ENTITY" for e in model),
                  "objects_in_database": sum(e.dxftype() == "ACAD_PROXY_OBJECT" for e in database),
                  "graphical_entities_with_proxy_graphics": sum(bool(data) for data in proxy_bytes),
                  "proxy_graphic_bytes": sum(len(data) for data in proxy_bytes if data),
                  "tianzheng_class_count": sum(c.dxf.name.upper().startswith("TCH_") for c in doc.classes)},
        "images": {"image_entities": sum(e.dxftype() == "IMAGE" for e in graphical),
                   "image_definitions": sum(e.dxftype() == "IMAGEDEF" for e in database)},
        "unchanged_during_inspection": digest_file(path) == original_hash,
        "limitations": [
            "Counts and hashes cannot prove visual or semantic preservation.",
            "Modelspace bounding box is approximate and may omit unsupported entity geometry.",
            "Raw text differences may reflect equivalent encoding or MTEXT formatting changes.",
            "Exploding INSERTs changes entity counts even if their appearance is preserved; counts need not match.",
            "Inspect geometry and editing semantics separately from structural inventories.",
        ],
    }
    return result


def changes(left, right, prefix=""):
    result = {}
    for key in sorted(left.keys() | right.keys()):
        if key in {"path", "file_bytes", "sha256", "limitations", "unchanged_during_inspection"}:
            continue
        name = f"{prefix}.{key}" if prefix else key
        a, b = left.get(key), right.get(key)
        if isinstance(a, dict) and isinstance(b, dict):
            result.update(changes(a, b, name))
        elif a != b:
            result[name] = {"before": a, "after": b}
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                result[name]["delta"] = b - a
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", nargs="?", type=Path)
    args = parser.parse_args()
    output = {"before": report(args.before)}
    if args.after:
        output["after"] = report(args.after)
        output["differences"] = changes(output["before"], output["after"])
    json.dump(output, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
