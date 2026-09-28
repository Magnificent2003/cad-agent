"""Conservative 2D DXF inspection and validation against explicit site facts.

Supported footprints are straight, closed planar polylines, SOLID/TRACE/3DFACE,
plus INSERTs whose fully transformed contents have one unambiguous footprint.
Bulges, curved footprints, meshes, tilted planes and ambiguous blocks are rejected;
no bounding box, convex hull, or point sampling substitutes for occupied geometry.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Any

import ezdxf
from ezdxf.entities import DXFGraphic
from ezdxf.lldxf.tagwriter import TagCollector
from ezdxf.math import Vec2, Vec3
from shapely.affinity import translate
from shapely.geometry import LineString, Polygon, mapping
from shapely.ops import nearest_points

APPID = "CAD_AGENT_SITE"
# INSUNITS is a declaration, never independent verification of drawing scale.
UNIT_METRES = {
    1: .0254, 2: .3048, 3: 1609.344, 4: .001, 5: .01, 6: 1., 7: 1000.,
    8: .0000000254, 9: .0000254, 10: .9144, 11: 1e-10, 12: 1e-9,
    13: 1e-6, 14: .1, 15: 10., 16: 100., 17: 1e9, 18: 149597870700.,
    19: 9460730472580800., 20: 3.085677581491367e16,
    21: 1200 / 3937, 22: 100 / 3937, 23: 3600 / 3937, 24: 6336000 / 3937,
}
ANNOTATIONS = {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}
EPS = 1e-9


@dataclass(frozen=True)
class Site:
    boundary: Polygon
    obstacles: list[tuple[str, Polygon]]
    metres_per_unit: float
    origin: tuple[float, float]
    angle_deg: float
    source_sha256: str
    clearance_m: float = 0.0
    boundary_handles: tuple[str, ...] = ()


class FactsError(ValueError):
    def __init__(self, message: str, code: str = "unconfirmed_facts", status: str = "unknown"):
        super().__init__(message)
        self.code = code
        self.status = status


def digest(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read(path):
    # Parse exactly the bytes hashed, not a second potentially replaced file.
    data = Path(path).read_bytes()
    if data.startswith(b"AutoCAD Binary DXF"):
        raise ValueError("Binary DXF is unsupported; provide an ASCII DXF without repairing geometry.")
    from ezdxf.lldxf.validator import dxf_info
    probe = data.decode("latin1")
    info = dxf_info(io.StringIO(probe))
    encoding = "utf-8" if info.version >= "AC1021" else info.encoding
    doc = ezdxf.read(io.StringIO(data.decode(encoding, errors="surrogateescape")))
    return doc, hashlib.sha256(data).hexdigest()


def _number(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number.")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number.") from None
    if not math.isfinite(number) or (positive and number <= 0) or (nonnegative and number < 0):
        raise ValueError(f"{name} must be finite" + (" and positive." if positive else " and nonnegative." if nonnegative else "."))
    return number


def _json(value):
    if isinstance(value, (Vec2, Vec3)):
        return _json(list(value))
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest(), "length": len(value)}
    if isinstance(value, dict):
        return {str(k): _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)


def _planar(entity):
    normal = Vec3(entity.dxf.get("extrusion", (0, 0, 1)))
    if not all(math.isfinite(v) for v in normal) or abs(normal.x) > EPS or abs(normal.y) > EPS or abs(abs(normal.z) - 1) > EPS:
        raise ValueError("Only verified XY-plane OCS geometry is supported; tilted extrusion is unsupported.")
    if abs(entity.dxf.get("thickness", 0)) > EPS:
        raise ValueError("Nonzero thickness is unsupported for 2D occupied footprints.")


def _xy(points):
    values = [tuple(float(x) for x in p) for p in points]
    if not values or any(len(p) < 2 or not all(math.isfinite(v) for v in p) for p in values):
        raise ValueError("Geometry has empty or non-finite coordinates.")
    zs = [p[2] if len(p) > 2 else 0.0 for p in values]
    if max(zs) - min(zs) > EPS:
        raise ValueError("Nonplanar geometry is unsupported for a 2D footprint.")
    return [(p[0], p[1]) for p in values]


def _poly_points(entity, *, allow_width=False):
    _planar(entity)
    if entity.dxftype() == "LWPOLYLINE":
        points = list(entity.get_points("xyseb"))
        if any(p[4] != 0 for p in points):
            raise ValueError("Bulged polyline segments require curved-geometry support; no straight approximation was used.")
        if not allow_width and (abs(entity.dxf.get("const_width", 0)) > EPS or any(abs(p[2]) > EPS or abs(p[3]) > EPS for p in points)):
            raise ValueError("A widened polyline cannot be treated as a zero-width polygon boundary.")
        return _xy(entity.vertices_in_wcs()), bool(entity.closed)
    if entity.dxftype() == "POLYLINE":
        if not entity.is_2d_polyline:
            raise ValueError("Only 2D POLYLINE footprints are supported; mesh/3D polyline is unsupported.")
        if any(v.dxf.get("bulge", 0) != 0 for v in entity.vertices):
            raise ValueError("Bulged polyline segments are unsupported.")
        if not allow_width and (abs(entity.dxf.get("default_start_width", 0)) > EPS or abs(entity.dxf.get("default_end_width", 0)) > EPS or any(abs(v.dxf.get("start_width", 0)) > EPS or abs(v.dxf.get("end_width", 0)) > EPS for v in entity.vertices)):
            raise ValueError("A widened polyline is not a zero-width polygon boundary.")
        return _xy(entity.points_in_wcs()), bool(entity.is_closed)
    raise ValueError(f"{entity.dxftype()} is not a supported straight polyline.")


def _virtual_children(entity, limit=None):
    if entity.mcount != 1:
        raise ValueError("Array INSERT footprints are ambiguous; select explicit resolved footprints.")
    skipped = []
    try:
        children = []
        for child in entity.virtual_entities(skipped_entity_callback=lambda e, why: skipped.append(why)):
            children.append(child)
            if limit is not None and len(children) >= limit:
                break
    except (ezdxf.DXFError, ValueError, TypeError, RecursionError) as exc:
        raise ValueError(f"Cannot resolve INSERT transform: {exc}") from exc
    if skipped:
        raise ValueError("INSERT contains unsupported transformed geometry: " + "; ".join(skipped))
    return children


def _polygon(entity, ancestry=()):
    kind = entity.dxftype()
    if kind == "INSERT":
        name = entity.dxf.name
        if name in ancestry or len(ancestry) >= 24:
            raise ValueError("Recursive or excessively nested INSERT is unsupported.")
        footprints = []
        for child in _virtual_children(entity):
            if child.dxftype() in ANNOTATIONS:
                continue
            footprints.append(_polygon(child, (*ancestry, name)))
        if len(footprints) != 1:
            raise ValueError("INSERT must contain exactly one unambiguous closed footprint; multiple/empty footprints cannot be inferred.")
        return footprints[0]
    if kind in {"LWPOLYLINE", "POLYLINE"}:
        points, closed = _poly_points(entity)
        if not closed:
            raise ValueError("An open polyline does not define an occupied polygon.")
    elif kind in {"SOLID", "TRACE", "3DFACE"}:
        _planar(entity)
        points = _xy(entity.wcs_vertices() if kind != "3DFACE" else [entity.dxf.get(f"vtx{i}") for i in range(4)])
    else:
        raise ValueError(f"Unsupported footprint type {kind}; no bounding box or convex hull was substituted.")
    if len(set(points)) < 3:
        raise ValueError("A polygon requires at least three distinct points.")
    polygon = Polygon(points)
    if not polygon.is_valid or polygon.is_empty or polygon.area <= 0:
        raise ValueError("Footprint must be a valid, non-self-intersecting polygon with positive area.")
    return polygon


def entity_polygon(doc, handle: str) -> Polygon:
    """Read one exact supported footprint in WCS native drawing units."""
    handle = str(handle).upper()
    entity = doc.entitydb.get(handle)
    if entity is None or not entity.is_alive:
        raise ValueError(f"Footprint handle {handle} does not exist.")
    if entity.dxf.get("owner") != doc.modelspace().block_record_handle:
        raise ValueError(f"Footprint handle {handle} is not a modelspace entity; select its transformed INSERT instead of a block definition.")
    return _polygon(entity)


def _confirmed(facts, name):
    item = facts.get(name)
    if not isinstance(item, dict) or item.get("status") != "confirmed":
        raise FactsError(f"{name} must be explicitly confirmed with evidence.")
    evidence = item.get("evidence")
    if not evidence or isinstance(evidence, str) and not evidence.strip():
        raise FactsError(f"{name} requires nonempty confirmation evidence.")
    return item


def _handles(item, name, *, empty=False):
    handles = item.get("handles")
    if not isinstance(handles, list) or (not handles and not empty) or any(not isinstance(h, str) or not h.strip() for h in handles):
        raise FactsError(f"{name}.handles must be an explicit {'possibly empty ' if empty else 'nonempty '}list of handles.")
    handles = [h.upper() for h in handles]
    if len(set(handles)) != len(handles):
        raise FactsError(f"{name}.handles contains duplicate handles.")
    return handles


def _site(doc, source_sha256, facts):
    if not isinstance(facts, dict):
        raise FactsError("Site validation requires source-bound confirmed facts.")
    if not facts.get("source_sha256"):
        raise FactsError("Facts must identify the baseline source_sha256.")
    if facts["source_sha256"] != source_sha256:
        raise FactsError("Site facts refer to different source bytes; reconfirm against this baseline.", "stale_facts", "failed")
    units = _confirmed(facts, "units")
    try:
        scale = _number(units.get("metres_per_unit"), "units.metres_per_unit", positive=True)
    except ValueError as exc:
        raise FactsError(str(exc)) from exc
    declared = UNIT_METRES.get(int(doc.header.get("$INSUNITS", 0)))
    if declared is not None and not math.isclose(scale, declared, rel_tol=1e-9, abs_tol=0) and units.get("override_header") is not True:
        raise FactsError("Confirmed units contradict INSUNITS; explicit override_header=true and confirmation evidence are required.", "units_conflict")
    boundary_handles = _handles(_confirmed(facts, "boundary"), "boundary")
    if len(boundary_handles) != 1:
        raise FactsError("Exactly one unambiguous closed boundary footprint is supported; separate fragments are not silently joined.", "unsupported_boundary")
    obstacle_handles = _handles(_confirmed(facts, "obstacles"), "obstacles", empty=True)
    if boundary_handles[0] in obstacle_handles:
        raise FactsError("The same handle cannot be both site boundary and obstacle.")
    try:
        boundary = entity_polygon(doc, boundary_handles[0])
        obstacles = [(h, entity_polygon(doc, h)) for h in obstacle_handles]
    except ValueError as exc:
        raise FactsError(str(exc), "unsupported_site_geometry") from exc
    frame = facts.get("frame", {})
    if not isinstance(frame, dict):
        raise FactsError("frame must contain an optional origin and angle_deg.")
    origin = frame.get("origin", [0, 0])
    if not isinstance(origin, (list, tuple)) or len(origin) != 2:
        raise FactsError("frame.origin must contain two WCS drawing coordinates.")
    try:
        origin = tuple(_number(v, "frame.origin") for v in origin)
        angle = _number(frame.get("angle_deg", 0), "frame.angle_deg")
        clearance = _number(facts.get("clearance_m", 0), "clearance_m", nonnegative=True)
    except ValueError as exc:
        raise FactsError(str(exc)) from exc
    return Site(boundary, obstacles, scale, origin, angle, source_sha256, clearance, tuple(boundary_handles))


def load_site(source: str | Path, facts: dict) -> Site:
    """Resolve explicit confirmed facts against their immutable baseline DXF."""
    doc, source_sha256 = _read(source)
    return _site(doc, source_sha256, facts)


def _limited_value(value, budget):
    """Bound inspection output only; validators never consume these previews."""
    value = _json(value)
    if isinstance(value, str):
        count = min(len(value), 512, budget["text_chars"])
        budget["text_chars"] -= count
        if count < len(value):
            budget["truncated"] = True
            return value[:count] + "…[truncated]"
        return value
    if isinstance(value, list):
        if len(value) > 16:
            budget["truncated"] = True
        return [_limited_value(v, budget) for v in value[:16]]
    if isinstance(value, dict):
        if len(value) > 48:
            budget["truncated"] = True
        return {k: _limited_value(v, budget) for k, v in list(value.items())[:48]}
    return value


def _record(entity, ancestry=(), coordinate_system="WCS", budget=None):
    if budget is None:
        budget = {"nodes": 100, "vertices": 512, "text_chars": 6000, "truncated": False}
    if budget["nodes"] <= 0:
        budget["truncated"] = True
        return {"handle": entity.dxf.get("handle"), "type": entity.dxftype(), "inspection_truncated": True,
                "footprint": {"status": "unknown", "reason": "Inspection node budget exhausted."}}
    budget["nodes"] -= 1
    record = {"handle": entity.dxf.get("handle"), "type": entity.dxftype(),
              "layer": _limited_value(entity.dxf.get("layer", "0"), budget),
              "dxf_attributes": _limited_value(entity.dxf.all_existing_dxf_attribs(), budget)}
    if record["handle"] is None and entity.source_of_copy is not None:
        record["source_handle"] = entity.source_of_copy.dxf.get("handle")
    if entity.dxftype() in {"TEXT", "MTEXT", "ATTRIB", "ATTDEF"}:
        record["text"] = _limited_value(entity.text if entity.dxftype() == "MTEXT" else entity.dxf.text, budget)
    if entity.dxftype() in {"LWPOLYLINE", "POLYLINE"}:
        try:
            points, closed = _poly_points(entity, allow_width=True)
            count = min(len(points), budget["vertices"])
            budget["vertices"] -= count
            record["geometry"] = {"points": points[:count], "closed": closed, "coordinate_system": coordinate_system,
                                  "total_vertices": len(points), "vertices_truncated": count < len(points)}
            if count < len(points):
                budget["truncated"] = True
        except ValueError as exc:
            record["geometry_error"] = str(exc)
        native = list(entity.get_points("xyseb")) if entity.dxftype() == "LWPOLYLINE" else [v.dxf.all_existing_dxf_attribs() for v in entity.vertices]
        count = min(len(native), budget["vertices"])
        budget["vertices"] -= count
        record["native_points"] = _json(native[:count])
        record["native_points_truncated"] = count < len(native)
        budget["truncated"] |= count < len(native)
    if entity.dxftype() == "INSERT":
        record["block_name"] = _limited_value(entity.dxf.name, budget)
        record["transform"] = list(entity.matrix44())
        record["attributes"] = []
        for attrib in entity.attribs:
            if budget["nodes"] <= 0:
                record["attributes_truncated"] = budget["truncated"] = True
                break
            record["attributes"].append(_record(attrib, ancestry, coordinate_system, budget))
        record["block_path"] = list(ancestry) + [record["block_name"]]
        try:
            if entity.dxf.name in ancestry or len(ancestry) >= 24:
                raise ValueError("Recursive or excessively nested INSERT.")
            children = _virtual_children(entity, limit=max(1, budget["nodes"] + 1))
            record["children"] = []
            for child in children:
                if budget["nodes"] <= 0:
                    record["children_truncated"] = budget["truncated"] = True
                    break
                record["children"].append(_record(child, (*ancestry, entity.dxf.name), coordinate_system, budget))
            if record.get("children_truncated"):
                record["footprint"] = {"status": "unknown", "reason": "INSERT inspection was truncated; request targeted entities for further evidence."}
            else:
                spatial = [c for c in record["children"] if c["type"] not in ANNOTATIONS]
                if len(spatial) == 1:
                    record["footprint"] = dict(spatial[0]["footprint"])
                else:
                    record["footprint"] = {"status": "unsupported", "reason": "INSERT does not contain one unambiguous supported closed footprint."}
        except ValueError as exc:
            record["expansion_error"] = str(exc)
            record["footprint"] = {"status": "unsupported", "reason": str(exc)}
        return record
    try:
        polygon = _polygon(entity)
        record["footprint"] = {"status": "supported", "coordinate_system": coordinate_system,
                               "area_drawing_units2": polygon.area, "bounds": list(polygon.bounds)}
        vertex_count = len(polygon.exterior.coords) + sum(len(r.coords) for r in polygon.interiors)
        if vertex_count <= budget["vertices"]:
            budget["vertices"] -= vertex_count
            record["footprint"]["geometry"] = _json(mapping(polygon))
        else:
            budget["truncated"] = True
            record["footprint"]["geometry_truncated"] = True
    except ValueError as exc:
        record["footprint"] = {"status": "unsupported", "reason": str(exc)}
    return record


def inspect_dxf(path: str | Path, handles=None, limit=30) -> dict:
    """Expose bounded source evidence; candidates never imply semantic site facts."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500.")
    doc, sha256 = _read(path)
    if handles is not None and (not isinstance(handles, (list, tuple)) or any(not isinstance(h, str) for h in handles)):
        raise ValueError("handles must be a list of entity handles.")
    selected = None if handles is None else {h.upper() for h in handles}
    entities = [e for e in doc.modelspace() if selected is None or e.dxf.handle in selected]
    if selected is not None:
        found = {e.dxf.handle for e in entities}
        entities.extend(doc.entitydb[h] for h in sorted(selected - found) if h in doc.entitydb)
    budget = {"nodes": 100, "vertices": 512, "text_chars": 6000, "truncated": False}
    records = []
    for entity in entities[:limit]:
        if budget["nodes"] <= 0:
            budget["truncated"] = True
            break
        record = _record(entity, coordinate_system="WCS" if entity.dxf.get("owner") == doc.modelspace().block_record_handle else "definition_or_paperspace_local", budget=budget)
        records.append(record)
    counts = Counter(e.dxftype() for e in doc.modelspace())
    layers = {}
    for entity in doc.modelspace():
        name = entity.dxf.get("layer", "0")
        if name not in layers:
            layers[name] = Counter()
        layers[name][entity.dxftype()] += 1
    overview = []
    for layer in list(doc.layers)[:80]:
        name = layer.dxf.name
        layer_counts = layers.get(name, Counter())
        overview.append({"name": name[:160], "color": layer.dxf.color,
                         "entity_count": sum(layer_counts.values()), "entity_types": dict(layer_counts.most_common(40))})
    code = int(doc.header.get("$INSUNITS", 0))
    report = {"sha256": sha256, "source_sha256": sha256, "dxf_version": doc.dxfversion,
              "units": {"status": "declared" if code else "unknown", "header": "$INSUNITS", "code": code, "metres_per_unit": UNIT_METRES.get(code), "confirmed": False},
              "overview": {"modelspace_entity_count": sum(counts.values()), "entity_types": dict(counts.most_common(40)),
                           "layers": overview, "layer_count": len(doc.layers), "truncated": len(doc.layers) > 80 or len(counts) > 40},
              "entities": records, "candidate_handles": [], "total": len(entities), "limit": limit,
              "truncated": budget["truncated"] or len(entities) > len(records),
              "missing_handles": sorted(selected - {e.dxf.handle for e in entities})[:100] if selected is not None else [],
              "inspection_limits": {"nodes": 100, "vertices": 512, "text_chars": 6000, "json_chars": 64000},
              "semantic_facts_inferred": False,
              "scope": "Exact straight planar 2D primitives; transformed unambiguous INSERT footprints. Curves and ambiguous geometry remain unsupported. Inspection truncation never relaxes actual-file validation."}
    while len(json.dumps(report, ensure_ascii=True)) > 62000 and records:
        report["truncated"] = True
        if len(records) > 1:
            records.pop()
        else:
            record = records[0]
            for key in ("children", "attributes", "native_points", "dxf_attributes", "geometry"):
                record.pop(key, None)
            record.get("footprint", {}).pop("geometry", None)
            record["details_truncated"] = True
            break
    while len(json.dumps(report, ensure_ascii=True)) > 62000 and overview:
        overview.pop()
        report["truncated"] = report["overview"]["truncated"] = True
    while len(json.dumps(report, ensure_ascii=True)) > 62000 and report["missing_handles"]:
        report["missing_handles"].pop()
        report["truncated"] = True
    report["candidate_handles"] = [r["handle"] for r in records if r["footprint"]["status"] == "supported" and r["footprint"].get("coordinate_system") == "WCS"]
    return report


