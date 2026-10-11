"""Evidence-bound concept layout: configurable area program and finite candidates.

No project coordinates or entity handles are embedded in placement rules. This
is a reusable geometric candidate generator; the agent may select the program,
preferences and alternative attempts. It is not a construction-code approval.
"""
from __future__ import annotations

import argparse
from collections import deque, defaultdict
import json
import math
import re
from pathlib import Path
import time

import ezdxf
from ezdxf.enums import TextEntityAlignment
from shapely import affinity
from shapely.geometry import LineString, Point, box, shape
from shapely.ops import unary_union
from shapely.prepared import prep

from .geometry import digest
from .layout import _write_tag
from .site_program import build_program, load_rules
from .spatial_extract import extract_drawing
from .spatial_facts import construct_facts
from .spatial_refinement import refine_spatial_facts


class PlanningFailure(ValueError):
    pass


def to_local(geometry, scene):
    p = affinity.translate(geometry, -scene["origin_wcs"][0], -scene["origin_wcs"][1])
    return affinity.scale(affinity.rotate(p, -scene["angle_deg"], origin=(0, 0)),
                          scene["metres_per_unit"], scene["metres_per_unit"], origin=(0, 0))


def to_world(geometry, scene):
    p = affinity.scale(geometry, 1 / scene["metres_per_unit"], 1 / scene["metres_per_unit"], origin=(0, 0))
    return affinity.translate(affinity.rotate(p, scene["angle_deg"], origin=(0, 0)), *scene["origin_wcs"])


def adopt_scene(facts, refined, rules):
    """Use the user's delegated judgment, without pretending it was manual approval."""
    if facts["source"]["sha256"] != refined["source"]["sha256"]:
        raise PlanningFailure("Spatial facts and classifications refer to different source versions")
    boundaries = [f for f in facts["features"] if f["properties"]["kind"] == "boundary_candidate"]
    if len(boundaries) != 1:
        raise PlanningFailure("This candidate generator requires one evidenced working boundary")
    candidate = refined["units"].get("candidate_metres_per_unit")
    if candidate is None or not math.isfinite(candidate) or candidate <= 0:
        raise PlanningFailure("No finite, evidence-supported scale is available for area estimation")
    scale = round(candidate, 9)
    if scale <= 0:
        scale = candidate
    frame = refined["axis_frame"]
    if frame.get("axis_deg") is None:
        raise PlanningFailure("No consistent reference frame is available")
    obstacles = [{"id": item["id"], "name": item["label"], "geometry": item["geometry"],
                  "human_confirmed": item["human_confirmed"], "confidence": item["confidence"],
                  "constraint_tier": "retained", "service_target": True}
                 for item in refined["classifications"] if item["category"] == "retained"]
    # Classification uncertainty does not make a region empty. Keep unresolved
    # major outlines out of this concept layout without relabelling them buildings.
    keepouts = []
    if rules["planning"]["uncertain_keepouts"] == "major_non_annotation":
        exclusions = [re.compile(pattern, re.IGNORECASE) for pattern in
                      rules["planning"]["uncertain_excluded_layer_patterns"]]
        for item in refined["classifications"]:
            if item["category"] != "uncertain" or item["display_priority"] != "major":
                continue
            if any(pattern.search(item["layer"]) for pattern in exclusions):
                continue
            keepouts.append({"id": item["id"], "name": "待定避让区", "geometry": item["geometry"],
                             "human_confirmed": False, "confidence": item["confidence"],
                             "constraint_tier": "uncertain_keepout", "service_target": False,
                             "source_layer": item["layer"], "reason_codes": item["reason_codes"]})
    return {"source_sha256": facts["source"]["sha256"], "metres_per_unit": scale,
            "origin_wcs": frame["local_frame"]["origin_wcs"], "angle_deg": frame["axis_deg"],
            "boundary_id": boundaries[0]["id"], "boundary_geometry": boundaries[0]["geometry"],
            "obstacles": obstacles + keepouts, "planning": rules["planning"],
            "decisions": [
                {"item": "units", "value": scale, "source": "building-area consensus and user-delegated judgment",
                 "source_header_code": facts["source"]["units_code"], "header_conflict": refined["units"]["status"] == "conflict",
                 "note": "Working scale adopted; original file untouched. Output unit declaration may be corrected without rescaling coordinates."},
                {"item": "working_boundary", "value": boundaries[0]["id"], "source": "single candidate and user-delegated judgment",
                 "note": "Research working boundary, not legal boundary certification"},
                {"item": "obstacles", "count": len(obstacles), "source": "reviewed outlines plus retained candidates per user instruction"},
                {"item": "uncertain_keepouts", "count": len(keepouts), "source": rules["planning"]["uncertain_keepouts"],
                 "note": "Temporarily protected major uncertain outlines; not confirmed buildings or extra road service targets."},
                {"item": "other_objects", "source": "research_scope",
                 "note": "Parking symbols are not solid building obstacles; other unresolved objects will be reported separately, not certified as free space."}],
            "engineering_compliance": False}


