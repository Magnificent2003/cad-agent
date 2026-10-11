"""Read-only, provenance-preserving extraction for spatial fact review (Part A).

This module extracts reference boundaries, not certified building footprints.  It
never repairs, explodes or saves the source drawing.  Curves, clipping and proxy
objects remain explicit limitations rather than being replaced with bounding boxes.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import math
from typing import Any

from ezdxf.lldxf.encoding import decode_dxf_unicode, decode_mif_to_unicode
from ezdxf.math import Matrix44, Vec3
from ezdxf.tools.text import plain_mtext, plain_text
from ezdxf.xclip import XClip
from shapely.geometry import Polygon, mapping
from shapely.validation import explain_validity

from .geometry import _read


EPS = 1e-8


def decode_name(value: Any) -> str:
    """Decode both legacy MIF Chinese names and DXF Unicode escapes."""
    return decode_mif_to_unicode(decode_dxf_unicode(str(value)))


def _matrix_values(matrix: Matrix44) -> list[list[float]]:
    return [[float(value) for value in matrix.get_row(i)] for i in range(4)]


def _reference_polygon(points) -> tuple[dict, float, float]:
    points = [Vec3(point) for point in points]
    if len(points) < 3 or any(not all(math.isfinite(v) for v in point) for point in points):
        raise ValueError("Missing or non-finite polygon vertices.")
    z_values = [point.z for point in points]
    if max(z_values) - min(z_values) > EPS:
        raise ValueError("Transformed boundary is not parallel to the WCS XY plane.")
    xy = [(point.x, point.y) for point in points]
    origin = xy[0]
    local = Polygon([(x - origin[0], y - origin[1]) for x, y in xy])
    if not local.is_valid or local.is_empty or local.area <= EPS:
        raise ValueError("Invalid polygon: " + explain_validity(local))
    # Calculation uses a local origin; serialization retains exact WCS vertices.
    return mapping(Polygon(xy)), float(local.area), float(z_values[0])


def _width(entity) -> dict:
    if entity.dxftype() == "LWPOLYLINE":
        pairs = [(float(p[2]), float(p[3])) for p in entity.get_points("xyse")]
        constant = float(entity.dxf.get("const_width", 0))
        defaults = (0.0, 0.0)
    else:
        pairs = [(float(v.dxf.get("start_width", 0)), float(v.dxf.get("end_width", 0)))
                 for v in entity.vertices]
        constant = 0.0
        defaults = (float(entity.dxf.get("default_start_width", 0)),
                    float(entity.dxf.get("default_end_width", 0)))
    maximum = max([abs(constant), *(abs(v) for v in defaults),
                   *(abs(v) for pair in pairs for v in pair)])
    return {"constant": constant, "default_start": defaults[0], "default_end": defaults[1],
            "vertex_start_end": [list(pair) for pair in pairs] if any(any(pair) for pair in pairs) else [],
            "max_abs": maximum, "coordinate_space": "source_entity_local"}


def extract_drawing(path: str | Path, *, max_depth: int = 32, max_entities: int = 250_000) -> dict:
    """Extract exact straight closed boundaries, text anchors and WIPEOUT masks.

    Every INSERT occurrence is traversed through the *original* block entities, so
    nested/array instances retain source handles.  matrix44 includes the block base
    point, rotation, OCS and scale; child @ parent maps local coordinates to WCS.
    Text positions are anchors, not estimated glyph extents.  Layer 0 inherits its
    enclosing INSERT layer.  No semantic class or actual drawing scale is inferred.
    """
    doc, sha256 = _read(path)
    result = {"schema_version": "spatial-extraction/1", "source_sha256": sha256,
              "source_path": str(Path(path)), "dxf_version": doc.dxfversion,
              "units_code": int(doc.header.get("$INSUNITS", 0)),
              "units_status": "header_declaration_only_not_verified", "coordinate_system": "source_WCS_XY",
              "polygons": [], "texts": [], "wipeouts": [], "inserts": [], "issues": []}
    root_counts = Counter(entity.dxftype() for entity in doc.modelspace())
    expanded_counts: Counter = Counter()
    non_area: Counter = Counter()
    seen = 0
    layer_metadata = {}
    for layer in doc.layers:
        layer_metadata[layer.dxf.name] = {"name": decode_name(layer.dxf.name),
                                          "off": layer.is_off(), "frozen": layer.is_frozen(),
                                          "color_aci": abs(layer.dxf.color)}
    result["layers"] = layer_metadata

    def issue(code, message, base):
        result["issues"].append({"code": code, "message": message,
                                  "id": base["id"], "entity_type": base["entity_type"],
                                  "source": base["source"], "layer": base["effective_layer"]})

    def visit(entity, matrix, root_handle, insert_path=(), block_path=(), inherited_layer="0", clipped=False, external=False):
        nonlocal seen
        seen += 1
        kind = entity.dxftype()
        expanded_counts[kind] += 1
        handle = str(entity.dxf.get("handle", "missing-handle"))
        raw_layer = str(entity.dxf.get("layer", "0"))
        effective_raw = inherited_layer if raw_layer == "0" and insert_path else raw_layer
        source = {"root_handle": root_handle, "entity_handle": handle,
                  "insert_path": list(insert_path), "block_path": list(block_path),
                  "transform_wcs": _matrix_values(matrix)}
        ident = "/".join([*insert_path, handle])
        base = {"id": ident, "source": source, "entity_type": kind,
                "layer": decode_name(raw_layer), "raw_layer": raw_layer,
                "effective_layer": decode_name(effective_raw), "effective_raw_layer": effective_raw,
                "visibility": {"entity_invisible": bool(entity.dxf.get("invisible", 0)),
                               "layer": layer_metadata.get(effective_raw, {}),
                               "inside_xclip": clipped, "unverified_external_reference": external}}
        if seen > max_entities:
            raise RuntimeError(f"Expanded entity limit ({max_entities}) exceeded; no partial result was returned.")
        if kind == "INSERT":
            name = entity.dxf.name
            if name in block_path or len(block_path) >= max_depth:
                issue("recursive_or_deep_insert", "Block recursion/depth limit; contents were not inferred.", base)
                return
            block = entity.block()
            if block is None:
                issue("missing_block", "INSERT has no resolvable block definition.", base)
                return
            clip = XClip(entity)
            has_clip = bool(clip.has_clipping_path and clip.is_clipping_enabled)
            if has_clip:
                issue("xclip_scope_unresolved", "Unclipped source boundaries retained for review only; XCLIP is not applied.", base)
            is_external = bool(block.block.is_xref or block.block.is_xref_overlay)
            if is_external:
                base["visibility"]["unverified_external_reference"] = True
                issue("external_reference", "External reference contents may be missing or out of date.", base)
            try:
                instances = list(entity.multi_insert()) if entity.mcount > 1 else [entity]
                for index, instance in enumerate(instances):
                    occurrence = handle if len(instances) == 1 else f"{handle}[{index}]"
                    transform = instance.matrix44() @ matrix
                    if not all(math.isfinite(value) for row in _matrix_values(transform) for value in row):
                        raise ValueError("Non-finite INSERT transform.")
                    record = dict(base, block_name=decode_name(name), raw_block_name=name,
                                  instance_index=index if len(instances) > 1 else None,
                                  position_wcs=list(matrix.transform(instance.ocs().to_wcs(instance.dxf.insert))),
                                  content_transform_wcs=_matrix_values(transform),
                                  geometry_status=("unresolved_xclip" if clipped or has_clip else
                                                   "unresolved_external_reference" if external or is_external else "expanded"))
                    if len(instances) > 1:
                        record["id"] = "/".join([*insert_path, occurrence])
                    result["inserts"].append(record)
                    for child in block:
                        visit(child, transform, root_handle, (*insert_path, occurrence),
                              (*block_path, name), effective_raw, clipped or has_clip, external or is_external)
                    # Attached ATTRIB positions are already in INSERT's parent coordinate system.
                    for attrib in instance.attribs:
                        visit(attrib, matrix, root_handle, (*insert_path, occurrence),
                              (*block_path, name), effective_raw, clipped or has_clip, external or is_external)
            except (ValueError, TypeError, ArithmeticError) as exc:
                issue("insert_transform_failed", str(exc), base)
            return
        if kind in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
            # Nonconstant ATTDEFs are templates; do not invent instance values.
            if kind == "ATTDEF" and not entity.is_const:
                non_area["ATTDEF_template"] += 1
                return
            try:
                if kind == "MTEXT":
                    raw_text = entity.text
                    value = decode_name(plain_mtext(raw_text))
                    anchor = Vec3(entity.dxf.insert)  # MTEXT insertion is WCS (not OCS).
                    direction = Vec3(entity.get_text_direction()).normalize()
                    normal = Vec3(entity.dxf.get("extrusion", (0, 0, 1))).normalize()
                    height_vector = normal.cross(direction) * float(entity.dxf.char_height)
                else:
                    raw_text = entity.dxf.text
                    value = decode_name(plain_text(raw_text))
                    anchor = entity.ocs().to_wcs(entity.dxf.insert)
                    angle = math.radians(entity.dxf.get("rotation", 0))
                    direction = entity.ocs().to_wcs(Vec3(math.cos(angle), math.sin(angle), 0))
                    height_vector = entity.ocs().to_wcs(
                        Vec3(-math.sin(angle), math.cos(angle), 0) * float(entity.dxf.height))
                position = matrix.transform(anchor)
                world_direction = matrix.transform_direction(direction)
                height = matrix.transform_direction(height_vector).magnitude
                rotation = math.degrees(math.atan2(world_direction.y, world_direction.x)) % 360
                if not all(math.isfinite(v) for v in position):
                    raise ValueError("Non-finite text anchor.")
                result["texts"].append(dict(base, text=value, raw_text=raw_text,
                                             position=[position.x, position.y], elevation=position.z,
                                             height_native=height, rotation_deg=rotation,
                                             height_interpretation="transformed_character_height_not_glyph_extent",
                                             position_kind="insertion_anchor_not_glyph_extent",
                                             geometry_status=("unresolved_xclip" if clipped else
                                                              "unresolved_external_reference" if external else "anchor_only")))
            except (ValueError, TypeError, ArithmeticError) as exc:
                issue("text_read_failed", str(exc), base)
            return
        if kind not in {"LWPOLYLINE", "POLYLINE", "SOLID", "TRACE", "3DFACE", "WIPEOUT"}:
            non_area[kind] += 1
            if kind in {"ACAD_PROXY_ENTITY", "HATCH", "CIRCLE", "ELLIPSE", "SPLINE", "REGION", "3DSOLID", "ARC"}:
                issue("unresolved_geometry", "No occupied polygon inferred from this entity type.", base)
            return
        width = {"max_abs": 0.0, "coordinate_space": "source_entity_local"}
        try:
            if kind in {"LWPOLYLINE", "POLYLINE"}:
                closed = bool(entity.closed) if kind == "LWPOLYLINE" else bool(entity.is_closed)
                if not closed:
                    non_area["open_" + kind] += 1
                    return
                if kind == "POLYLINE" and not entity.is_2d_polyline:
                    raise ValueError("3D/mesh POLYLINE is not a verified 2D footprint.")
                bulges = [p[4] for p in entity.get_points("xyseb")] if kind == "LWPOLYLINE" else [
                    v.dxf.get("bulge", 0) for v in entity.vertices]
                if any(abs(value) > 0 for value in bulges):
                    raise ValueError("Closed boundary contains arc bulges; no straight-line approximation used.")
                if abs(entity.dxf.get("thickness", 0)) > EPS:
                    raise ValueError("Nonzero thickness is not a verified 2D boundary.")
                points = list(entity.vertices_in_wcs() if kind == "LWPOLYLINE" else entity.points_in_wcs())
                width = _width(entity)
            elif kind == "WIPEOUT":
                points = entity.boundary_path_wcs()
            elif kind == "3DFACE":
                points = [entity.dxf.get(f"vtx{i}") for i in range(4)]
            else:
                if abs(entity.dxf.get("thickness", 0)) > EPS:
                    raise ValueError("Nonzero thickness is not a verified 2D boundary.")
                points = list(entity.wcs_vertices())
            geometry, area, elevation = _reference_polygon(matrix.transform_vertices(points))
            # Width belongs to the entity's OCS; only uniform in-plane scaling admits a scalar WCS width.
            ocs = entity.ocs()
            ux = matrix.transform_direction(ocs.to_wcs(Vec3(1, 0, 0)))
            uy = matrix.transform_direction(ocs.to_wcs(Vec3(0, 1, 0)))
            uniform = abs(ux.magnitude - uy.magnitude) <= EPS * max(ux.magnitude, uy.magnitude, 1.0)
            uniform = uniform and abs(ux.dot(uy)) <= EPS * max(ux.magnitude * uy.magnitude, 1.0)
            notes = []
            if width["max_abs"]:
                notes.append("Boundary centerline retained; displayed width is not automatically a physical setback or footprint expansion.")
            if not uniform:
                notes.append("Nonuniform/sheared instance transform: no scalar WCS line width inferred.")
            if clipped:
                notes.append("XCLIP not applied: source geometry is not verified as current visible design.")
            if external:
                notes.append("External-reference contents are unverified: reference availability, freshness and design validity need confirmation.")
            record = dict(base, geometry=geometry, area_native2=area, elevation=elevation,
                          width_native=width,
                          width_wcs_max=(width["max_abs"] * ux.magnitude if uniform else None),
                          geometry_status=("unresolved_xclip" if clipped else
                                           "unresolved_external_reference" if external else "valid_reference_boundary"),
                          interpretation_status="pending_semantic_and_width_confirmation",
                          notes=notes)
            result["wipeouts" if kind == "WIPEOUT" else "polygons"].append(record)
        except (ValueError, TypeError, ArithmeticError) as exc:
            issue("boundary_not_resolved", str(exc), base)

    for entity in doc.modelspace():
        visit(entity, Matrix44(), str(entity.dxf.get("handle", "missing-handle")))
    result["inventory"] = {"root_entities": sum(root_counts.values()), "root_by_type": dict(root_counts),
                           "expanded_entities": seen, "expanded_by_type": dict(expanded_counts),
                           "non_area_by_type": dict(non_area),
                           "reference_polygons": len(result["polygons"]), "text_anchors": len(result["texts"]),
                           "wipeout_masks": len(result["wipeouts"]), "insert_instances": len(result["inserts"]),
                           "issues_by_code": dict(Counter(issue["code"] for issue in result["issues"]))}
    result["limitations"] = [
        "Semantic identity, design phase and actual scale require evidence beyond closed geometry.",
        "Open lines are counted, not polygonized; curved/proxy/hatched objects are unresolved.",
        "Reference polygons use boundary centerlines, not an inferred interpretation of display widths.",
        "WIPEOUT masks do not erase geometry and are not treated as free land.",
        "XCLIP and external-reference validity require separate review.",
        "Text anchors are not text bounding boxes; drawing order is not a design-validity test.",
    ]
    return result