def _snapshots(doc):
    result = {}
    protected_tables = {"BLOCK", "ENDBLK", "BLOCK_RECORD", "LAYER", "LTYPE", "STYLE", "DIMSTYLE", "UCS"}
    for handle, entity in doc.entitydb.items():
        if not entity.is_alive or not (isinstance(entity, DXFGraphic) or entity.dxftype() in protected_tables):
            continue
        if entity.dxftype() == "SEQEND" and not (entity.xdata or entity.appdata or entity.has_extension_dict):
            continue
        collector = TagCollector(dxfversion=doc.dxfversion, optional=False)
        entity.export_dxf(collector)
        payload = [[tag.code, _json(tag.value)] for tag in collector.tags]
        result[handle] = hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True).encode()).hexdigest()
    return result


def _metadata(entity):
    if not entity.has_xdata(APPID):
        return None
    tags = entity.get_xdata(APPID)
    value = json.loads("".join(str(tag.value) for tag in tags if tag.code == 1000))
    if not isinstance(value, dict):
        raise ValueError("Component XDATA must be a JSON object.")
    return {**value, "handle": entity.dxf.handle}


def _location(a, b=None):
    if b is None:
        p = a.representative_point()
    elif a.intersects(b):
        p = a.intersection(b).representative_point()
    else:
        p, _ = nearest_points(a, b)
    return [p.x, p.y]