class OrthogonalNetwork:
    def __init__(self, boundary, obstacles, width, options):
        self.step = options["road_grid_step_m"]
        self.width = width
        self.clearance = options["obstacle_clearance_m"]
        safe = boundary.buffer(-width / 2 - options["site_edge_clearance_m"])
        if obstacles:
            safe = safe.difference(unary_union([p.buffer(width / 2 + self.clearance) for p in obstacles]))
        if safe.is_empty:
            raise PlanningFailure("No road-center corridor remains at the selected width and clearance")
        self.safe = prep(safe)
        self.nodes = {}
        minx, miny, maxx, maxy = safe.bounds
        for ix in range(math.ceil(minx / self.step), math.floor(maxx / self.step) + 1):
            for iy in range(math.ceil(miny / self.step), math.floor(maxy / self.step) + 1):
                point = (ix * self.step, iy * self.step)
                if self.safe.covers(Point(point)):
                    self.nodes[(ix, iy)] = point
        if not self.nodes:
            raise PlanningFailure("Road grid has no feasible nodes; change search resolution or program")
        self._neighbors = {}
        self.edges = set()
        self.network_nodes = set()

    def neighbors(self, node):
        if node not in self._neighbors:
            x, y = node
            self._neighbors[node] = tuple(other for other in ((x-1, y), (x+1, y), (x, y-1), (x, y+1))
                if other in self.nodes and self.safe.covers(LineString([self.nodes[node], self.nodes[other]])))
        return self._neighbors[node]

    def paths_from_network(self):
        previous = {key: None for key in sorted(self.network_nodes)}
        distance = dict.fromkeys(previous, 0)
        queue = deque(previous)
        while queue:
            current = queue.popleft()
            for nxt in self.neighbors(current):
                if nxt not in previous:
                    previous[nxt] = current
                    distance[nxt] = distance[current] + 1
                    queue.append(nxt)
        return previous, distance

    def connect(self, polygon):
        previous, distances = self.paths_from_network()
        closest = min(Point(self.nodes[key]).distance(polygon) for key in previous)
        targets = [key for key in previous if Point(self.nodes[key]).distance(polygon) <= closest + self.step * .2]
        target = min(targets, key=lambda key: (distances[key], Point(self.nodes[key]).distance(polygon), key))
        cursor = target
        while previous[cursor] is not None:
            other = previous[cursor]
            self.edges.add(tuple(sorted([cursor, other])))
            self.network_nodes.update((cursor, other))
            cursor = other
        return {"node": list(self.nodes[target]), "distance_to_footprint_m": Point(self.nodes[target]).distance(polygon),
                "new_grid_edges": distances[target]}

    def segments(self):
        groups = defaultdict(list)
        for a, b in self.edges:
            if a[0] == b[0]:
                groups[("v", a[0])].append((min(a[1], b[1]), max(a[1], b[1])))
            else:
                groups[("h", a[1])].append((min(a[0], b[0]), max(a[0], b[0])))
        result = []
        for (axis, fixed), intervals in sorted(groups.items()):
            intervals.sort()
            start, end = intervals[0]
            combined = []
            for left, right in intervals[1:]:
                if left <= end:
                    end = max(end, right)
                else:
                    combined.append((start, end))
                    start, end = left, right
            combined.append((start, end))
            for left, right in combined:
                points = [(left*self.step, fixed*self.step), (right*self.step, fixed*self.step)] if axis == "h" else [
                    (fixed*self.step, left*self.step), (fixed*self.step, right*self.step)]
                result.append(LineString(points))
        return result


