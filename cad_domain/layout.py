"""Finite, parameterized 2D site components in a confirmed local frame.

Dimensions and positions are metres; ``position_m`` is a rectangle's lower-left
corner before its own counter-clockwise rotation. Existing drawing coordinates
are never transformed. Automatic placement is a bounded candidate search, not
a feasibility proof or an engineering design rule.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any

import ezdxf
from ezdxf.enums import TextEntityAlignment
from shapely import affinity
from shapely.errors import ShapelyError
from shapely.geometry import LineString, Point, Polygon

from .geometry import digest, entity_polygon, load_site, occupied_polygon


APPID = "CAD_AGENT_SITE"
MAX_CANDIDATES = 250_000
SHORT_LABEL_LENGTH = 20


class LayoutError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise LayoutError("invalid_component", f"{name} must be a finite number")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise LayoutError("invalid_component", f"{name} must be a finite number") from None
    if not math.isfinite(result) or (positive and result <= 0):
        raise LayoutError("invalid_component", f"{name} must be {'positive and ' if positive else ''}finite")
    return result


def _point(value: Any, name: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise LayoutError("invalid_component", f"{name} must contain exactly two coordinates")
    return _number(value[0], name), _number(value[1], name)


def _text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise LayoutError("invalid_component", f"{name} must contain 1–{limit} characters")
    return " ".join(value.split())


def _read_tag(entity) -> dict | None:
    if not entity.has_xdata(APPID):
        return None
    try:
        tag = json.loads("".join(str(t.value) for t in entity.get_xdata(APPID) if t.code == 1000))
    except (TypeError, ValueError):
        raise LayoutError("invalid_metadata", f"Malformed {APPID} tag on {entity.dxf.handle}") from None
    if not isinstance(tag, dict):
        raise LayoutError("invalid_metadata", f"Invalid {APPID} tag on {entity.dxf.handle}")
    return tag


def _write_tag(entity, metadata: dict) -> None:
    # ASCII chunks stay below DXF's 255-byte limit for each XDATA string.
    raw = json.dumps(metadata, ensure_ascii=True, separators=(",", ":"))
    entity.set_xdata(APPID, [(1000, raw[i:i + 240]) for i in range(0, len(raw), 240)])


def _to_world(site, geometry):
    geometry = affinity.scale(geometry, xfact=1 / site.metres_per_unit,
                              yfact=1 / site.metres_per_unit, origin=(0, 0))
    geometry = affinity.rotate(geometry, site.angle_deg, origin=(0, 0))
    return affinity.translate(geometry, xoff=site.origin[0], yoff=site.origin[1])


def _to_local(site, geometry):
    geometry = affinity.translate(geometry, xoff=-site.origin[0], yoff=-site.origin[1])
    geometry = affinity.rotate(geometry, -site.angle_deg, origin=(0, 0))
    return affinity.scale(geometry, xfact=site.metres_per_unit,
                          yfact=site.metres_per_unit, origin=(0, 0))


def _current_components(doc, site) -> dict[str, dict]:
    result = {}
    for entity in doc.modelspace():
        tag = _read_tag(entity)
        if tag is None or tag.get("role") in {"annotation", "coverage"}:
            continue
        if tag.get("role") not in {"facility", "road"}:
            raise LayoutError("invalid_metadata", f"Unknown site component role on {entity.dxf.handle}")
        component_id = _text(tag.get("id"), "component id", 64)
        if component_id in result:
            raise LayoutError("duplicate_component", f"Duplicate existing component id {component_id}")
        if tag.get("handle", entity.dxf.handle) != entity.dxf.handle:
            raise LayoutError("invalid_metadata", f"Component {component_id} has a stale handle")
        tag = dict(tag, handle=entity.dxf.handle)
        polygon = occupied_polygon(doc, entity.dxf.handle, tag)
        if tag["role"] == "facility":
            width = _number(tag.get("width_m"), "width_m", positive=True)
            depth = _number(tag.get("depth_m"), "depth_m", positive=True)
            coords = list(_to_local(site, polygon).exterior.coords)
            if len(coords) != 5:
                raise LayoutError("invalid_component", f"Existing component {component_id} is not a rectangle")
            actual = [math.dist(coords[i], coords[i + 1]) for i in range(4)]
            if any(not math.isclose(a, b, rel_tol=1e-7, abs_tol=1e-7)
                   for a, b in zip(actual, [width, depth, width, depth])):
                raise LayoutError("dimension_mismatch", f"Existing component {component_id} dimensions differ from its tag")
            diagonal = math.dist(coords[0], coords[2])
            if not math.isclose(diagonal, math.hypot(width, depth), rel_tol=1e-7, abs_tol=1e-7):
                raise LayoutError("invalid_component", f"Existing component {component_id} is not rectangular")
            position = list(coords[0])
            angle = math.degrees(math.atan2(coords[1][1] - coords[0][1], coords[1][0] - coords[0][0]))
            spec = dict(tag, kind="rectangle", position_m=position, angle_deg=angle)
        else:
            width = _number(tag.get("width_m"), "width_m", positive=True)
            if entity.dxftype() != "LWPOLYLINE" or entity.closed:
                raise LayoutError("invalid_road", f"Existing road {component_id} must be an open LWPOLYLINE")
            if not math.isclose(float(entity.dxf.const_width) * site.metres_per_unit,
                                width, rel_tol=1e-7, abs_tol=1e-7):
                raise LayoutError("dimension_mismatch", f"Existing road {component_id} width differs from its tag")
            points = list(_to_local(site, LineString(list(entity.get_points("xy")))).coords)
            spec = dict(tag, kind="road", points_m=points)
        result[component_id] = {"spec": spec, "metadata": tag, "polygon": polygon,
                                "entity": entity, "new": False}
    return result


def _collision(polygon, boundary, occupied: list[tuple[str, Any]], clearance: float) -> str | None:
    if not boundary.covers(polygon) or (clearance > 0 and polygon.distance(boundary.boundary) < clearance):
        return "outside the confirmed boundary or its required clearance"
    for name, other in occupied:
        if polygon.intersection(other).area > 0 or (clearance > 0 and polygon.distance(other) < clearance):
            return f"conflicts with {name}"
    return None


def _rectangle(spec: dict, position: tuple[float, float]) -> Polygon:
    # Explicit order keeps width as the first edge on every persisted component.
    polygon = Polygon([(0, 0), (spec["width_m"], 0),
                       (spec["width_m"], spec["depth_m"]), (0, spec["depth_m"])])
    polygon = affinity.rotate(polygon, spec["angle_deg"], origin=(0, 0))
    return affinity.translate(polygon, xoff=position[0], yoff=position[1])


def _place_rectangle(spec, site, boundary, occupied, clearance, facts):
    if spec.get("position_m") is not None:
        position = _point(spec["position_m"], "position_m")
        polygon = _to_world(site, _rectangle(spec, position))
        conflict = _collision(polygon, boundary, occupied, clearance)
        if conflict:
            raise LayoutError("placement_conflict", f"Component {spec['id']} {conflict}")
        return polygon, position, None

    options = facts.get("search", {})
    if not isinstance(options, dict):
        raise LayoutError("invalid_search", "facts.search must be an object")
    step = _number(options.get("step_m", min(1.0, spec["width_m"] / 2, spec["depth_m"] / 2)),
                   "search.step_m", positive=True)
    limit = options.get("max_candidates", 25_000)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_CANDIDATES:
        raise LayoutError("invalid_search", f"search.max_candidates must be an integer from 1 to {MAX_CANDIDATES}")
    search_boundary = boundary.buffer(-clearance) if clearance else boundary
    local_bounds = _to_local(site, search_boundary).bounds
    if len(local_bounds) != 4 or not all(math.isfinite(value) for value in local_bounds):
        raise LayoutError("no_placement", "Boundary clearance leaves no usable area")
    shape_bounds = _rectangle(spec, (0, 0)).bounds
    minx, miny = local_bounds[0] - shape_bounds[0], local_bounds[1] - shape_bounds[1]
    maxx, maxy = local_bounds[2] - shape_bounds[2], local_bounds[3] - shape_bounds[3]
    attempts = 0
    if maxx >= minx and maxy >= miny:
        # Include the final edge position even when the grid does not divide the extent.
        def count(extent):
            intervals = extent / step
            return limit + 1 if not math.isfinite(intervals) else min(limit + 1, math.ceil(intervals) + 1)

        nx, ny = count(maxx - minx), count(maxy - miny)
        for iy in range(ny):
            y = min(miny + iy * step, maxy)
            for ix in range(nx):
                if attempts >= limit:
                    raise LayoutError("candidate_limit", f"Component {spec['id']}: search stopped at {limit} candidates (step {step:g} m)")
                x = min(minx + ix * step, maxx)
                attempts += 1
                polygon = _to_world(site, _rectangle(spec, (x, y)))
                if _collision(polygon, boundary, occupied, clearance) is None:
                    return polygon, (x, y), {"attempts": attempts, "step_m": step, "max_candidates": limit}
    raise LayoutError("no_placement", f"Component {spec['id']}: no valid placement in {attempts} bounded candidates (step {step:g} m); dimensions unchanged")


def _prepare(spec: dict, existing: dict | None) -> dict:
    merged = dict(existing["spec"] if existing else {}, **spec)
    merged["id"] = _text(merged.get("id"), "component id", 64)
    merged["name"] = _text(merged.get("name", merged["id"]), "component name", 160)
    kind = merged.get("kind", "rectangle")
    if kind not in {"rectangle", "road"}:
        raise LayoutError("invalid_component", f"Unsupported component kind {kind!r}")
    if existing and kind != existing["spec"]["kind"]:
        raise LayoutError("invalid_operation", "A move cannot change a component's kind")
    merged["kind"] = kind
    merged["width_m"] = _number(merged.get("width_m"), "width_m", positive=True)
    if kind == "rectangle":
        merged["depth_m"] = _number(merged.get("depth_m"), "depth_m", positive=True)
        merged["angle_deg"] = _number(merged.get("angle_deg", 0), "angle_deg")
    return merged


def _annotation(doc, text, position, height, rotation, component_id):
    entity = doc.modelspace().add_text(text, dxfattribs={"height": height, "rotation": rotation,
                                                        "layer": "CAD_AGENT_SITE_LABELS"})
    entity.set_placement(position, align=TextEntityAlignment.MIDDLE_CENTER)
    _write_tag(entity, {"role": "annotation", "component_id": component_id})
    return entity


def _draw(doc, site, item, legend_index):
    spec, polygon = item["spec"], item["polygon"]
    model = doc.modelspace()
    if spec["kind"] == "rectangle":
        entity = model.add_lwpolyline(list(polygon.exterior.coords)[:-1], close=True,
                                      dxfattribs={"layer": "CAD_AGENT_SITE_COMPONENTS"})
        rotation = site.angle_deg + spec["angle_deg"]
        label_position = tuple(polygon.centroid.coords)[0]
        available_width, available_height = spec["width_m"], spec["depth_m"]
    else:
        line = _to_world(site, LineString(spec["points_m"]))
        entity = model.add_lwpolyline(list(line.coords), dxfattribs={
            "layer": "CAD_AGENT_SITE_COMPONENTS", "const_width": spec["width_m"] / site.metres_per_unit, "color": 8})
        rotation = site.angle_deg
        label_position = tuple(line.interpolate(0.5, normalized=True).coords)[0]
        available_width, available_height = line.length * site.metres_per_unit, spec["width_m"]
    label = spec["name"] if len(spec["name"]) <= SHORT_LABEL_LENGTH else spec["id"]
    if len(label) > SHORT_LABEL_LENGTH:
        label = f"C{legend_index + 1:03d}"
    height = min(0.35, available_height / 4, available_width * 0.8 / max(1, len(label)) / 0.7)
    label_entity = _annotation(doc, label, label_position, height / site.metres_per_unit,
                               rotation, spec["id"])
    metadata = {"id": spec["id"], "name": spec["name"], "role": "facility" if spec["kind"] == "rectangle" else "road",
                "handle": entity.dxf.handle, "label_handle": label_entity.dxf.handle,
                "width_m": spec["width_m"]}
    if spec["kind"] == "rectangle":
        metadata["depth_m"] = spec["depth_m"]
    if len(spec["name"]) > SHORT_LABEL_LENGTH:
        bounds = _to_local(site, site.boundary).bounds
        entry = f"{label}: {spec['name']}"
        lines = [entry[i:i + 48] for i in range(0, len(entry), 48)]
        handles = []
        for index, line in enumerate(lines):
            position = _to_world(site, Point(bounds[2] + 8,
                                             bounds[3] - legend_index * 2.5 - index * 0.45)).coords[0]
            note = _annotation(doc, line, position, 0.3 / site.metres_per_unit,
                               site.angle_deg, spec["id"])
            handles.append(note.dxf.handle)
        metadata["legend_handles"] = handles
    _write_tag(entity, metadata)
    item["metadata"] = metadata


def _remove(doc, item, allowed):
    metadata = item["metadata"]
    handles = [metadata["handle"], metadata.get("label_handle"), *metadata.get("legend_handles", [])]
    for handle in handles:
        if not handle:
            continue
        entity = doc.entitydb.get(handle)
        if entity is None or not entity.is_alive:
            raise LayoutError("invalid_metadata", f"Component {metadata['id']} references missing entity {handle}")
        if handle != metadata["handle"]:
            tag = _read_tag(entity)
            if entity.dxftype() not in {"TEXT", "MTEXT"} or not tag or tag.get("role") != "annotation" or tag.get("component_id") != metadata["id"]:
                raise LayoutError("invalid_metadata", f"Component {metadata['id']} references an unrelated label {handle}")
        doc.modelspace().delete_entity(entity)
        allowed.append(handle)


def build_layout(source, target, *, facts: dict, components: list[dict], baseline=None) -> dict:
    """Add/move/remove explicit components, publishing only a wholly valid layout.

    ``baseline`` binds confirmed facts while ``source`` can be a prior task
    output. Current tagged geometry is read anew on every call. A finite search
    reports failure when exhausted; it never shrinks parts or relaxes clearance.
    The delivery gate remains responsible for full baseline preservation checks.
    """
    report = {"status": "failed", "components": [], "allowed_changes": [], "issues": []}
    temporary = None
    try:
        source, target = Path(source).resolve(), Path(target).resolve()
        baseline = Path(baseline or source).resolve()
        if target in {source, baseline}:
            raise LayoutError("source_overwrite", "Target must differ from source and confirmed baseline")
        if not isinstance(facts, dict) or not isinstance(components, list):
            raise LayoutError("invalid_request", "facts must be an object and components must be a list")
        site = load_site(baseline, facts)
        report.update(source_sha256=digest(source), baseline_sha256=site.source_sha256, facts=facts)
        doc = ezdxf.readfile(source)
        # A revised source must still contain the original confirmed geometry.
        original = ezdxf.readfile(baseline)
        if doc.header.get("$INSUNITS", 0) != original.header.get("$INSUNITS", 0):
            raise LayoutError("units_changed", "Current source changed the confirmed baseline INSUNITS")
        protected = [*facts["boundary"]["handles"], *facts["obstacles"]["handles"]]
        for handle in protected:
            if not entity_polygon(doc, handle).equals_exact(entity_polygon(original, handle), 0):
                raise LayoutError("protected_geometry_changed", f"Confirmed site entity {handle} changed in the current source")
        current = _current_components(doc, site)
        clearance_m = _number(facts.get("clearance_m", 0), "clearance_m")
        if clearance_m < 0:
            raise LayoutError("invalid_clearance", "clearance_m cannot be negative")
        clearance = clearance_m / site.metres_per_unit
        boundary = site.boundary
        if clearance and boundary.buffer(-clearance).is_empty:
            raise LayoutError("no_placement", "Boundary clearance leaves no usable area")
        requested = set()
        pending = []
        for spec in components:
            if not isinstance(spec, dict):
                raise LayoutError("invalid_component", "Each component must be an object")
            component_id = _text(spec.get("id"), "component id", 64)
            if component_id in requested:
                raise LayoutError("duplicate_component", f"Component {component_id} is requested more than once")
            requested.add(component_id)
            operation = spec.get("operation", "add")
            if operation not in {"add", "move", "remove"}:
                raise LayoutError("invalid_operation", f"Unsupported operation {operation!r}")
            existing = current.get(component_id)
            if operation == "add" and existing:
                raise LayoutError("duplicate_component", f"Component {component_id} already exists")
            if operation != "add" and existing is None:
                raise LayoutError("missing_component", f"Component {component_id} does not exist")
            if spec.get("handle") and (not existing or spec["handle"] != existing["metadata"]["handle"]):
                raise LayoutError("stale_handle", f"Component {component_id} does not match the supplied handle")
            if existing:
                if existing["metadata"]["handle"] in protected:
                    raise LayoutError("protected_geometry_changed", f"Confirmed site entity {existing['metadata']['handle']} cannot be moved or removed")
                _remove(doc, existing, report["allowed_changes"])
                del current[component_id]
            if operation != "remove":
                pending.append(_prepare(spec, existing))

        occupied = [(f"obstacle {handle}", polygon) for handle, polygon in site.obstacles]
        for component_id, item in current.items():
            conflict = _collision(item["polygon"], boundary, occupied, clearance)
            if conflict:
                raise LayoutError("existing_component_conflict", f"Existing component {component_id} {conflict}")
            occupied.append((f"component {component_id}", item["polygon"]))

        for spec in pending:
            if spec["kind"] == "rectangle":
                polygon, position, search = _place_rectangle(spec, site, boundary, occupied, clearance, facts)
                spec["position_m"] = list(position)
            else:
                raw_points = spec.get("points_m")
                if not isinstance(raw_points, (list, tuple)) or len(raw_points) < 2:
                    raise LayoutError("invalid_road", "Road points_m must contain at least two points")
                points = [_point(point, "points_m") for point in raw_points]
                line = LineString(points)
                if not line.is_simple or line.length <= 0 or any(a == b for a, b in zip(points, points[1:])):
                    raise LayoutError("invalid_road", "Road centerline must be simple, nonzero, and have no repeated adjacent points")
                spec["points_m"] = points
                polygon = _to_world(site, line.buffer(spec["width_m"] / 2, cap_style=2, join_style=2))
                if polygon.is_empty or not polygon.is_valid or polygon.geom_type != "Polygon":
                    raise LayoutError("invalid_road", "Road width does not produce one valid corridor")
                conflict = _collision(polygon, boundary, occupied, clearance)
                if conflict:
                    raise LayoutError("placement_conflict", f"Road {spec['id']} {conflict}")
                search = None
            current[spec["id"]] = {"spec": spec, "polygon": polygon, "new": True, "search": search}
            occupied.append((f"component {spec['id']}", polygon))

        if APPID not in doc.appids:
            doc.appids.new(APPID)
        for name in ("CAD_AGENT_SITE_COMPONENTS", "CAD_AGENT_SITE_LABELS"):
            if name not in doc.layers:
                doc.layers.new(name)
        legend_index = sum(bool(item.get("metadata", {}).get("legend_handles")) for item in current.values())
        for item in current.values():
            if item["new"]:
                _draw(doc, site, item, legend_index)
                legend_index += 1
        manifest = [dict(item["metadata"]) for item in current.values()]
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".dxf", dir=target.parent)
        os.close(descriptor)
        doc.saveas(temporary)
        # Read the actual serialized geometry before publication.
        saved = ezdxf.readfile(temporary)
        for item in current.values():
            actual = occupied_polygon(saved, item["metadata"]["handle"], item["metadata"])
            if not actual.equals_exact(item["polygon"], 1e-7 / site.metres_per_unit):
                # Road buffers can have a different start vertex/order.
                if actual.symmetric_difference(item["polygon"]).area > 1e-10 / site.metres_per_unit**2:
                    raise LayoutError("serialization_mismatch", f"Saved geometry changed for {item['spec']['id']}")
        target_hash = digest(temporary)
        os.replace(temporary, target)
        temporary = None
        report.update(status="passed", path=str(target), target_sha256=target_hash, components=manifest)
        searches = {key: item["search"] for key, item in current.items() if item.get("search")}
        if searches:
            report["search"] = searches
    except (ValueError, TypeError, OverflowError, OSError, ezdxf.DXFError, ShapelyError) as exc:
        report["status"] = getattr(exc, "status", "failed")
        report["issues"] = [{"code": getattr(exc, "code", "layout_error"), "message": str(exc)}]
        report["allowed_changes"] = []
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return report