def _entity_location(entity):
    """A diagnostic point on an invalid entity, without inventing a footprint."""
    try:
        if entity.dxftype() in {"LWPOLYLINE", "POLYLINE"}:
            points, closed = _poly_points(entity, allow_width=True)
            if closed and points and points[-1] != points[0]:
                points.append(points[0])
            if 3 < len(points) <= 200:
                segments = [LineString((a, b)) for a, b in zip(points, points[1:]) if a != b]
                for i, a in enumerate(segments):
                    for j in range(i + 2, len(segments)):
                        if closed and i == 0 and j == len(segments) - 1:
                            continue
                        crossing = a.intersection(segments[j])
                        if not crossing.is_empty:
                            return {"location": _location(crossing)}
            if points:
                return {"location": list(points[0])}
    except (ValueError, ezdxf.DXFError):
        pass
    return {}


def _rectangle_dimensions(polygon):
    # GEOS rotated-envelope calculations lose precision at survey coordinates.
    # Translate only this measurement into a local frame; occupied geometry is
    # never changed or replaced by its envelope.
    coordinate_ulp = math.ulp(max(abs(v) for v in polygon.bounds))
    x0, y0 = polygon.exterior.coords[0]
    polygon = translate(polygon, xoff=-x0, yoff=-y0)
    rect = polygon.minimum_rotated_rectangle
    if not polygon.equals(rect):
        # Rotation arithmetic may introduce sub-ulp differences; require area
        # equality as well as near-identical complete boundaries.
        tolerance = max(1e-9, coordinate_ulp * 32, polygon.length * 1e-12)
        if polygon.symmetric_difference(rect).area > polygon.length * tolerance or polygon.hausdorff_distance(rect) > tolerance:
            raise ValueError("Dimension verification supports exact rectangular footprints only.")
    pts = list(rect.exterior.coords)
    return sorted([math.dist(pts[0], pts[1]), math.dist(pts[1], pts[2])])