def build_roads(boundary, obstacles, scene, refined, program):
    network = OrthogonalNetwork(boundary, list(obstacles.values()), program["road"]["width_m"], program["planning"])
    support = refined["axis_frame"]["boundary_support"]
    if not support:
        raise PlanningFailure("No supported boundary edge for an auditable access-point proposal")
    edge = max(support, key=lambda e: (e["length_native"], e["id"]))
    midpoint = to_local(Point([(edge["start"][i] + edge["end"][i]) / 2 for i in (0, 1)]), scene)
    entry = min(network.nodes, key=lambda key: (Point(network.nodes[key]).distance(midpoint), key))
    network.network_nodes.add(entry)
    connections = []
    service_ids = {item["id"] for item in scene["obstacles"] if item["service_target"]}
    for key, polygon in sorted(obstacles.items(), key=lambda item: (-item[1].area, item[0])):
        if key not in service_ids:
            continue
        connections.append({"obstacle_id": key, **network.connect(polygon)})
    lines = network.segments()
    if not lines:
        raise PlanningFailure("No road network was produced")
    scene["access"] = {"entry_local_m": list(network.nodes[entry]), "reference_edge_id": edge["id"],
                       "boundary_distance_m": Point(network.nodes[entry]).distance(boundary.boundary),
                       "entry_note": "Autonomously proposed access point inside working boundary; external gate/road connection not verified",
                       "building_connections": connections}
    return lines


def _grid_positions(low, high, step):
    if high < low:
        return []
    values = [low + 1e-7, high - 1e-7]
    values += [i * step for i in range(math.ceil(low / step), math.floor(high / step) + 1)]
    return sorted(set(x for x in values if low <= x <= high))


def _candidate_rectangles(area, spec, free, roads, preference, options, remaining_budget):
    if free.is_empty:
        return [], 0
    prepared = prep(free)
    low, high = spec["shape"]["aspect_ratio_range"]
    count = options["aspect_ratio_samples"]
    ratios = [low + (high-low)*i/max(1, count-1) for i in range(count)]
    ratios = sorted(set([spec["shape"]["preferred_aspect_ratio"], *ratios]))
    minx, miny, maxx, maxy = free.bounds
    tried, best = 0, []
    for ratio in ratios:
        width, depth = math.sqrt(area*ratio), math.sqrt(area/ratio)
        for width, depth in sorted({(width, depth), (depth, width)}):
            xs = _grid_positions(minx, maxx-width, options["grid_step_m"])
            ys = _grid_positions(miny, maxy-depth, options["grid_step_m"])
            # Visit nearer preferred workfaces first; complete feasible scoring
            # is still bounded by the explicit candidate budget.
            xs.sort(key=lambda x: abs(x+width/2-preference.x))
            ys.sort(key=lambda y: abs(y+depth/2-preference.y))
            for x in xs:
                for y in ys:
                    tried += 1
                    if tried > remaining_budget:
                        return best, tried-1
                    polygon = box(x, y, x+width, y+depth)
                    if not prepared.covers(polygon):
                        continue
                    road_gap = polygon.distance(roads)
                    # Geometric convenience only; the program can override
                    # preferences, dimensions and candidate search parameters.
                    score = polygon.centroid.distance(preference) + road_gap * 4
                    candidate = (score, x, y, width, depth, polygon)
                    if len(best) < options["access_candidate_limit"] or candidate[:5] < best[-1][:5]:
                        best.append(candidate)
                        best.sort(key=lambda c: c[:5])
                        del best[options["access_candidate_limit"]:]
    return best, tried


