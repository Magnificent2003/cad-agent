"""Independent read-back checks for the Part B concept-layout prototype.

Only measured DXF geometry is tested. Source reference polygons and research
assumptions are supplied explicitly; a pass is not construction-code approval,
proof of complete obstacle recognition, or a vehicle swept-path assessment.
"""
from __future__ import annotations

from collections import defaultdict
import math
from pathlib import Path

import ezdxf
from shapely import affinity
from shapely.errors import ShapelyError
from shapely.geometry import LineString, Point, mapping, shape
from shapely.ops import nearest_points, unary_union

from .geometry import ANNOTATIONS, _metadata, _snapshots, digest, occupied_polygon
from .spatial_extract import extract_drawing

LINEAR_TOLERANCE_M = 1e-5
AREA_TOLERANCE_M2 = 1e-5
ANGLE_TOLERANCE_DEG = 0.01


def _number(value, name, *, positive=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{name} must be {'positive and ' if positive else ''}finite")
    return result


def _local(geometry, scene):
    x, y = scene["origin_wcs"]
    result = affinity.translate(geometry, xoff=-x, yoff=-y)
    result = affinity.rotate(result, -scene["angle_deg"], origin=(0, 0))
    return affinity.scale(result, xfact=scene["metres_per_unit"],
                          yfact=scene["metres_per_unit"], origin=(0, 0))


def _valid_polygon(value):
    polygon = shape(value)
    if polygon.geom_type != "Polygon" or not polygon.is_valid or polygon.is_empty or polygon.area <= 0:
        raise ValueError("Scene geometry must be a valid positive-area Polygon")
    return polygon


def review_layout(source_dxf, target_dxf, scene: dict, program: dict) -> dict:
    """Reopen a delivered DXF and measure source preservation and scene rules."""
    report = {"schema_version": "site-plan-review/0.1", "status": "passed",
              "checks": {}, "issues": [], "warnings": [], "metrics": {}, "components": [],
              "scope": "Measured 2D concept-layout geometry under the supplied scene assumptions only",
              "limits": ["Construction regulations, firefighting and vehicle turning are not validated.",
                         "Unknown objects and construction phases are not resolved by this checker.",
                         "Remaining unallocated area is not certified buildable space.",
                         "Tower-crane coverage and lifting facilities are outside this round."],
              "tolerances": {"linear_m": LINEAR_TOLERANCE_M, "area_m2": AREA_TOLERANCE_M2,
                             "axis_degrees": ANGLE_TOLERANCE_DEG}}

    def fail(code, message, **details):
        report["status"] = "failed"
        report["issues"].append({"code": code, "message": message, **details})

    def checked(name, start, **details):
        report["checks"][name] = {"status": "passed" if len(report["issues"]) == start else "failed", **details}

    try:
        source_path, target_path = Path(source_dxf).resolve(), Path(target_dxf).resolve()
        if source_path == target_path:
            raise ValueError("The output must not overwrite the baseline")
        source_hash, target_hash = digest(source_path), digest(target_path)
        report.update(source_sha256=source_hash, target_sha256=target_hash)
        if source_hash != scene.get("source_sha256"):
            raise ValueError("Scene evidence belongs to different baseline bytes")
        scale = _number(scene["metres_per_unit"], "metres_per_unit", positive=True)
        _number(scene["angle_deg"], "angle_deg")
        if len(scene["origin_wcs"]) != 2:
            raise ValueError("origin_wcs must contain two coordinates")
        for value in scene["origin_wcs"]:
            _number(value, "origin_wcs")
        boundary = _local(_valid_polygon(scene["boundary_geometry"]), scene)
        obstacles = [(item["id"], _local(_valid_polygon(item["geometry"]), scene))
                     for item in scene["obstacles"]]
        if len({identity for identity, _ in obstacles}) != len(obstacles):
            raise ValueError("Scene obstacle instance IDs must be unique")
        planning = scene.get("planning", program.get("planning", {}))
        site_gap = _number(planning.get("site_edge_clearance_m", 0), "site_edge_clearance_m")
        obstacle_gap = _number(planning.get("obstacle_clearance_m", 0), "obstacle_clearance_m")
        frontage_limit = _number(planning["frontage_max_gap_m"], "frontage_max_gap_m", positive=True)
        if site_gap < 0 or obstacle_gap < 0:
            raise ValueError("Clearances cannot be negative")
        facility_specs = {item["id"]: item for item in program["facilities"]}
        if len(facility_specs) != len(program["facilities"]):
            raise ValueError("Program facility IDs must be unique")
        original, delivered = ezdxf.readfile(source_path), ezdxf.readfile(target_path)
        start = len(report["issues"])
        before, after = _snapshots(original), _snapshots(delivered)
        missing = [handle for handle in before if handle not in after]
        changed = [handle for handle in before if handle in after and before[handle] != after[handle]]
        if missing or changed:
            fail("baseline_entities_changed", "Original graphic entities and protected CAD tables must remain unchanged",
                 missing_handles=missing, changed_handles=changed)
        # This prototype explicitly declares metre drawing coordinates after the
        # reviewed unit conflict. The original geometry is never rescaled.
        target_units = int(delivered.header.get("$INSUNITS", 0))
        if target_units != 6 or abs(scale - 1.0) > 1e-8:
            fail("output_units_mismatch", "This metre-output prototype requires one native unit per metre and INSUNITS=6",
                 target_insunits=target_units, scene_metres_per_unit=scale)
        checked("baseline_preservation", start, baseline_snapshot_count=len(before),
                source_insunits=int(original.header.get("$INSUNITS", 0)), target_insunits=target_units,
                header_change_policy="explicit metre declaration; source coordinates and objects unchanged")

        start = len(report["issues"])
        wanted = {item["id"]: item["geometry"] for item in scene["obstacles"]}
        wanted[scene["boundary_id"]] = scene["boundary_geometry"]
        for label, path in (("source", source_path), ("target", target_path)):
            extracted = extract_drawing(path)
            records = {item["id"]: item for item in extracted["polygons"]}
            for identity, geometry in wanted.items():
                candidate = records.get(identity)
                if candidate is None:
                    fail("missing_reference_outline", "Referenced instance outline could not be reread", file=label, id=identity)
                    continue
                expected = _local(shape(geometry), scene)
                actual = _local(shape(candidate["geometry"]), scene)
                mismatch = expected.symmetric_difference(actual).area
                if mismatch > AREA_TOLERANCE_M2 or expected.hausdorff_distance(actual) > LINEAR_TOLERANCE_M:
                    fail("reference_outline_changed", "Actual instance geometry differs from adopted spatial facts",
                         file=label, id=identity, symmetric_difference_m2=mismatch)
        checked("spatial_references", start, obstacle_count=len(obstacles), boundary_id=scene["boundary_id"])

        start = len(report["issues"])
        baseline_model_handles = {entity.dxf.handle for entity in original.modelspace()}
        parsed = []
        component_ids = set()
        for entity in delivered.modelspace():
            if entity.dxf.handle in baseline_model_handles:
                continue
            handle = entity.dxf.handle
            try:
                tag = _metadata(entity)
                if tag is None:
                    raise ValueError("Every new model-space entity needs an explicit CAD_AGENT_SITE role")
                role = tag.get("role")
                if role == "annotation":
                    if entity.dxftype() not in ANNOTATIONS:
                        raise ValueError("An annotation role cannot exempt spatial geometry")
                    continue
                if role not in {"facility", "road"}:
                    raise ValueError("Only facility and road occupied geometry is supported this round")
                identity = tag.get("id")
                if not isinstance(identity, str) or not identity or identity in component_ids:
                    raise ValueError("Component IDs must be non-empty and unique")
                component_ids.add(identity)
                if entity.dxftype() != "LWPOLYLINE":
                    raise ValueError("Prototype components must be native LWPOLYLINEs")
                raw = occupied_polygon(delivered, handle, tag)
                actual = _local(raw, scene)
                item = {"id": identity, "handle": handle, "role": role,
                        "facility_id": tag.get("facility_id"), "name": tag.get("name", identity),
                        "area_m2": actual.area, "geometry": mapping(raw), "geometry_local_m": mapping(actual)}
                if role == "road":
                    vertices = list(entity.vertices_in_wcs())
                    if len(vertices) != 2 or entity.closed:
                        raise ValueError("Each prototype road segment must have two endpoints and be open")
                    centerline = _local(LineString([(p.x, p.y) for p in vertices]), scene)
                    (x1, y1), (x2, y2) = centerline.coords
                    angle = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 90
                    deviation = min(angle, 90 - angle)
                    width = float(entity.dxf.get("const_width", 0)) * scale
                    item.update(width_m=width, axis_deviation_deg=deviation,
                                centerline_local_m=mapping(centerline), length_m=centerline.length)
                    if width + LINEAR_TOLERANCE_M < program["road"]["width_m"]:
                        fail("road_width_unmet", "Measured road width is below the required width", id=identity,
                             measured_m=width, required_m=program["road"]["width_m"])
                    if deviation > ANGLE_TOLERANCE_DEG:
                        fail("road_axis_unmet", "Measured road is neither parallel nor perpendicular to the adopted axis",
                             id=identity, deviation_deg=deviation)
                else:
                    spec = facility_specs.get(item["facility_id"])
                    if spec is None or not spec.get("enabled", True):
                        raise ValueError("Facility type must identify an enabled program requirement")
                    rectangle = actual.minimum_rotated_rectangle
                    if len(actual.exterior.coords) != 5 or actual.symmetric_difference(rectangle).area > AREA_TOLERANCE_M2:
                        raise ValueError("Facility geometry must be a four-sided rectangle")
                    lengths = sorted(math.dist(a, b) for a, b in zip(rectangle.exterior.coords, list(rectangle.exterior.coords)[1:]))
                    item.update(measured_width_m=lengths[0], measured_length_m=lengths[-1],
                                clearance_m=float(spec.get("clearance_m", 0)))
                    expected_dimensions = sorted((_number(tag["width_m"], "width_m", positive=True),
                                                  _number(tag["depth_m"], "depth_m", positive=True)))
                    if max(abs(lengths[0] - expected_dimensions[0]), abs(lengths[-1] - expected_dimensions[1])) > LINEAR_TOLERANCE_M:
                        fail("facility_dimensions_mismatch", "Actual rectangle dimensions differ from registered dimensions", id=identity)
                    shape_rule = spec.get("shape", {}).get("aspect_ratio_range")
                    if shape_rule and not shape_rule[0] - 1e-6 <= lengths[-1] / lengths[0] <= shape_rule[1] + 1e-6:
                        fail("facility_aspect_ratio_unmet", "Actual rectangle aspect ratio is outside program bounds", id=identity)
                parsed.append((item, actual))
                report["components"].append(item)
            except (ValueError, TypeError, KeyError, OverflowError, ShapelyError) as exc:
                fail("invalid_component", str(exc), handle=handle)
        checked("actual_component_geometry", start, component_count=len(parsed))

        start = len(report["issues"])
        roads = [(item, poly) for item, poly in parsed if item["role"] == "road"]
        facilities = [(item, poly) for item, poly in parsed if item["role"] == "facility"]
        for item, polygon in parsed:
            identity = item["id"]
            outside = polygon.difference(boundary).area
            if outside > AREA_TOLERANCE_M2:
                fail("outside_boundary", "Actual full footprint extends outside the adopted boundary", id=identity, outside_area_m2=outside)
            if polygon.distance(boundary.boundary) + LINEAR_TOLERANCE_M < site_gap:
                fail("site_clearance_unmet", "Actual footprint is too close to the adopted boundary", id=identity,
                     measured_m=polygon.distance(boundary.boundary), required_m=site_gap)
            required_gap = max(obstacle_gap, item.get("clearance_m", 0))
            for obstacle_id, obstacle in obstacles:
                overlap = polygon.intersection(obstacle).area
                distance = polygon.distance(obstacle)
                if overlap > AREA_TOLERANCE_M2 or distance + LINEAR_TOLERANCE_M < required_gap:
                    fail("obstacle_conflict", "Actual footprint overlaps or is too close to a reserved outline", id=identity,
                         obstacle_id=obstacle_id, intersection_area_m2=overlap, distance_m=distance, required_m=required_gap)
        for index, (item, polygon) in enumerate(parsed):
            for other, other_poly in parsed[index + 1:]:
                if item["role"] == other["role"] == "road":
                    continue
                overlap = polygon.intersection(other_poly).area
                if overlap > AREA_TOLERANCE_M2:
                    fail("component_collision", "Actual occupied component footprints overlap", ids=[item["id"], other["id"]], intersection_area_m2=overlap)
                gap = max(item.get("clearance_m", 0), other.get("clearance_m", 0))
                distance = polygon.distance(other_poly)
                if distance + LINEAR_TOLERANCE_M < gap:
                    fail("facility_clearance_unmet", "Actual separation around a facility is below the required gap", ids=[item["id"], other["id"]], distance_m=distance, required_m=gap)
        checked("occupied_geometry_constraints", start, road_to_road_junctions="allowed")

        start = len(report["issues"])
        grouped = defaultdict(list)
        for item, polygon in facilities:
            grouped[item["facility_id"]].append((item, polygon))
        area_rows = []
        for identity, spec in facility_specs.items():
            target_area = float(spec["target_area_m2"])
            members = grouped[identity]
            union = unary_union([poly for _, poly in members])
            actual_area = union.area
            area_rows.append({"id": identity, "name": spec.get("name", identity), "target_area_m2": target_area,
                              "actual_area_m2": actual_area, "part_count": len(members), "deficit_m2": max(0, target_area - actual_area)})
            tolerance = max(AREA_TOLERANCE_M2, target_area * 1e-8)
            if actual_area + tolerance < target_area:
                fail("facility_area_unmet", "Union of actual facility parts does not meet the unchanged target", facility_id=identity,
                     actual_area_m2=actual_area, required_area_m2=target_area)
            split = spec.get("split", {})
            maximum = split.get("max_parts", 1) if split.get("allowed", False) else 1
            if len(members) > maximum:
                fail("facility_split_count_unmet", "Facility has too many separate parts", facility_id=identity, actual_parts=len(members), maximum=maximum)
            if len(members) > 1:
                minimum = split.get("min_part_area_m2", 0)
                for item, polygon in members:
                    if polygon.area + AREA_TOLERANCE_M2 < minimum:
                        fail("facility_part_too_small", "Split facility part is below its minimum area", id=item["id"], actual_area_m2=polygon.area, required_area_m2=minimum)
        checked("facility_program", start, facilities=area_rows)

        start = len(report["issues"])
        road_union = unary_union([polygon for _, polygon in roads])
        road_connected = not road_union.is_empty and road_union.geom_type == "Polygon"
        if not road_connected:
            fail("road_network_disconnected", "Actual road corridors must form one connected polygon", geometry_type=road_union.geom_type)
        access = scene.get("access", {})
        entry = access.get("entry_local_m")
        if entry is None:
            fail("entry_missing", "A selected entry is required to review road access")
        elif road_union.is_empty or road_union.distance(Point(entry)) > LINEAR_TOLERANCE_M:
            fail("entry_not_connected", "The selected entry is not on the actual road corridor", entry_local_m=entry,
                 distance_m=road_union.distance(Point(entry)) if not road_union.is_empty else None)
        frontage_rows = []
        for item, polygon in facilities:
            gap = polygon.distance(road_union) if not road_union.is_empty else None
            row = {"id": item["id"], "gap_m": gap, "straight_connector_clear": None}
            frontage_rows.append(row)
            if gap is None or gap > frontage_limit + LINEAR_TOLERANCE_M:
                fail("facility_frontage_unmet", "Actual facility is too far from the connected road network", id=item["id"], measured_gap_m=gap, maximum_gap_m=frontage_limit)
            elif gap > LINEAR_TOLERANCE_M:
                # Distance alone could see a nearby road through a building.
                # Check a direct geometric connection; this is deliberately not
                # a vehicle swept-path or a claimed legal loading entrance.
                ends = nearest_points(polygon, road_union)
                connector = LineString([point.coords[0] for point in ends])
                blocked_by = [key for key, obstacle in obstacles
                              if connector.intersection(obstacle).length > LINEAR_TOLERANCE_M]
                blocked_by.extend(other["id"] for other, other_polygon in facilities
                                  if other["id"] != item["id"] and
                                  connector.intersection(other_polygon).length > LINEAR_TOLERANCE_M)
                outside_length = connector.difference(boundary).length
                row.update(straight_connector_clear=not blocked_by and outside_length <= LINEAR_TOLERANCE_M,
                           connector_local_m=mapping(connector), blocked_by=blocked_by)
                if blocked_by or outside_length > LINEAR_TOLERANCE_M:
                    fail("frontage_connector_obstructed", "Shortest facility-to-road connection crosses a reserved footprint or leaves the site; another access route must be demonstrated", id=item["id"], blocked_by=blocked_by, outside_length_m=outside_length)
        checked("road_and_frontage", start, road_connected=road_connected, frontage=frontage_rows,
                entry_basis=access.get("entry_note"), scope="geometric adjacency; vehicle routes and loading access are not certified")

        occupied_union = unary_union([polygon for _, polygon in obstacles]).intersection(boundary)
        tiers = {item["id"]: item.get("constraint_tier", "retained") for item in scene["obstacles"]}
        retained_union = unary_union([polygon for identity, polygon in obstacles
                                      if tiers[identity] != "uncertain_keepout"]).intersection(boundary)
        uncertain_union = unary_union([polygon for identity, polygon in obstacles
                                       if tiers[identity] == "uncertain_keepout"]).intersection(boundary)
        # Reserve candidates remain unconfirmed; count only additional reserved
        # area outside the retained union to avoid inflating existing buildings.
        uncertain_increment = uncertain_union.difference(retained_union)
        facility_union = unary_union([polygon for _, polygon in facilities])
        used_union = unary_union([occupied_union, facility_union, road_union]).intersection(boundary)
        remaining = boundary.difference(used_union).area
        road_area = road_union.intersection(boundary).area
        reserve_target = float(program.get("reserve", {}).get("target_area_m2", 0))
        road_target = float(program["road"].get("target_area_m2", 0))
        report["metrics"] = {"site_area_m2": boundary.area, "obstacle_union_area_m2": occupied_union.area,
                             "retained_building_area_m2": retained_union.area,
                             "uncertain_reserved_area_m2": uncertain_increment.area,
                             "uncertain_raw_union_area_m2": uncertain_union.area,
                             "obstacle_tier_counts": {"retained": sum(tier != "uncertain_keepout" for tier in tiers.values()),
                                                      "uncertain_keepout": sum(tier == "uncertain_keepout" for tier in tiers.values())},
                             "facility_area_m2": facility_union.area, "facility_total_area_m2": facility_union.area,
                             "road_area_m2": road_area, "road_union_area_m2": road_area,
                             "road_fraction": road_area / boundary.area, "road_soft_area_m2": road_target,
                             "remaining_unallocated_area_m2": remaining, "remaining_area_m2": remaining,
                             "reserve_soft_area_m2": reserve_target, "facility_areas": area_rows,
                             "road_connected": road_connected, "component_count": len(parsed),
                             "maximum_frontage_gap_m": max((row["gap_m"] for row in frontage_rows if row["gap_m"] is not None), default=None)}
        if road_area > road_target + AREA_TOLERANCE_M2:
            report["warnings"].append({"code": "road_soft_budget_exceeded", "actual_area_m2": road_area, "reference_area_m2": road_target})
        if remaining + AREA_TOLERANCE_M2 < reserve_target:
            report["warnings"].append({"code": "reserve_soft_budget_unmet", "remaining_area_m2": remaining, "reference_area_m2": reserve_target})
        if digest(source_path) != source_hash or digest(target_path) != target_hash:
            fail("files_changed_during_review", "Input or output bytes changed while the independent review was running")
    except (OSError, ValueError, TypeError, KeyError, OverflowError, ezdxf.DXFError, ShapelyError) as exc:
        fail("review_input_error", str(exc))
    return report
