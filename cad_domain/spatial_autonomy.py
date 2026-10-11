"""Autonomous Part A: derive evidence and outline tiers from the supplied DXF only.

No reviewed geometry, project-specific IDs, previous layout, or saved answer is an
input. ``machine_supported`` means the drawing's independent observations agree;
it never means a person confirmed a building, phase, boundary or construction rule.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
import math
from pathlib import Path
import re

from shapely import affinity
from shapely.geometry import Point, shape
from shapely.strtree import STRtree

from .geometry import UNIT_METRES, digest
from .spatial_extract import extract_drawing
from .spatial_facts import BUILDING_LAYER, BUILDING_NAME, TABLE_TITLE, axis_metrics, construct_facts

# Layer names are evidence, not an exhaustive whitelist of building layers.
NON_BUILDING_HINT = re.compile(
    r"植物|绿地|停车|家具|图框|铺装|标注|^CAR$|FURN|ANNO|PARK|PUB[-_]?TAB|TABLE", re.I
)


@dataclass(frozen=True)
class AnalysisConfig:
    axis_tolerance_deg: float = 2.0
    minimum_anchor_agreement: float = .75
    minimum_axis_anchors: int = 2
    minimum_edge_support_fraction: float = .02
    minimum_rectangularity: float = .7
    minimum_orthogonal_perimeter_fraction: float = .85
    minimum_area_relative_to_smallest_anchor: float = .05
    maximum_area_relative_to_largest_anchor: float = 4.0
    maximum_aspect_ratio: float = 12.0
    area_absolute_tolerance_native2: float = 1e-6
    area_relative_tolerance: float = 1e-8
    boundary_distance_tolerance_native: float = 1e-5


def _axis_distance(a, b):
    return abs((a - b + 45) % 90 - 45)


def _axis_mean(angles):
    x = sum(math.cos(math.radians(value * 4)) for value in angles)
    y = sum(math.sin(math.radians(value * 4)) for value in angles)
    return None if math.hypot(x, y) < 1e-10 else (math.degrees(math.atan2(y, x)) / 4) % 90


def _orthogonal_fraction(polygon, angle, tolerance):
    if angle is None:
        return 0.0
    total = aligned = 0.0
    for ring in (polygon.exterior, *polygon.interiors):
        coords = list(ring.coords)
        for start, end in zip(coords, coords[1:]):
            length = math.dist(start, end)
            total += length
            bearing = math.degrees(math.atan2(end[1] - start[1], end[0] - start[0]))
            if _axis_distance(bearing, angle) <= tolerance:
                aligned += length
    return aligned / total if total else 0.0


def _derive_frame(anchors, boundaries, selected, origin, config):
    observations = [{"id": feature["id"], **axis_metrics(feature["geometry"])}
                    for feature in anchors]
    clusters = [{a["id"] for a in observations
                 if _axis_distance(a["axis_deg"], seed["axis_deg"]) <= config.axis_tolerance_deg}
                for seed in observations]
    largest = max((len(group) for group in clusters), default=0)
    winners = {tuple(sorted(group)) for group in clusters if len(group) == largest}
    inliers, angle = [], None
    if (largest >= config.minimum_axis_anchors and observations
            and largest / len(observations) >= config.minimum_anchor_agreement and len(winners) == 1):
        inliers = list(next(iter(winners)))
        angle = _axis_mean([a["axis_deg"] for a in observations if a["id"] in inliers])
    edges = []
    for boundary in boundaries:
        polygon = shape(boundary["geometry"])
        coords = list(polygon.exterior.coords)
        for index, (start, end) in enumerate(zip(coords, coords[1:]), 1):
            length = math.dist(start, end)
            if length <= 0:
                continue
            bearing = math.degrees(math.atan2(end[1] - start[1], end[0] - start[0])) % 180
            deviation = None if angle is None else _axis_distance(bearing, angle)
            tangent = [(end[i] - start[i]) / length for i in (0, 1)]
            sign = 1 if polygon.exterior.is_ccw else -1
            enough = length >= config.minimum_edge_support_fraction * polygon.length
            chosen = selected is not None and boundary["id"] == selected["id"]
            edges.append({"id": f"{boundary['id']}:edge:{index}", "boundary_id": boundary["id"],
                          "start": list(start), "end": list(end), "length_native": length,
                          "angle_deg": bearing, "deviation_deg": deviation,
                          "unit_tangent_wcs": tangent,
                          "local_inward_unit_normal_wcs": [-sign * tangent[1], sign * tangent[0]],
                          "normal_scope": "local source-ring side, not a feasibility or setback guarantee",
                          "sufficient_length": enough,
                          "supports_frame": chosen and boundary["geometry_status"] == "valid_reference_boundary"
                              and enough and deviation is not None and deviation <= config.axis_tolerance_deg,
                          "engineering_boundary_role_confirmed": False})
    support = [edge for edge in edges if edge["supports_frame"]]
    basis = None if angle is None else {
        "origin_wcs": origin,
        "u": [math.cos(math.radians(angle)), math.sin(math.radians(angle))],
        "v": [-math.sin(math.radians(angle)), math.cos(math.radians(angle))],
        "world_to_local": "dot(point-origin,u), dot(point-origin,v)",
        "local_to_world": "origin+x*u+y*v; source CAD is not rotated",
    }
    return {"status": "unresolved" if angle is None else "machine_supported" if support
            else "anchor_only_boundary_unconfirmed", "axis_deg": angle,
            "orthogonal_axis_deg": None if angle is None else angle + 90,
            "local_frame": basis, "anchor_ids": inliers, "anchors": observations,
            "anchor_outliers": [a["id"] for a in observations if a["id"] not in inliers],
            "boundary_support": support, "boundary_edges": edges,
            "human_confirmed": False,
            "meaning": "Machine-evidenced orthogonal axis family, not north or the long edge of every building; no longest-edge fallback."}


def _classify(extracted, facts, anchors, selected, frame, config):
    features = {feature["id"]: feature for feature in facts["features"]}
    anchor_ids = {feature["id"] for feature in anchors}
    table_roots = {text["source"]["root_handle"] for text in extracted["texts"]
                   if TABLE_TITLE.search(text["text"])}
    table_roots.update(row["header"]["source"]["root_handle"] for row in facts["area_table"])
    name_observations = [(text, Point(text["position"])) for text in extracted["texts"]
                         if text["geometry_status"] == "anchor_only"
                         and text["source"]["root_handle"] not in table_roots
                         and BUILDING_NAME.search(text["text"])]
    boundary_ids = {feature["id"] for feature in facts["features"]
                    if feature["properties"]["kind"] == "boundary_candidate"}
    origin = facts["scope"]["origin"]
    def local(geometry):
        return affinity.translate(shape(geometry), -origin[0], -origin[1])
    site = local(selected["geometry"]) if selected is not None else None
    anchored = {feature["id"]: local(feature["geometry"]) for feature in anchors}
    smallest = min((poly.area for poly in anchored.values()), default=None)
    largest = max((poly.area for poly in anchored.values()), default=None)
    minimum = None if smallest is None else smallest * config.minimum_area_relative_to_smallest_anchor
    maximum = None if largest is None else largest * config.maximum_area_relative_to_largest_anchor
    def tolerance(a, b):
        return max(config.area_absolute_tolerance_native2,
                   config.area_relative_tolerance * min(a.area, b.area))
    def covered(child, parent):
        distance = config.boundary_distance_tolerance_native
        return (child.difference(parent).area <= tolerance(child, parent) + child.length * distance
                and parent.buffer(distance).covers(child))
    items, polygons = {}, {}
    for record in sorted(extracted["polygons"], key=lambda item: item["id"]):
        key = record["id"]
        if key in boundary_ids:
            continue
        polygon = local(record["geometry"])
        properties = features.get(key, {}).get("properties", {})
        # Retain separately labelled observations beyond the scope as conflicts,
        # but never restrict search to an expected set of layers or IDs.
        if site is not None and not polygon.intersects(site) and not properties.get("primary"):
            continue
        polygons[key] = polygon
        metrics = axis_metrics(record["geometry"])
        negative = bool(NON_BUILDING_HINT.search(record["effective_layer"]))
        observations = list(properties.get("evidence", []))
        if not observations:
            world_shape = shape(record["geometry"])
            observations = [{"kind": "contained_name_only", "id": text["id"], "text": text["text"],
                             "position": text["position"], "source": text["source"]}
                            for text, point in name_observations if world_shape.covers(point)]
        building_hint = bool(BUILDING_LAYER.search(record["effective_layer"]))
        valid = record["geometry_status"] == "valid_reference_boundary"
        angle = frame["axis_deg"]
        deviation = None if angle is None else _axis_distance(metrics["axis_deg"], angle)
        orthogonal = _orthogonal_fraction(polygon, angle, config.axis_tolerance_deg)
        scale_support = minimum is not None and minimum <= polygon.area <= maximum
        aspect = max(metrics["edge_lengths_native"]) / min(metrics["edge_lengths_native"])
        reasons = []
        if not valid:
            reasons.append("unresolved_source_geometry")
        if site is None:
            reasons.append("boundary_scope_unresolved")
        if deviation is None or deviation > config.axis_tolerance_deg:
            reasons.append("axis_unresolved_or_misaligned")
        if orthogonal < config.minimum_orthogonal_perimeter_fraction:
            reasons.append("perimeter_not_orthogonal_to_frame")
        if metrics["rectangularity"] < config.minimum_rectangularity or aspect > config.maximum_aspect_ratio:
            reasons.append("shape_evidence_weak")
        if not scale_support:
            reasons.append("scale_outside_machine_anchor_prior")
        if negative:
            reasons.append("non_building_layer_hint")
        machine = key in anchor_ids
        item = {"id": key, "geometry": record["geometry"], "source": record["source"],
                "label": properties.get("label", "闭合轮廓候选"), "layer": record["effective_layer"],
                "category": "retained" if machine else "uncertain",
                "machine_supported": machine, "human_confirmed": False,
                "geometry_status": record["geometry_status"],
                "constraint_tier": "machine_supported_footprint" if machine else "unresolved",
                "reason_codes": ["number_name_and_independent_table_area_agree"] if machine else reasons,
                "number": properties.get("number"), "area_native2": polygon.area,
                "axis_deg": metrics["axis_deg"], "long_edge_angle_deg": metrics["long_edge_angle_deg"],
                "angle_deviation_deg": deviation, "rectangularity": metrics["rectangularity"],
                "orthogonal_perimeter_fraction": orthogonal,
                "covered_by": [], "conflicts_with": [], "enclosing_candidates": [],
                "original_layer_candidate": building_hint,
                "confidence": {"level": "machine_supported" if machine else "unresolved",
                               "calibrated_probability": None, "semantic_role_confirmed": False,
                               "construction_phase_confirmed": False},
                "evidence": {"drawing_observations": observations,
                             "direction": deviation is not None and deviation <= config.axis_tolerance_deg,
                             "building_layer_hint": building_hint, "non_building_layer_hint": negative,
                             "geometric_prior_satisfied": not reasons},
                "display_priority": "major" if machine or properties.get("primary") or building_hint
                    or (scale_support and not negative) else "minor",
                "context_role": "machine_evidenced_building_reference" if machine else "unresolved_footprint",
                "width_native": record.get("width_native"), "width_wcs_max": record.get("width_wcs_max"),
                "occluded_by": properties.get("occluded_by", [])}
        if site is not None:
            outside = polygon.difference(site).area
            item["boundary_relation"] = {"boundary_id": selected["id"], "outside_area_native2": outside,
                                         "outside_fraction": outside / polygon.area,
                                         "meaning": "geometric relation to a candidate boundary, not an engineering violation"}
        items[key] = item
    relations = []
    for key, item in items.items():
        if item["machine_supported"] or item["geometry_status"] != "valid_reference_boundary":
            continue
        polygon = polygons[key]
        for parent_id, parent in anchored.items():
            intersection = polygon.intersection(parent).area
            if intersection <= tolerance(polygon, parent):
                continue
            relations.append({"a": key, "b": parent_id, "intersection_area_native2": intersection,
                              "b_support": "machine_supported"})
            if covered(polygon, parent):
                item["covered_by"].append(parent_id)
            else:
                item["conflicts_with"].append(parent_id)
        if item["covered_by"]:
            item.update(category="suppressed", constraint_tier="dependent_geometry",
                        reason_codes=["covered_by_machine_supported_reference"], context_role="dependent_geometry")
            item["confidence"]["level"] = "geometrically_redundant_independent_reservation"
        elif item["conflicts_with"]:
            item["reason_codes"].append("partial_overlap_machine_supported_reference")
        elif item["evidence"]["non_building_layer_hint"] and not item["evidence"]["drawing_observations"]:
            item.update(category="non_building", constraint_tier="non_building_hint",
                        reason_codes=["non_building_layer_hint"], context_role="non_building_hint")
            item["confidence"]["level"] = "semantic_layer_hint_only"
    eligible = [key for key, item in items.items()
                if item["category"] == "uncertain" and not item["reason_codes"]]
    tree = STRtree([polygons[key] for key in eligible])
    containers = {key: [] for key in eligible}
    for index, key in enumerate(eligible):
        first = polygons[key]
        for match in tree.query(first, predicate="intersects"):
            other_index = int(match)
            if other_index <= index:
                continue
            other = eligible[other_index]
            second = polygons[other]
            if first.intersection(second).area <= tolerance(first, second):
                continue
            first_in_second = covered(first, second)
            second_in_first = covered(second, first)
            if first_in_second and second_in_first:
                keep, duplicate = sorted((key, other))
                containers[duplicate].append(keep)
            elif first_in_second:
                containers[key].append(other)
            elif second_in_first:
                containers[other].append(key)
            else:
                items[key]["conflicts_with"].append(other)
                items[other]["conflicts_with"].append(key)
    for key in eligible:
        item = items[key]
        item["enclosing_candidates"] = sorted(containers[key])
        if item["conflicts_with"]:
            item["reason_codes"].append("partial_overlap_candidate")
        elif not containers[key]:
            item.update(category="retained", constraint_tier="provisional_reservation",
                        reason_codes=["axis_shape_scale_supported_outer_candidate"],
                        context_role="geometric_reservation_semantics_unconfirmed")
            item["confidence"]["level"] = "geometric_support_semantics_unconfirmed"
        else:
            item["reason_codes"] = ["nested_candidate_pending_parent_selection"]
    retained = {key: polygons[key] for key, item in items.items() if item["category"] == "retained"}
    for key, item in items.items():
        if item["category"] in {"retained", "suppressed"} or item["geometry_status"] != "valid_reference_boundary":
            continue
        parents = [parent_id for parent_id, parent in retained.items()
                   if parent.area >= polygons[key].area and covered(polygons[key], parent)]
        if parents:
            item.update(category="suppressed", constraint_tier="dependent_geometry", covered_by=sorted(parents),
                        reason_codes=["covered_by_provisional_reservation"], context_role="dependent_geometry")
            item["confidence"]["level"] = "conditional_redundant_footprint"
    for item in items.values():
        for field in ("covered_by", "conflicts_with", "enclosing_candidates"):
            item[field] = sorted(set(item[field]))
        item["constraint_policy"] = {
            "retained": "reserve_reference_pending_phase_and_width_interpretation",
            "suppressed": "inherit_retained_parent_without_deleting_source",
            "uncertain": "query_evidence_before_occupation",
            "non_building": "not_a_building_reservation_but_not_certified_free",
        }[item["category"]]
    return list(items.values()), relations, {"minimum": minimum, "maximum": maximum,
                "meaning": "Relative geometry triage from machine-supported anchors, not a minimum building size."}


def analyze_spatial(source: str, boundary_id: str | None = None) -> dict:
    """Read one current DXF and return machine-evidenced spatial facts, never edit it.

    A sole boundary candidate supplies a geometric analysis scope. Multiple
    candidates require an explicit ID; no boundary's legal role is auto-confirmed.
    Missing evidence produces an unresolved frame, not a borrowed previous angle.
    """
    source_path = Path(source)
    before = digest(source_path)
    extracted = extract_drawing(source_path)
    if extracted["source_sha256"] != before:
        raise RuntimeError("Source changed before extraction; no stale facts returned")
    try:
        facts = construct_facts(extracted)
    except ValueError as exc:
        if not str(exc).startswith("No defensible scene extent"):
            raise
        facts = {"source": {"name": source_path.name, "sha256": before, "units_code": extracted["units_code"]},
                 "features": [], "scope": {"origin": [0.0, 0.0]}, "boundary_edges": [], "area_table": [],
                 "units": {"status": "unresolved", "declared_code": extracted["units_code"],
                           "declared_metres_per_unit": UNIT_METRES.get(extracted["units_code"]),
                           "candidate_metres_per_unit": None, "confirmed": False, "evidence": []},
                 "unresolved": [{"code": "scene_extent_unresolved", "message": str(exc), "source_ids": []}],
                 "inventory": extracted["inventory"]}
    config = AnalysisConfig()
    boundaries = [{"id": f["id"], "geometry": f["geometry"], "source": f["properties"]["source"],
                   "label": f["properties"]["label"], "layer": f["properties"]["layer"],
                   "geometry_status": f["properties"]["geometry_status"],
                   "status": "candidate", "engineering_role_confirmed": False,
                   "land_area_comparison": f["properties"].get("land_area_comparison")}
                  for f in facts["features"] if f["properties"]["kind"] == "boundary_candidate"]
    selected, selection_reason = None, "unresolved"
    if boundary_id is not None:
        selected = next((item for item in boundaries if item["id"] == boundary_id), None)
        if selected is None or selected["geometry_status"] != "valid_reference_boundary":
            raise ValueError("boundary_id must name a valid extracted boundary candidate in this source file")
        selection_reason = "explicit_candidate_id_for_geometry_scope_only"
    elif len(boundaries) == 1 and boundaries[0]["geometry_status"] == "valid_reference_boundary":
        selected = boundaries[0]
        selection_reason = "sole_drawn_boundary_candidate_for_geometry_scope_only"
    anchors = [f for f in facts["features"] if f["properties"].get("primary")
               and f["properties"].get("status") == "supported"
               and f["properties"]["geometry_status"] == "valid_reference_boundary"]
    if selected is not None:
        selected_shape = shape(selected["geometry"])
        anchors = [feature for feature in anchors if shape(feature["geometry"]).intersects(selected_shape)]
    # Conflicting semantic layer hints are not silently overruled by equal area.
    conflicting = [f for f in anchors if NON_BUILDING_HINT.search(f["properties"]["layer"])]
    anchors = [f for f in anchors if f not in conflicting]
    frame = _derive_frame(anchors, boundaries, selected, facts["scope"]["origin"], config)
    classifications, relations, size_prior = _classify(extracted, facts, anchors, selected, frame, config)
    issues = list(facts["unresolved"])
    if selected is None:
        issues.append({"code": "boundary_selection_unresolved", "message": "No sole valid boundary candidate; query candidates or specify boundary_id.",
                       "source_ids": [item["id"] for item in boundaries]})
    if frame["axis_deg"] is None:
        issues.append({"code": "axis_evidence_insufficient", "message": "No unique multi-building machine-supported direction consensus; no default direction assumed.",
                       "source_ids": [f["id"] for f in anchors]})
    if conflicting:
        issues.append({"code": "semantic_layer_conflict", "message": "Equal area and text association conflict with a non-building layer hint; excluded as axis anchors.",
                       "source_ids": [f["id"] for f in conflicting]})
    after = digest(source_path)
    if after != before:
        raise RuntimeError("Source changed during analysis; no stale facts returned")
    counts = Counter(item["category"] for item in classifications)
    return {"schema_version": "spatial-autonomy/0.1", "source": {**facts["source"], "path": str(source_path), "unchanged": True},
            "units": {**facts["units"], "adopted_metres_per_unit": None}, "axis_frame": frame,
            "classifications": classifications, "relations": relations, "config": asdict(config),
            "boundary_candidates": boundaries, "selected_boundary": selected,
            "boundary_selection_reason": selection_reason, "boundary_edges": frame["boundary_edges"],
            "area_table": facts["area_table"], "size_prior_native2": size_prior, "scope": facts["scope"],
            "summary": {"analyzed_closed_outlines": len(classifications),
                        "machine_supported_buildings": len(anchors), "human_confirmed_buildings": 0,
                        "boundary_candidates": len(boundaries), "counts": dict(counts),
                        "major_uncertain": sum(item["category"] == "uncertain" and item["display_priority"] == "major" for item in classifications),
                        "all_buildings_recognized": False, "free_space_certified": False},
            "inventory": extracted["inventory"], "issues": issues,
            "assumptions": ["Only this source DXF and general rules were read; no reviewed outlines or layout answers are inputs.",
                            "Machine-supported means drawing evidence agrees, not human-confirmed construction semantics.",
                            "A selected boundary is a geometric scope candidate; legal status and construction phase remain unknown.",
                            "Widths are preserved as drawing evidence; reference faces are not buffered physical footprints.",
                            "Geometric reservations remain provisional; non-building hints do not certify usable land.",
                            "Missing parsed geometry or evidence is uncertainty, never assumed vacant space."],
            "deletion": {"source_entities_deleted": 0, "meaning": "Suppression only links dependent reservations; source entities remain intact."}}