def extend_access(boundary, obstacles, reserved, polygon, roads, program):
    """Grow connected roads into unused land as facilities are proposed."""
    options = program["planning"]
    width = program["road"]["width_m"]
    network = OrthogonalNetwork(boundary, [*obstacles.values(), *reserved, polygon], width, options)
    existing = prep(roads.buffer(1e-6))
    network.network_nodes = {key for key, point in network.nodes.items() if existing.covers(Point(point))}
    if not network.network_nodes:
        return None
    connection = network.connect(polygon)
    if connection["distance_to_footprint_m"] - width / 2 > options["frontage_max_gap_m"]:
        return None
    lines = network.segments()
    extended = unary_union([roads, *[line.buffer(width/2, cap_style=2) for line in lines]])
    # Endpoint caps can make center-distance minus half-width optimistic.
    # Use the actual proposed road surface, as the independent DXF review does.
    if polygon.distance(extended) > options["frontage_max_gap_m"]:
        return None
    return lines


def place_facilities(boundary, obstacles, roads, scene, program):
    options = program["planning"]
    entry = Point(scene["access"]["entry_local_m"])
    workfaces = unary_union([obstacles[item["id"]] for item in scene["obstacles"] if item["service_target"]]).centroid
    active = [f for f in program["facilities"] if f["enabled"]]
    ordering = [sorted(active, key=lambda s: (-s["target_area_m2"], s["id"])),
                sorted(active, key=lambda s: (s["preferences"]["access"] == "entry", -s["target_area_m2"], s["id"])),
                sorted(active, key=lambda s: (-s["target_area_m2"], s["id"]), reverse=True)]
    attempts = []
    for attempt_no, specs in enumerate(ordering[:options["placement_order_trials"]], 1):
        placed = []
        current_roads = roads
        added_lines = []
        attempt = {"attempt": attempt_no, "order": [s["id"] for s in specs], "facilities": []}
        for spec in specs:
            gap = max(spec["clearance_m"], options["obstacle_clearance_m"])
            preference = entry if spec["preferences"]["access"] == "entry" else workfaces
            max_parts = spec["split"]["max_parts"] if spec["split"]["allowed"] else 1
            success = None
            part_trials = []
            for parts in range(1, max_parts+1):
                area = spec["target_area_m2"] / parts
                if parts > 1 and area < spec["split"]["min_part_area_m2"]:
                    break
                pending = []
                trial_roads = current_roads
                trial_lines = []
                tried = 0
                for index in range(parts):
                    blocked = [p.buffer(gap) for p in obstacles.values()] + [trial_roads.buffer(spec["clearance_m"])]
                    blocked += [p["polygon"].buffer(max(spec["clearance_m"], p["clearance_m"])) for p in [*placed, *pending]]
                    free = boundary.buffer(-options["site_edge_clearance_m"]).difference(unary_union(blocked))
                    candidates, count = _candidate_rectangles(area, spec, free, trial_roads, preference, options,
                                                       options["candidate_budget"] - tried)
                    tried += count
                    selected = None
                    for candidate in candidates:
                        _, x, y, width, depth, polygon = candidate
                        connector = extend_access(boundary, obstacles, [p["polygon"] for p in [*placed, *pending]],
                                                  polygon, trial_roads, program)
                        if connector is not None:
                            selected = candidate
                            break
                    if selected is None:
                        break
                    _, x, y, width, depth, polygon = selected
                    trial_lines.extend(connector)
                    trial_roads = unary_union([trial_roads, *[line.buffer(program["road"]["width_m"]/2, cap_style=2) for line in connector]])
                    pending.append({"id": f"{spec['id']}-{index+1}", "facility_id": spec["id"], "name": spec["name"],
                                    "width_m": width, "depth_m": depth, "clearance_m": spec["clearance_m"],
                                    "polygon": polygon, "position_m": [x, y], "area_m2": polygon.area})
                complete = len(pending) == parts
                part_trials.append({"parts": parts, "tested_rectangles": tried, "complete": complete})
                if complete:
                    success = pending
                    current_roads = trial_roads
                    added_lines.extend(trial_lines)
                    break
            attempt["facilities"].append({"id": spec["id"], "part_trials": part_trials, "complete": success is not None})
            print(json.dumps({"planning_attempt": attempt_no, "facility": spec["name"], "parts": len(success or []),
                              "area_target_m2": spec["target_area_m2"]}, ensure_ascii=False), flush=True)
            if success is None:
                break
            placed.extend(success)
        attempts.append(attempt)
        if {p["facility_id"] for p in placed} == {s["id"] for s in active}:
            return placed, attempts, added_lines
    raise PlanningFailure(json.dumps({"message": "Finite search did not fit the complete unshrunk program", "attempts": attempts}, ensure_ascii=False))


