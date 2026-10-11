"""Offline axis evidence and layer-independent footprint triage.

Retained means a provisional geometric reservation, never an approved building.
Suppression removes redundant independent reservations, not source CAD entities.
Every decision remains bound to the reviewed input bytes and source geometry.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re

from shapely import affinity
from shapely.geometry import shape
from shapely.strtree import STRtree

from .geometry import digest
from .spatial_extract import extract_drawing
from .spatial_facts import BUILDING_LAYER, axis_metrics, construct_facts

NON_BUILDING_HINT = re.compile(r"植物|绿地|停车|家具|图框|铺装|标注|^CAR$|FURN|ANNO|PARK|PUB[-_]?TAB|TABLE", re.I)


@dataclass(frozen=True)
class RefinementConfig:
    axis_tolerance_deg: float = 2.0
    minimum_anchor_agreement: float = .75
    minimum_edge_support_fraction: float = .02
    minimum_rectangularity: float = .7
    minimum_orthogonal_perimeter_fraction: float = .85
    minimum_area_relative_to_smallest_anchor: float = .05
    maximum_area_relative_to_largest_anchor: float = 4.0
    maximum_aspect_ratio: float = 12.0
    area_absolute_tolerance_native2: float = 1e-6
    area_relative_tolerance: float = 1e-8
    boundary_distance_tolerance_native: float = 1e-5

    def __post_init__(self):
        for value in asdict(self).values():
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Refinement thresholds must be finite and positive")
        if self.axis_tolerance_deg >= 45:
            raise ValueError("Axis tolerance must be less than half the 90-degree period")
        for value in (self.minimum_anchor_agreement, self.minimum_edge_support_fraction,
                      self.minimum_rectangularity, self.minimum_orthogonal_perimeter_fraction):
            if value > 1:
                raise ValueError("Fraction thresholds must be <= 1")


def geometry_digest(geometry):
    return hashlib.sha256(json.dumps(geometry, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def axis_distance(a, b):
    """Orthogonal directions are equivalent; a long-edge angle is stored separately."""
    return abs((a - b + 45) % 90 - 45)


def _axis_mean(angles):
    x = sum(math.cos(math.radians(a * 4)) for a in angles)
    y = sum(math.sin(math.radians(a * 4)) for a in angles)
    if math.hypot(x, y) < 1e-10:
        return None
    return (math.degrees(math.atan2(y, x)) / 4) % 90


def validate_review(facts, review):
    if review.get("source_sha256") != facts["source"]["sha256"]:
        raise ValueError("Human review belongs to another source version")
    features = {f["id"]: f for f in facts["features"]}
    approved = []
    for entry in review.get("confirmed_outlines", []):
        item = features.get(entry["id"])
        if item is None or geometry_digest(item["geometry"]) != entry["geometry_sha256"]:
            raise ValueError(f"Reviewed geometry changed or is missing: {entry['id']}")
        if item["properties"]["geometry_status"] != "valid_reference_boundary":
            raise ValueError("Unresolved source geometry cannot become an approved reference")
        if entry["id"] in approved:
            raise ValueError("Duplicate reviewed outline")
        approved.append(entry["id"])
    if not approved:
        raise ValueError("No human-reviewed outlines supplied")
    if not set(review.get("axis_reference_ids", [])).issubset(approved):
        raise ValueError("Axis references must be reviewed outlines")
    return approved


def derive_axis_frame(facts, review, config=None):
    config = config or RefinementConfig()
    validate_review(facts, review)
    by_id = {f["id"]: f for f in facts["features"]}
    anchors = [{"id": key, **axis_metrics(by_id[key]["geometry"])}
               for key in review.get("axis_reference_ids", [])]
    clusters = [{a["id"] for a in anchors if axis_distance(a["axis_deg"], seed["axis_deg"])
                 <= config.axis_tolerance_deg} for seed in anchors]
    largest = max((len(c) for c in clusters), default=0)
    winners = {tuple(sorted(c)) for c in clusters if len(c) == largest}
    angle = None
    inliers = []
    if largest >= 2 and largest / len(anchors) >= config.minimum_anchor_agreement and len(winners) == 1:
        inliers = list(next(iter(winners)))
        angle = _axis_mean([a["axis_deg"] for a in anchors if a["id"] in inliers])
    edges = []
    boundary_shapes = {f["id"]: shape(f["geometry"]) for f in facts["features"]
                       if f["properties"]["kind"] == "boundary_candidate"}
    # Use real segments, not the bounding rectangle as a fictitious boundary edge.
    perimeter_by_boundary = Counter()
    for edge in facts["boundary_edges"]:
        perimeter_by_boundary[edge["boundary_id"]] += edge["length_native"]
    for edge in facts["boundary_edges"]:
        deviation = None if angle is None else axis_distance(edge["angle_deg"], angle)
        enough = edge["length_native"] >= config.minimum_edge_support_fraction * perimeter_by_boundary[edge["boundary_id"]]
        dx, dy = (edge["end"][i] - edge["start"][i] for i in (0, 1))
        length = math.hypot(dx, dy)
        tangent = [dx / length, dy / length] if length else None
        boundary = boundary_shapes.get(edge["boundary_id"])
        normal = None
        if tangent is not None and boundary is not None:
            sign = 1 if boundary.exterior.is_ccw else -1
            normal = [-sign * tangent[1], sign * tangent[0]]
        edges.append({**edge, "deviation_deg": deviation,
                      "unit_tangent_wcs": tangent, "local_inward_unit_normal_wcs": normal,
                      "normal_scope": "local side of source ring only; not a feasible placement or setback",
                      "supports_frame": deviation is not None and deviation <= config.axis_tolerance_deg and enough,
                      "sufficient_length": enough, "engineering_boundary_role_confirmed": False})
    support = [e for e in edges if e["supports_frame"]]
    status = "unresolved" if angle is None else "supported" if support else "anchor_only_boundary_unconfirmed"
    basis = None if angle is None else {"origin_wcs": facts["scope"]["origin"],
        "u": [math.cos(math.radians(angle)), math.sin(math.radians(angle))],
        "v": [-math.sin(math.radians(angle)), math.cos(math.radians(angle))],
        "world_to_local": "dot(point-origin,u), dot(point-origin,v)",
        "local_to_world": "origin + x*u + y*v; original CAD is not rotated"}
    return {"status": status, "axis_deg": angle, "local_frame": basis,
            "orthogonal_axis_deg": None if angle is None else angle + 90,
            "anchor_ids": inliers, "anchors": anchors,
            "anchor_outliers": [a["id"] for a in anchors if a["id"] not in inliers],
            "boundary_support": [{"edge_id": e["id"], **e} for e in support],
            "boundary_other_edges": [e["id"] for e in edges if not e["supports_frame"]],
            "boundary_support_length_native": sum(e["length_native"] for e in support),
            "boundary_total_length_native": sum(e["length_native"] for e in edges),
            "boundary_edges": edges,
            "meaning": "reviewed building orthogonal frame, independently checked against actual candidate boundary segments; not north or a universal placement bearing"}


def _orthogonal_fraction(poly, angle, tolerance):
    if angle is None:
        return 0.0
    total = aligned = 0.0
    for ring in [poly.exterior, *poly.interiors]:
        coords = list(ring.coords)
        for start, end in zip(coords, coords[1:]):
            length = math.dist(start, end)
            total += length
            bearing = math.degrees(math.atan2(end[1]-start[1], end[0]-start[0]))
            if axis_distance(bearing, angle) <= tolerance:
                aligned += length
    return aligned / total if total else 0.0


def refine_spatial_facts(facts, review, config=None):
    config = config or RefinementConfig()
    approved = validate_review(facts, review)
    frame = derive_axis_frame(facts, review, config)
    by_id = {f["id"]: f for f in facts["features"]}
    boundaries = [f for f in facts["features"] if f["properties"]["kind"] == "boundary_candidate"]
    if not boundaries:
        raise ValueError("A candidate boundary is required to define the review search scope")
    origin = facts["scope"]["origin"]
    def local(f):
        return affinity.translate(shape(f["geometry"]), -origin[0], -origin[1])
    site_polys = [local(f) for f in boundaries]
    approved_shapes = {key: local(by_id[key]) for key in approved}
    minimum_area = min(g.area for g in approved_shapes.values()) * config.minimum_area_relative_to_smallest_anchor
    maximum_area = max(g.area for g in approved_shapes.values()) * config.maximum_area_relative_to_largest_anchor
    selected = [f for f in facts["features"] if f["properties"]["kind"] in {"building_candidate", "context_outline"}
                and (f["id"] in approved or any(local(f).intersects(s) for s in site_polys))]
    selected.sort(key=lambda f: f["id"])
    items, polygons = {}, {}
    for feature in selected:
        key, properties = feature["id"], feature["properties"]
        poly = local(feature)
        polygons[key] = poly
        metrics = axis_metrics(feature["geometry"])
        deviation = None if frame["axis_deg"] is None else axis_distance(metrics["axis_deg"], frame["axis_deg"])
        aligned = _orthogonal_fraction(poly, frame["axis_deg"], config.axis_tolerance_deg)
        aspect = max(metrics["edge_lengths_native"]) / min(metrics["edge_lengths_native"])
        negative = bool(NON_BUILDING_HINT.search(properties["layer"]))
        original_layer = properties["kind"] == "building_candidate" and properties.get("selection_method") is None
        reasons = []
        if properties["geometry_status"] != "valid_reference_boundary":
            reasons.append("unresolved_source_geometry")
        if deviation is None or deviation > config.axis_tolerance_deg:
            reasons.append("axis_unresolved_or_misaligned")
        if aligned < config.minimum_orthogonal_perimeter_fraction:
            reasons.append("perimeter_not_orthogonal_to_frame")
        if metrics["rectangularity"] < config.minimum_rectangularity or aspect > config.maximum_aspect_ratio:
            reasons.append("shape_evidence_weak")
        if not minimum_area <= poly.area <= maximum_area:
            reasons.append("scale_outside_review_prior")
        if negative:
            reasons.append("non_building_layer_hint")
        human = key in approved
        items[key] = {"id": key, "geometry": feature["geometry"], "source": properties["source"],
            "label": properties["label"], "layer": properties["layer"], "original_layer_candidate": original_layer,
            "category": "retained" if human else "uncertain", "human_confirmed": human,
            "reason_codes": ["human_confirmed_outline"] if human else reasons,
            "angle_deviation_deg": deviation, "axis_deg": metrics["axis_deg"],
            "long_edge_angle_deg": metrics["long_edge_angle_deg"], "rectangularity": metrics["rectangularity"],
            "orthogonal_perimeter_fraction": aligned, "area_native2": poly.area,
            "covered_by": [], "conflicts_with": [], "enclosing_candidates": [],
            "confidence": {"level": "human_reviewed_geometry" if human else "unresolved",
                           "calibrated_probability": None, "semantic_role_confirmed": human,
                           "construction_phase_confirmed": False},
            "evidence": {"direction": deviation is not None and deviation <= config.axis_tolerance_deg,
                         "building_layer_hint": bool(BUILDING_LAYER.search(properties["layer"])),
                         "non_building_layer_hint": negative, "geometric_prior_satisfied": not reasons},
            "display_priority": "major" if human or original_layer or
                (minimum_area <= poly.area <= maximum_area and not negative) else "minor",
            "context_role": "reviewed_building" if human else "non_building_layer_hint" if negative else "unresolved_footprint"}
    def tolerance(a, b):
        return max(config.area_absolute_tolerance_native2,
                   config.area_relative_tolerance * min(a.area, b.area))
    def covered(child, parent):
        outside = child.difference(parent).area
        linear = config.boundary_distance_tolerance_native
        # Both distance and area must be negligible. A long thin protrusion
        # cannot disappear merely because its area is small.
        return (outside <= tolerance(child, parent) + child.length * linear
                and parent.buffer(linear).covers(child))
    relations = []
    for key, item in items.items():
        if item["human_confirmed"] or "unresolved_source_geometry" in item["reason_codes"]:
            continue
        polygon = polygons[key]
        for anchor, parent in approved_shapes.items():
            intersection = polygon.intersection(parent).area
            tol = tolerance(polygon, parent)
            if intersection <= tol:
                continue
            outside = polygon.difference(parent).area
            relations.append({"a": key, "b": anchor, "intersection_area_native2": intersection,
                              "a_outside_b_area_native2": outside, "b_human_confirmed": True})
            if covered(polygon, parent):
                item["covered_by"].append(anchor)
            else:
                item["conflicts_with"].append(anchor)
        if item["covered_by"]:
            item["category"] = "suppressed"
            item["reason_codes"] = ["covered_by_reviewed_outline"]
            item["confidence"]["level"] = "redundant_independent_footprint"
        elif item["conflicts_with"]:
            item["reason_codes"].append("partial_overlap_reviewed_outline")
    eligible = [key for key, item in items.items() if not item["human_confirmed"]
                and item["category"] != "suppressed" and not item["reason_codes"]]
    eligible_geometries = [polygons[key] for key in eligible]
    tree = STRtree(eligible_geometries)
    containers = {key: [] for key in eligible}
    for index, key in enumerate(eligible):
        poly = polygons[key]
        for match in tree.query(poly, predicate="intersects"):
            other_index = int(match)
            if other_index <= index:
                continue
            other = eligible[other_index]
            second = polygons[other]
            area = poly.intersection(second).area
            tol = tolerance(poly, second)
            if area <= tol:
                continue
            a_out = poly.difference(second).area
            b_out = second.difference(poly).area
            if covered(poly, second) and covered(second, poly):
                # Same footprint, stable representative; no drawing-content deletion.
                keep, duplicate = sorted([key, other])
                containers[duplicate].append(keep)
            elif covered(poly, second):
                containers[key].append(other)
            elif covered(second, poly):
                containers[other].append(key)
            else:
                items[key]["conflicts_with"].append(other)
                items[other]["conflicts_with"].append(key)
            relations.append({"a": key, "b": other, "intersection_area_native2": area,
                              "a_outside_b_area_native2": a_out, "b_outside_a_area_native2": b_out,
                              "b_human_confirmed": False})
    # Keep maximal qualifying exterior candidates. Containment is provisional
    # hierarchy evidence, not proof of building identity; conflicting roots abstain.
    for key in eligible:
        item = items[key]
        item["enclosing_candidates"] = sorted(containers[key])
        if item["conflicts_with"]:
            item["reason_codes"].append("partial_overlap_candidate")
        elif not containers[key]:
            item["category"] = "retained"
            item["reason_codes"] = ["axis_shape_scale_supported_outer_candidate"]
            item["confidence"]["level"] = "geometric_support_semantics_unconfirmed"
        else:
            item["reason_codes"] = ["nested_candidate_pending_parent_selection"]
    retained = {key: polygons[key] for key, item in items.items() if item["category"] == "retained"}
    # Unconfirmed descendants may be suppressed only when an actual retained
    # parent covers their complete footprint. Partial overlap never deletes tails.
    for key, item in items.items():
        if item["category"] in {"retained", "suppressed"} or "unresolved_source_geometry" in item["reason_codes"]:
            continue
        covering = [parent for parent, geom in retained.items() if
                    geom.area >= polygons[key].area and covered(polygons[key], geom)]
        if covering:
            item["category"] = "suppressed"
            item["covered_by"] = sorted(covering)
            item["reason_codes"] = ["covered_by_provisional_outer_candidate"]
            item["confidence"]["level"] = "conditional_redundant_footprint"
    # Record qualified containing regions even if neither can safely be selected.
    for key, item in items.items():
        for field in ("covered_by", "conflicts_with", "enclosing_candidates"):
            item[field] = sorted(set(item[field]))
        item["constraint_policy"] = {
            "retained": {"road": "reserve_footprint_pending_phase_review", "stockyard": "reserve_footprint_pending_phase_review"},
            "suppressed": {"road": "inherit_retained_parent", "stockyard": "inherit_retained_parent"},
            "uncertain": {"road": "review_before_crossing", "stockyard": "review_before_occupation"},
        }[item["category"]] | {"coverage_circle": "overlap_is_not_solid_occupancy_conflict",
                              "engineering_compliance_verified": False}
    layer_items = [item for item in items.values() if item["original_layer_candidate"]]
    def counts(values):
        c = Counter(i["category"] for i in values)
        return {key: c[key] for key in ("retained", "suppressed", "uncertain")}
    return {"schema_version": "spatial-refinement/0.1", "source": facts["source"],
        "review": review, "config": asdict(config), "axis_frame": frame,
        "classifications": list(items.values()), "relations": sorted(relations, key=lambda r: (r["a"], r["b"])),
        "summary": {"reviewed_anchors": len(approved), "analyzed_closed_outlines": len(items),
                    "layer_candidates": len(layer_items), "layer_counts": counts(layer_items),
                    "global_counts": counts(items.values()),
                    "retained_unconfirmed": sum(i["category"] == "retained" and not i["human_confirmed"] for i in items.values()),
                    "minor_uncertain": sum(i["category"] == "uncertain" and i["display_priority"] == "minor" for i in items.values())},
        "search_scope": "all extracted closed reference polygons intersecting candidate boundaries, plus reviewed outlines even outside; no layer gate",
        "size_prior_native2": {"minimum": minimum_area, "maximum": maximum_area,
                               "meaning": "relative review priority, not a minimum building size; excluded scales remain uncertain"},
        "coverage": {"free_space_certified": False, "all_buildings_recognized": False,
                     "unresolved_source_instances": facts["summary"]["unresolved_entity_instances"],
                     "non_area_by_type": facts["inventory"]["non_area_by_type"],
                     "note": "open line assembly, curved boundaries, proxies and unknown design roles remain unresolved; blank space is not certified free"},
        "deletion": {"source_entities_deleted": 0, "meaning": "suppressed entries leave only the independent constraint list, retain source geometry and parent links"},
        "boundary_edges": frame["boundary_edges"], "units": facts["units"]}


def assess_footprint_constraints(geometry, refinement, role="stockyard"):
    """Preview the tiered policy only; never certify placement or export CAD."""
    if role not in {"stockyard", "road", "coverage_circle"}:
        raise ValueError("Unknown footprint role")
    proposed = shape(geometry)
    if not proposed.is_valid or proposed.is_empty or proposed.geom_type != "Polygon":
        raise ValueError("A valid complete footprint polygon is required")
    reserved, unknown = [], []
    for item in refinement["classifications"]:
        if item["category"] == "suppressed":
            continue
        footprint = shape(item["geometry"])
        area = proposed.intersection(footprint).area
        tol = max(refinement["config"]["area_absolute_tolerance_native2"],
                  refinement["config"]["area_relative_tolerance"] * min(proposed.area, footprint.area))
        if area <= tol:
            continue
        (reserved if item["category"] == "retained" else unknown).append({"id": item["id"], "area_native2": area})
    status = ("coverage_needs_separate_checks" if role == "coverage_circle" else
              "intersects_reserved_footprint" if reserved else "requires_review" if unknown else "no_classified_conflict")
    return {"status": status, "role": role, "reserved_intersections": reserved,
            "uncertain_intersections": unknown, "site_feasible": None,
            "note": "phase, units, setbacks, boundary identity and unresolved source geometry are not validated"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--review", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--prefix", default="spatial_axis")
    args = parser.parse_args()
    if not re.fullmatch(r"[\w-]+", args.prefix):
        parser.error("prefix must be a filename stem")
    review = json.loads(args.review.read_text())
    before = digest(args.source)
    if before != review.get("source_sha256"):
        parser.error("source differs from the reviewed version")
    extracted = extract_drawing(args.source)
    facts = construct_facts(extracted)
    if facts["source"]["sha256"] != before or digest(args.source) != before:
        raise RuntimeError("Source changed during analysis")
    refinement = refine_spatial_facts(facts, review)
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / f"{args.prefix}_facts.json"
    path.write_text(json.dumps(refinement, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    csv_path = args.output / f"{args.prefix}_outlines.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["来源ID", "图层", "分类", "人工确认轮廓", "面积_原生平方", "轴差_度",
                         "矩形度", "同轴边长占比", "父轮廓", "冲突轮廓", "理由代码", "优先级"])
        for item in refinement["classifications"]:
            writer.writerow([item["id"], item["layer"], item["category"], item["human_confirmed"],
                             item["area_native2"], item["angle_deviation_deg"], item["rectangularity"],
                             item["orthogonal_perimeter_fraction"], ";".join(item["covered_by"]),
                             ";".join(item["conflicts_with"]), ";".join(item["reason_codes"]), item["display_priority"]])
    from .spatial_refinement_report import render_refinement_report
    paths = render_refinement_report(facts, refinement, args.output, args.prefix)
    print(json.dumps({"facts": str(path), "outlines_csv": str(csv_path), **paths, "summary": refinement["summary"],
                      "source_unchanged": digest(args.source) == before}, ensure_ascii=False))


if __name__ == "__main__":
    main()