def _road_geometry(entity, component):
    if component.get("kind", component.get("geometry_kind", component.get("representation"))) == "corridor":
        polygon = _polygon(entity)
        dims = _rectangle_dimensions(polygon)
        return polygon, dims[0]
    points, closed = _poly_points(entity, allow_width=True)
    if closed and points and points[-1] != points[0]:
        points.append(points[0])
    if len(points) < 2:
        raise ValueError("Road centerline needs at least two distinct points.")
    centerline = LineString(points)
    if not centerline.is_valid or not centerline.is_simple or centerline.length <= 0:
        raise ValueError("Road centerline must be valid and simple, without self-intersection.")
    if any(a == b for a, b in zip(points, points[1:])):
        raise ValueError("Road centerline contains a zero-length segment.")
    if entity.dxftype() != "LWPOLYLINE":
        raise ValueError("Road width verification currently requires an LWPOLYLINE constant width.")
    width = _number(entity.dxf.get("const_width", 0), "actual road const_width", positive=True)
    if any(abs(p[2]) > EPS or abs(p[3]) > EPS for p in entity.get_points("xyseb")):
        raise ValueError("Road per-segment widths are unsupported; use one real constant width.")
    # DXF wide straight polylines have butt end caps and mitred joins. Reject
    # unbounded joins rather than silently applying Shapely's bevel cutoff.
    turn_points = points + [points[1]] if closed else points
    for a, b, c in zip(turn_points, turn_points[1:], turn_points[2:]):
        ab, bc = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
        cosine = (ab[0] * bc[0] + ab[1] * bc[1]) / (math.hypot(*ab) * math.hypot(*bc))
        if cosine < -.98:
            raise ValueError("Near-reversing road joins cannot be reliably validated.")
    polygon = centerline.buffer(width / 2, cap_style="flat", join_style="mitre", mitre_limit=100)
    if polygon.geom_type != "Polygon" or not polygon.is_valid or polygon.area <= 0:
        raise ValueError("Road corridor is not a valid occupied polygon.")
    return polygon, width