def write_layout(source, target, scene, facilities, road_lines, program):
    if Path(source).resolve() == Path(target).resolve():
        raise PlanningFailure("The source CAD must not be overwritten")
    doc = ezdxf.readfile(source)
    if "CAD_AGENT_SITE" not in doc.appids:
        doc.appids.new("CAD_AGENT_SITE")
    for name, color in [("B_SITE_ROADS", 5), ("B_SITE_LABELS", 7), ("B_SITE_FACILITIES", 4)]:
        if name not in doc.layers:
            doc.layers.new(name, dxfattribs={"color": color})
    if "B_SITE_TEXT" not in doc.styles:
        doc.styles.new("B_SITE_TEXT", dxfattribs={"font": "NotoSansCJK-Regular.ttc"})
    if math.isclose(scene["metres_per_unit"], 1, rel_tol=1e-9):
        doc.header["$INSUNITS"] = 6
    scene["output_insunits"] = int(doc.header.get("$INSUNITS", 0))
    manifest = []
    msp = doc.modelspace()
    for index, line in enumerate(road_lines, 1):
        world = to_world(line, scene)
        entity = msp.add_lwpolyline(list(world.coords), dxfattribs={"layer": "B_SITE_ROADS", "const_width": program["road"]["width_m"]/scene["metres_per_unit"]})
        metadata = {"role": "road", "id": f"road-{index:03d}", "name": "施工道路", "handle": entity.dxf.handle,
                    "width_m": program["road"]["width_m"], "points_m": list(line.coords)}
        _write_tag(entity, metadata)
        manifest.append(metadata)
    for item in facilities:
        world = to_world(item["polygon"], scene)
        entity = msp.add_lwpolyline(list(world.exterior.coords)[:-1], close=True, dxfattribs={"layer": "B_SITE_FACILITIES"})
        metadata = {key: item[key] for key in ("id", "facility_id", "name", "width_m", "depth_m", "position_m", "clearance_m")}
        metadata.update(role="facility", handle=entity.dxf.handle, angle_deg=0)
        _write_tag(entity, metadata)
        # Compact labels only; assumptions and measurements belong in the report.
        label = msp.add_text(item["name"], dxfattribs={"layer": "B_SITE_LABELS", "style": "B_SITE_TEXT",
            "height": min(1.8, item["depth_m"] / 8)/scene["metres_per_unit"], "rotation": scene["angle_deg"]})
        label.set_placement(tuple(world.centroid.coords)[0], align=TextEntityAlignment.MIDDLE_CENTER)
        _write_tag(label, {"role": "annotation", "component_id": item["id"]})
        manifest.append(metadata)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(target)
    return manifest


