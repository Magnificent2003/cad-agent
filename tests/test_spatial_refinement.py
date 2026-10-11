"""Representative v0009 review checks, plus two small geometry counterexamples.

Source handles below are independent review references, never production rules.
The real engineering fixture is optional outside this workspace. These checks do
not call an LLM, launch services, export a modified CAD, or certify free space.
"""
from copy import deepcopy
import hashlib
from itertools import combinations
import json
from pathlib import Path

import pytest
from shapely import affinity
from shapely.geometry import box, mapping, shape
from shapely.ops import unary_union

from cad_domain.spatial_refinement import (
    assess_footprint_constraints,
    axis_distance,
    derive_axis_frame,
    geometry_digest,
    refine_spatial_facts,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/agent-tasks/786b4ced67ef4d1198bfe8bdf0359aee/input/drawing.dxf"
FACTS = ROOT / "dev_log_0929/v0009_spatial_facts.json"
REVIEW = ROOT / "dev_log_0929/v0009_outline_review.json"
SHA256 = "20f23308dd1246ed6e8e60549b569ecce0408b166b76c153f3844f3a7ec906db"


@pytest.fixture(scope="module")
def v9():
    if not all(path.exists() for path in (SOURCE, FACTS, REVIEW)):
        pytest.skip("Reviewed private v0009 engineering fixture is not present")
    before = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    assert before == SHA256
    facts = json.loads(FACTS.read_text())
    review = json.loads(REVIEW.read_text())
    assert facts["source"]["sha256"] == before == review["source_sha256"]
    before_facts, before_review = deepcopy(facts), deepcopy(review)
    result = refine_spatial_facts(facts, review)
    assert facts == before_facts and review == before_review
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == before
    return facts, review, result


def _items(result):
    return {item["id"]: item for item in result["classifications"]}


def test_review_rejects_another_version_or_changed_approved_geometry(v9):
    facts, review, result = v9
    assert result["source"]["sha256"] == SHA256
    changed = deepcopy(review)
    changed["source_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="another source version"):
        refine_spatial_facts(facts, changed)
    changed_facts = deepcopy(facts)
    approved_id = review["confirmed_outlines"][0]["id"]
    feature = next(item for item in changed_facts["features"] if item["id"] == approved_id)
    feature["geometry"]["coordinates"][0][0][0] += 1
    with pytest.raises(ValueError, match="Reviewed geometry changed"):
        refine_spatial_facts(changed_facts, review)
    assert result["deletion"]["source_entities_deleted"] == 0


def test_real_frame_uses_reviewed_buildings_and_actual_nonzero_boundary_edges(v9):
    _, review, result = v9
    frame = result["axis_frame"]
    assert frame["status"] == "supported"
    assert frame["axis_deg"] == pytest.approx(44.2662115, abs=1e-5)
    assert frame["orthogonal_axis_deg"] == pytest.approx(134.2662115, abs=1e-5)
    assert set(frame["anchor_ids"]) == set(review["axis_reference_ids"])
    assert len(frame["boundary_edges"]) == 16
    assert all(edge["length_native"] > 0 for edge in frame["boundary_edges"])
    assert {edge["id"] for edge in frame["boundary_support"]} == {
        "BAE9:edge:14", "BAE9:edge:15", "BAE9:edge:16",
    }
    assert all(not edge["engineering_boundary_role_confirmed"] for edge in frame["boundary_edges"])


def test_layer_independent_search_finds_large_candidates_without_promoting_symbols(v9):
    _, _, result = v9
    items = _items(result)
    assert result["summary"]["layer_candidates"] == 48
    assert result["summary"]["analyzed_closed_outlines"] > 2000
    for key in ("AEC4", "AEC7", "AEF6"):
        item = items[key]
        assert item["layer"] == "YD-R2"
        assert item["original_layer_candidate"] is False
        assert item["category"] == "retained"
        assert item["confidence"]["semantic_role_confirmed"] is False
        assert item["confidence"]["calibrated_probability"] is None
    cars = [item for item in items.values() if item["layer"] == "CAR"]
    assert len(cars) > 1000
    assert all(item["category"] != "retained" for item in cars)
    assert items["15CF"]["layer"] == "JMD"
    assert items["15CF"]["category"] == "uncertain"
    assert "axis_unresolved_or_misaligned" in items["15CF"]["reason_codes"]
    assert result["coverage"]["all_buildings_recognized"] is False
    assert result["coverage"]["free_space_certified"] is False


def test_real_reservations_do_not_overlap_and_suppression_preserves_parent_evidence(v9):
    _, review, result = v9
    items = _items(result)
    retained = {key: shape(item["geometry"]) for key, item in items.items() if item["category"] == "retained"}
    assert len(retained) > len(review["confirmed_outlines"])
    for (_, first), (_, second) in combinations(retained.items(), 2):
        tolerance = max(result["config"]["area_absolute_tolerance_native2"],
                        result["config"]["area_relative_tolerance"] * min(first.area, second.area))
        assert first.intersection(second).area <= tolerance
    distance = result["config"]["boundary_distance_tolerance_native"]
    suppressed = [item for item in items.values() if item["category"] == "suppressed"]
    assert suppressed
    for item in suppressed:
        assert item["covered_by"]
        assert any(retained[parent].buffer(distance).covers(shape(item["geometry"]))
                   for parent in item["covered_by"])
        assert item["constraint_policy"]["stockyard"] == "inherit_retained_parent"
        assert item["source"]  # Suppression keeps source identity; it is not CAD deletion.
    for entry in review["confirmed_outlines"]:
        item = items[entry["id"]]
        assert item["human_confirmed"] and item["category"] == "retained"
        assert item["confidence"]["construction_phase_confirmed"] is False


def test_real_classification_is_independent_of_input_order(v9):
    facts, review, result = v9
    changed, changed_review = deepcopy(facts), deepcopy(review)
    changed["features"].reverse()
    changed["boundary_edges"].reverse()
    changed_review["confirmed_outlines"].reverse()
    changed_review["axis_reference_ids"].reverse()
    reordered = refine_spatial_facts(changed, changed_review)
    assert _items(reordered) == _items(result)
    assert reordered["summary"] == result["summary"]
    assert reordered["axis_frame"]["axis_deg"] == pytest.approx(result["axis_frame"]["axis_deg"], abs=1e-10)
    assert {edge["id"] for edge in reordered["axis_frame"]["boundary_support"]} == {
        edge["id"] for edge in result["axis_frame"]["boundary_support"]
    }


def test_complete_footprints_distinguish_occupancy_unknowns_and_crane_coverage(v9):
    _, _, result = v9
    items = _items(result)
    inside_factory = mapping(shape(items["1204/11FA"]["geometry"]).representative_point().buffer(.1))
    for role in ("stockyard", "road"):
        report = assess_footprint_constraints(inside_factory, result, role=role)
        assert report["status"] == "intersects_reserved_footprint"
        assert "1204/11FA" in {entry["id"] for entry in report["reserved_intersections"]}
        assert report["site_feasible"] is None
    coverage = assess_footprint_constraints(inside_factory, result, role="coverage_circle")
    assert coverage["status"] == "coverage_needs_separate_checks"
    assert coverage["site_feasible"] is None
    occupied = unary_union([shape(item["geometry"]) for item in items.values() if item["category"] == "retained"])
    unreserved_part = shape(items["15CF"]["geometry"]).difference(occupied.buffer(1))
    assert not unreserved_part.is_empty
    uncertain_footprint = mapping(unreserved_part.representative_point().buffer(.001))
    report = assess_footprint_constraints(uncertain_footprint, result, role="road")
    assert report["status"] == "requires_review"
    assert "15CF" in {entry["id"] for entry in report["uncertain_intersections"]}
    assert report["site_feasible"] is None


def test_actual_partial_overlap_cannot_be_suppressed_as_contained_noise(v9):
    """Counterexample: a translated factory has a real outside tail, not CAD noise."""
    facts, review, _ = v9
    changed = deepcopy(facts)
    parent = next(feature for feature in changed["features"] if feature["id"] == "1204/11FA")
    partial = deepcopy(parent)
    partial["id"] = "counterexample:partial-overlap"
    partial["geometry"] = mapping(affinity.translate(shape(parent["geometry"]), xoff=10))
    partial["properties"]["kind"] = "context_outline"
    changed["features"].append(partial)
    overlap = shape(partial["geometry"]).intersection(shape(parent["geometry"]))
    assert 0 < overlap.area < shape(partial["geometry"]).area
    item = _items(refine_spatial_facts(changed, review))[partial["id"]]
    assert item["category"] == "uncertain"
    assert item["covered_by"] == []
    assert "1204/11FA" in item["conflicts_with"]
    assert "partial_overlap_reviewed_outline" in item["reason_codes"]


def test_orthogonal_frame_mean_wraps_at_ninety_degrees_instead_of_forty_five():
    """Counterexample: 89.7° and 0.3° share an axis family around 0°, not 45°."""
    features = []
    for index, angle in enumerate((89.7, .3)):
        geometry = mapping(affinity.rotate(box(0, 0, 20, 5), angle, origin=(0, 0)))
        features.append({"id": f"anchor:{index}", "geometry": geometry,
                         "properties": {"geometry_status": "valid_reference_boundary", "kind": "building_candidate"}})
    facts = {"source": {"sha256": "counterexample"}, "features": features, "scope": {"origin": [0, 0]},
             "boundary_edges": [{"id": "site:edge:1", "boundary_id": "site",
                                 "length_native": 30, "angle_deg": 0, "start": [0, 0], "end": [30, 0]}]}
    review = {"source_sha256": "counterexample",
              "confirmed_outlines": [{"id": feature["id"], "geometry_sha256": geometry_digest(feature["geometry"])}
                                     for feature in features],
              "axis_reference_ids": [feature["id"] for feature in features]}
    result = derive_axis_frame(facts, review)
    assert result["status"] == "supported"
    assert axis_distance(result["axis_deg"], 0) == pytest.approx(0, abs=1e-9)
    assert axis_distance(89.7, .3) == pytest.approx(.6, abs=1e-9)
    assert len(result["boundary_support"]) == 1