def occupied_polygon(doc, handle: str, component: dict | None = None) -> Polygon:
    """Return a modelspace component's actual supported WCS occupied polygon.

    A road uses its actual constant DXF width, never its claimed metadata width.
    Closed footprints remain available through entity_polygon.
    """
    entity = doc.entitydb.get(str(handle).upper())
    if entity is None or not entity.is_alive or entity.dxf.get("owner") != doc.modelspace().block_record_handle:
        raise ValueError(f"Occupied handle {handle} does not identify a modelspace entity.")
    metadata = component if component is not None else _metadata(entity) or {}
    if metadata.get("role") == "road":
        return _road_geometry(entity, metadata)[0]
    return _polygon(entity)


def check_dxf(source, target, *, profile="general", facts=None, components=None, allowed_changes=None) -> dict:
    """Reread actual DXFs and return a hash-bound preservation/geometry report.

    'passed' concerns only the named checks. No construction-code or engineering
    compliance is inferred. Unsupported spatial geometry produces 'unknown'.
    """
    report: dict[str, Any] = {"status": "passed", "profile": profile, "source_sha256": None, "file_sha256": None, "issues": [], "checks": {}, "scope": "Native preservation and supported 2D geometry only; construction norms are not tested."}

    def issue(code, message, handles=(), *, status="failed", **details):
        report["issues"].append({"code": code, "message": message, "handles": list(handles), "status": status, **details})
        if status == "failed" or report["status"] == "passed":
            report["status"] = status

    try:
        # Hash even an invalid file when possible, so rejected artifacts are bound.
        report["source_sha256"] = digest(source)
        report["file_sha256"] = digest(target)
        original, report["source_sha256"] = _read(source)
        current, report["file_sha256"] = _read(target)
    except (OSError, ezdxf.DXFError, UnicodeError, ValueError) as exc:
        issue("invalid_dxf", f"Unable to read actual DXF bytes: {exc}")
        report["checks"]["readability"] = "failed"
        return report
    report["checks"]["readability"] = "passed"
    if profile not in {"general", "site_layout"}:
        issue("unknown_profile", f"Unsupported validation profile {profile!r}.", status="unknown")
        return report
    for name, path in (("source", source), ("target", target)):
        # audit mutates its doc: never use that copy as the evidence geometry.
        try:
            audit_doc, audit_hash = _read(path)
            if audit_hash != report["source_sha256" if name == "source" else "file_sha256"]:
                issue("file_changed_during_check", f"{name} changed during validation.")
                continue
            audit = audit_doc.audit()
            findings = [{"code": int(e.code), "message": e.message} for e in (*audit.errors, *audit.fixes)]
            report["checks"][f"{name}_audit"] = {"status": "failed" if findings else "passed", "findings": findings, "repair_applied": False}
            if findings:
                issue("audit_failed", f"{name} DXF has audit errors or requires repairs; no file was repaired.")
        except (ezdxf.DXFError, ValueError, TypeError, RecursionError) as exc:
            issue("audit_failed", f"{name} audit could not complete: {exc}")
    if allowed_changes is None:
        allowed_changes = []
    if not isinstance(allowed_changes, (list, tuple)) or any(not isinstance(h, str) or not h for h in allowed_changes):
        issue("invalid_allowed_changes", "allowed_changes must be an explicit list of original entity handles.")
        allowed_changes = []
    allowed = {h.upper() for h in allowed_changes}
    before, after = _snapshots(original), _snapshots(current)
    changed = [h for h, value in before.items() if after.get(h) != value]
    for handle in changed:
        if handle not in allowed:
            issue("original_entity_changed", "An original native entity, attribute, style, or block definition changed or was removed.", [handle])
    report["checks"]["preservation"] = {"status": "failed" if any(h not in allowed for h in changed) else "passed", "original_count": len(before), "changed_handles": changed, "allowed_changed_handles": sorted(set(changed) & allowed), "added_handles": sorted(set(after) - set(before))}
    old_units = {k: _json(original.header.get(k)) for k in ("$INSUNITS", "$MEASUREMENT")}
    new_units = {k: _json(current.header.get(k)) for k in old_units}
    report["checks"]["units_preserved"] = "passed" if old_units == new_units else "failed"
    if old_units != new_units:
        issue("units_changed", "Original drawing unit declarations changed.", measured=new_units, required=old_units)
    if profile == "general":
        return report
    site = None
    try:
        site = _site(original, report["source_sha256"], facts)
    except FactsError as exc:
        issue(exc.code, str(exc), status=exc.status)
        report["checks"]["site_facts"] = exc.status
        report["checks"]["site_dependent_geometry"] = "skipped_unconfirmed_facts"
    else:
        report["checks"]["site_facts"] = "passed"
        report["checks"]["site_dependent_geometry"] = "evaluated"
    # Protect the referenced polygon AND all native dependencies of its INSERT.
    protected = [*site.boundary_handles, *(h for h, _ in site.obstacles)] if site is not None else []
    for handle in protected:
        try:
            geometry = entity_polygon(current, handle)
            baseline = entity_polygon(original, handle)
            if before.get(handle) != after.get(handle) or not geometry.equals_exact(baseline, 0):
                issue("site_reference_changed", "A confirmed boundary or obstacle changed in the target.", [handle])
        except ValueError as exc:
            issue("site_reference_changed", str(exc), [handle])
    discovered = []
    metadata = {}
    for entity in current.modelspace():
        try:
            item = _metadata(entity)
            if item is not None:
                metadata[entity.dxf.handle] = item
                discovered.append(item)
        except (ValueError, ezdxf.DXFError) as exc:
            issue("invalid_component_metadata", str(exc), [entity.dxf.handle], status="unknown")
    explicit_empty_registry = isinstance(components, list) and not components
    if components is None:
        components = discovered
    if not isinstance(components, list) or any(not isinstance(c, dict) for c in components):
        issue("invalid_components", "components must be a list of registered component objects.", status="unknown")
        return report
    occupied = []
    registered = set(protected)
    ids = set()
    spatial_registered = set()
    model_handles = {e.dxf.handle for e in current.modelspace()}
    for component in components:
        handle = str(component.get("handle", "")).upper()
        role = component.get("role")
        if role in {"annotation", "coverage"}:
            entity = current.entitydb.get(handle)
            if entity is not None and (role == "coverage" or entity.dxftype() in ANNOTATIONS):
                registered.add(handle)
            else:
                issue("invalid_annotation", "Annotation exemption requires an actual text entity.", [handle], status="unknown")
            continue
        if role not in {"facility", "road"}:
            issue("unknown_component_role", f"Unsupported component role {role!r}.", [handle], status="unknown")
            continue
        component_id = component.get("id")
        if not isinstance(component_id, str) or not component_id or component_id in ids or handle in registered:
            issue("invalid_component_identity", "Each spatial component requires a unique nonempty id and entity handle.", [handle], status="unknown")
            continue
        ids.add(component_id)
        spatial_registered.add(handle)
        registered.add(handle)
        entity = current.entitydb.get(handle)
        if entity is None or handle not in model_handles:
            if component.get("required", True):
                issue("missing_component", "A required registered component is missing from modelspace.", [handle])
            continue
        try:
            if role == "road":
                representation = component.get("kind", component.get("geometry_kind", component.get("representation")))
                if entity.dxftype() == "LWPOLYLINE" and representation != "corridor" and entity.dxf.get("const_width", 0) <= 0:
                    issue("road_zero_width", "The actual road has no positive constant DXF width, independently of its claimed width.", [handle], measured=entity.dxf.get("const_width", 0), measurement_units="drawing_units", required="positive constant width", **_entity_location(entity))
                polygon, width = _road_geometry(entity, component)
                if "width_m" in component and site is not None:
                    required = _number(component["width_m"], "required road width_m", positive=True)
                    measured = width * site.metres_per_unit
                    if measured + EPS < required:
                        issue("road_width_unmet", "Actual road width is below its requirement.", [handle], location=_location(polygon), measured=measured, required=required)
            else:
                polygon = _polygon(entity)
                if site is not None and ("width_m" in component or "depth_m" in component):
                    requirements = [_number(component[k], k, positive=True) for k in ("width_m", "depth_m") if k in component]
                    measured = [x * site.metres_per_unit for x in _rectangle_dimensions(polygon)]
                    if (len(requirements) == 2 and any(a + EPS < b for a, b in zip(measured, sorted(requirements)))) or (len(requirements) == 1 and max(measured) + EPS < requirements[0]):
                        issue("facility_dimensions_unmet", "Actual facility footprint does not meet its required dimensions.", [handle], location=_location(polygon), measured=measured, required=sorted(requirements))
        except (ValueError, ezdxf.DXFError) as exc:
            text = str(exc)
            known_failure = role == "road" and any(t in text for t in ("self-intersection", "positive", "zero-length", "two distinct")) or "non-self-intersecting polygon" in text
            issue("invalid_road" if role == "road" else "unsupported_component_geometry", text, [handle], status="failed" if known_failure else "unknown", **_entity_location(entity))
            continue
        occupied.append((handle, role, polygon))
        if site is not None:
            if not site.boundary.covers(polygon):
                issue("outside_boundary", "Complete occupied geometry extends outside the confirmed boundary.", [handle, *site.boundary_handles], location=_location(polygon.difference(site.boundary)))
            elif site.clearance_m and polygon.distance(site.boundary.boundary) * site.metres_per_unit + EPS < site.clearance_m:
                issue("boundary_clearance_unmet", "Occupied geometry is too close to the confirmed boundary.", [handle, *site.boundary_handles], location=_location(polygon, site.boundary.boundary), measured=polygon.distance(site.boundary.boundary) * site.metres_per_unit, required=site.clearance_m)
            for obstacle_handle, obstacle in site.obstacles:
                if polygon.intersection(obstacle).area > 0:
                    issue("obstacle_collision", "Occupied geometry overlaps a confirmed obstacle.", [handle, obstacle_handle], location=_location(polygon, obstacle), measured=polygon.intersection(obstacle).area * site.metres_per_unit ** 2, required=0)
                elif site.clearance_m and polygon.distance(obstacle) * site.metres_per_unit + EPS < site.clearance_m:
                    issue("obstacle_clearance_unmet", "Occupied geometry is too close to a confirmed obstacle.", [handle, obstacle_handle], location=_location(polygon, obstacle), measured=polygon.distance(obstacle) * site.metres_per_unit, required=site.clearance_m)
        label = component.get("label_handle")
        if label:
            label_entity = current.entitydb.get(str(label).upper())
            if label_entity is None or label_entity.dxftype() not in ANNOTATIONS:
                issue("missing_component_label", "Registered component label is absent or is not a text entity.", [handle, str(label)])
            else:
                registered.add(str(label).upper())
    for index, (handle, role, polygon) in enumerate(occupied):
        for other_handle, other_role, other in occupied[index + 1:]:
            if role == other_role == "road":
                continue  # Road junctions are not facility collisions.
            overlap = polygon.intersection(other)
            if overlap.area > 0:
                issue("component_collision", "Registered occupied geometries overlap.", [handle, other_handle], location=_location(overlap), measured=overlap.area * (site.metres_per_unit ** 2 if site is not None else 1), measurement_units="m2" if site is not None else "drawing_units2", required=0)
            elif site is not None and site.clearance_m and polygon.distance(other) * site.metres_per_unit + EPS < site.clearance_m:
                issue("component_clearance_unmet", "Registered occupied geometries violate minimum clearance.", [handle, other_handle], location=_location(polygon, other), measured=polygon.distance(other) * site.metres_per_unit, required=site.clearance_m)
    for entity in current.modelspace():
        handle = entity.dxf.handle
        declared = metadata.get(handle, {})
        if declared.get("role") in {"facility", "road"} and handle not in spatial_registered:
            issue("unregistered_spatial_component", "A current tagged facility or road was omitted from the explicit component registry; its occupancy remains unverified.", [handle], status="unknown")
            continue
        if handle in before or handle in registered or entity.dxftype() in ANNOTATIONS:
            continue
        if declared.get("role") == "coverage":
            continue
        issue("unregistered_spatial_entity", "A new spatial entity has no registered facility/road role or explicit coverage role; its occupancy is unverified.", [handle], status="unknown")
    if not ids and not explicit_empty_registry:
        issue("no_spatial_components", "No facility or road components were registered for site validation.", status="unknown")
    report["checks"]["site_geometry"] = {"status": report["status"], "registered_components": len(ids), "validated_occupied_components": len(occupied), "clearance_m": site.clearance_m if site is not None else None, "construction_norms": "not_tested", "road_to_road_junctions": "allowed"}
    return report
