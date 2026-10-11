"""Real v0009 evidence checks; private engineering data is optional in CI.

The expected handles/areas are independent review references for this fixture,
not an identification recipe embedded in the production algorithm.
"""
from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from cad_domain.spatial_extract import extract_drawing
from cad_domain.spatial_facts import construct_facts


V9 = Path(__file__).resolve().parents[1] / (
    "data/agent-tasks/786b4ced67ef4d1198bfe8bdf0359aee/input/drawing.dxf"
)
V9_SHA256 = "20f23308dd1246ed6e8e60549b569ecce0408b166b76c153f3844f3a7ec906db"
REVIEWED_BUILDINGS = {
    "7": ("1204/11FA", 5644.8),
    "8": ("11EE/11FA", 5644.8),
    "9": ("127A/127F", 3528.0),
    "10": ("1253/1258", 2822.4),
}


@pytest.fixture(scope="module")
def v9():
    if not V9.exists():
        pytest.skip("Private v0009 engineering drawing is not present")
    before = hashlib.sha256(V9.read_bytes()).hexdigest()
    assert before == V9_SHA256, "The review reference must use exactly the reviewed v0009 bytes"
    extracted = extract_drawing(V9)
    facts = construct_facts(extracted)
    assert hashlib.sha256(V9.read_bytes()).hexdigest() == before
    return extracted, facts


def _primary(facts):
    return {feature["properties"]["number"]: feature for feature in facts["features"]
            if feature["properties"].get("primary")}


def test_real_buildings_have_independent_geometry_evidence_without_engineering_approval(v9):
    _, facts = v9
    primary = _primary(facts)
    for number, (source_id, area) in REVIEWED_BUILDINGS.items():
        feature = primary[number]
        properties = feature["properties"]
        assert feature["id"] == source_id
        assert properties["area_native"] == pytest.approx(area, abs=1e-5)
        assert properties["long_edge_angle_deg"] == pytest.approx(134.266211, abs=1e-4)
        assert properties["axis_deg"] == pytest.approx(44.266211, abs=1e-4)
        assert properties["status"] == "supported"
        assert properties["role_status"] == "pending_engineering_confirmation"
        assert properties["phase"] == "unknown"
        assert {item["kind"] for item in properties["evidence"]} >= {
            "contained_number", "contained_name", "table_area",
        }
    assert facts["source"]["sha256"] == V9_SHA256
    assert facts["units"]["status"] == "conflict"
    assert facts["units"]["declared_metres_per_unit"] == .001
    assert facts["units"]["candidate_metres_per_unit"] == pytest.approx(1, abs=1e-8)
    assert facts["units"]["confirmed"] is False
    assert facts["scope"]["free_space_certified"] is False
    assert facts["summary"]["engineering_confirmed_buildings"] == 0


def test_real_area_conflict_and_unlocated_table_buildings_remain_unresolved(v9):
    _, facts = v9
    primary = _primary(facts)
    office = primary["11"]
    assert office["id"] == "1233/1238"
    assert office["properties"]["status"] == "candidate"
    assert office["properties"]["area_native"] == pytest.approx(1172.409219668, abs=1e-6)
    assert office["properties"]["area_relative_error"] == pytest.approx(.0029678338, abs=1e-8)
    conflicts = [issue for issue in facts["unresolved"] if issue["code"] == "building_area_conflict"]
    assert any(office["id"] in issue["source_ids"] for issue in conflicts)
    assert "3" not in primary and "4" not in primary
    not_located = {source for issue in facts["unresolved"] if issue["code"] == "table_building_not_located"
                   for source in issue["source_ids"]}
    assert {"16/1CB", "16/1D0"} <= not_located
    assert any(issue["code"] == "scene_coverage_incomplete" for issue in facts["unresolved"])
    relation = next(r for r in facts["spatial_relations"] if r["building_id"] == office["id"])
    assert relation["outside_area_native2"] == pytest.approx(503.68658215, abs=1e-5)
    assert relation["outside_fraction"] == pytest.approx(.4296167018, abs=1e-8)
    assert relation["status"] == "geometry_only_not_engineering_violation"
    assert facts["site_area_table"][0]["area_m2"] == 43214
    assert any(issue["code"] == "boundary_area_disagreement" for issue in facts["unresolved"])


def test_mask_removal_and_input_reordering_do_not_remove_or_reidentify_buildings(v9):
    extracted, baseline = v9
    changed = deepcopy(extracted)
    changed["wipeouts"] = []
    changed["polygons"].reverse()
    changed["texts"].reverse()
    revised = construct_facts(changed)
    old, new = _primary(baseline), _primary(revised)
    assert set(new) == set(old)
    for number in REVIEWED_BUILDINGS:
        assert new[number]["id"] == old[number]["id"]
        assert new[number]["geometry"] == old[number]["geometry"]
        assert old[number]["properties"]["occluded_by"]
        assert new[number]["properties"]["occluded_by"] == []
        assert new[number]["properties"]["status"] == old[number]["properties"]["status"]
    assert revised["scope"]["free_space_certified"] is False


def test_removing_occupancy_area_headers_invalidates_area_support(v9):
    extracted, baseline = v9
    headers = {row["header"]["id"] for row in baseline["area_table"]}
    assert headers  # This test removes actual column evidence, not arbitrary nearby text.
    changed = deepcopy(extracted)
    changed["texts"] = [text for text in changed["texts"] if text["id"] not in headers]
    revised = construct_facts(changed)
    assert revised["area_table"] == []
    assert revised["units"]["candidate_metres_per_unit"] is None
    assert revised["units"]["confirmed"] is False
    assert not any(feature["properties"]["status"] == "supported" for feature in revised["features"])
    assert revised["scope"]["free_space_certified"] is False
    assert any(issue["code"] == "units_unresolved" for issue in revised["unresolved"])


def test_real_geometry_does_not_receive_support_from_conflicting_names(v9):
    extracted, baseline = v9
    name_ids = {item["id"] for feature in _primary(baseline).values()
                if feature["properties"]["number"] in REVIEWED_BUILDINGS
                for item in feature["properties"]["evidence"] if item["kind"] == "contained_name"}
    changed = deepcopy(extracted)
    for text in changed["texts"]:
        if text["id"] in name_ids:
            text["text"] = "办公楼"  # Conflicts with table's 标准厂房; unchanged actual geometry/areas.
    revised = construct_facts(changed)
    assert revised["summary"]["area_supported_buildings"] == 0
    assert revised["units"]["candidate_metres_per_unit"] is None
    assert any(issue["code"] == "building_name_conflict" for issue in revised["unresolved"])