def generate_layout(source, review_path, output_dir, prefix="v0009_site_layout", overrides=None, rules_path=None):
    started = time.perf_counter()
    source = Path(source)
    review = json.loads(Path(review_path).read_text())
    before = digest(source)
    if before != review["source_sha256"]:
        raise PlanningFailure("The input differs from the reviewed source version")
    facts = construct_facts(extract_drawing(source))
    refined = refine_spatial_facts(facts, review)
    rules = load_rules(rules_path)
    # Validate and merge all rule overrides before adopting geometric facts.
    # Facility budgets are recomputed below using the measured site area.
    planning_rules = {**rules, "planning": build_program(1, 0, overrides=overrides, rules=rules)["planning"]}
    scene = adopt_scene(facts, refined, planning_rules)
    boundary = to_local(shape(scene["boundary_geometry"]), scene)
    obstacles = {item["id"]: to_local(shape(item["geometry"]), scene) for item in scene["obstacles"]}
    retained_union = unary_union([obstacles[item["id"]] for item in scene["obstacles"] if item["service_target"]])
    occupied = boundary.intersection(retained_union).area
    scene["retained_building_area_m2"] = occupied
    scene["uncertain_reserved_area_m2"] = boundary.intersection(unary_union(list(obstacles.values())).difference(retained_union)).area
    program = build_program(boundary.area, occupied, overrides=overrides, rules=rules)
    scene["planning"] = program["planning"]
    if program["status"] == "budget_conflict":
        raise PlanningFailure(json.dumps(program["issues"], ensure_ascii=False))
    road_lines = build_roads(boundary, obstacles, scene, refined, program)
    roads = unary_union([line.buffer(program["road"]["width_m"]/2, cap_style=2) for line in road_lines])
    facilities, attempts, added_lines = place_facilities(boundary, obstacles, roads, scene, program)
    road_lines.extend(added_lines)
    roads = unary_union([line.buffer(program["road"]["width_m"]/2, cap_style=2) for line in road_lines])
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{prefix}.dxf"
    manifest = write_layout(source, target, scene, facilities, road_lines, program)
    from .site_plan_review import review_layout
    validation = review_layout(source, target, scene, program)
    # Report uncertain intersections as unresolved context, never as proof that
    # all unclassified native geometry was safe to remove or occupy.
    context = []
    placed_union = unary_union([p["polygon"] for p in facilities] + [roads])
    for item in refined["classifications"]:
        if item["category"] != "uncertain" or item["display_priority"] != "major":
            continue
        polygon = to_local(shape(item["geometry"]), scene)
        overlap = polygon.intersection(placed_union).area
        if overlap > 1e-6:
            context.append({"id": item["id"], "layer": item["layer"], "overlap_m2": overlap, "reason_codes": item["reason_codes"]})
    report = {"source_sha256": before, "target_sha256": digest(target), "source": str(source), "target": str(target),
              "scene": scene, "program": program, "components": manifest, "checks": validation.get("checks", []),
              "validation": validation, "metrics": validation.get("metrics", {}), "search_attempts": attempts,
              "unresolved_context_intersections": context, "status": validation.get("status", "unknown"),
              "engineering_compliance": False, "elapsed_seconds": time.perf_counter()-started,
              "source_unchanged": digest(source) == before,
              "limits": ["Geometry checks apply to retained outlines, declared provisional keepouts and adopted working boundary.",
                         "Other uncertain geometry is reported, not certified absent.",
                         "Gate connection outside the working boundary and vehicle turning envelopes are not validated.",
                         "Tower crane, construction hoist and sand/aggregate storage are outside this round."]}
    report_path = output_dir / f"{prefix}_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+"\n")
    from .site_layout_report import render_site_layout_report
    paths = render_site_layout_report(target, report, output_dir, prefix)
    return {"dxf": str(target), "report": str(report_path), **paths, "status": report["status"],
            "source_unchanged": report["source_unchanged"], "elapsed_seconds": report["elapsed_seconds"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prefix", default="v0009_site_layout")
    parser.add_argument("--rules", type=Path)
    parser.add_argument("--overrides", type=Path)
    args = parser.parse_args()
    if Path(args.prefix).name != args.prefix:
        parser.error("prefix must be a filename stem")
    overrides = json.loads(args.overrides.read_text()) if args.overrides else None
    print(json.dumps(generate_layout(args.source, args.review, args.output, args.prefix, overrides, args.rules), ensure_ascii=False))


if __name__ == "__main__":
    main()
