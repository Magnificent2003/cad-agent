"""Small contract checks for allocation decisions, not a substitute for the real CAD trial."""
from cad_domain.site_program import build_program


def test_explicit_area_overrides_estimate_without_renormalizing_other_facilities():
    program = build_program(10000, 2500, overrides={
        "facilities": {"rebar": {"area_m2": 850, "preferences": {"access": "entry"}},
                       "office": {"enabled": False}},
        "road": {"width_m": 8},
        "planning": {"grid_step_m": 2.5, "site_edge_clearance_m": 3, "uncertain_keepouts": "none",
                     "uncertain_excluded_layer_patterns": ["^CAR$", "停车"]},
    })
    facilities = {facility["id"]: facility for facility in program["facilities"]}
    assert facilities["rebar"]["target_area_m2"] == 850
    assert facilities["rebar"]["area_source"] == "explicit_user_area"
    assert facilities["office"]["target_area_m2"] == 0
    assert facilities["formwork"]["target_area_m2"] == 300
    assert program["road"]["width_m"] == 8
    assert program["road"]["width_source"] == "user_override"
    assert program["budget"]["fractions_normalized"] is False
    assert program["existing_buildings"]["reference_is_target"] is False
    assert program["planning"]["grid_step_m"] == 2.5
    assert program["planning"]["site_edge_clearance_m"] == 3
    assert program["planning"]["candidate_budget"] == 180000
    assert program["planning"]["uncertain_keepouts"] == "none"
    assert program["planning"]["uncertain_excluded_layer_patterns"] == ["^CAR$", "停车"]
    assert facilities["rebar"]["preferences"]["access"] == "entry"
    assert facilities["formwork"]["preferences"]["access"] == "workfaces"
    assert facilities["rebar"]["clearance_m"] == 1  # Independent of the search grid.
    assert program["status"] == "estimated"  # 25% actual occupancy is allowed.


def test_insufficient_area_is_reported_without_shrinking_facility_targets():
    constrained = build_program(10000, 8500)
    spacious = build_program(10000, 1000)
    assert [f["target_area_m2"] for f in constrained["facilities"]] == [
        f["target_area_m2"] for f in spacious["facilities"]
    ]
    assert constrained["status"] == "budget_conflict"
    assert constrained["planning"]["uncertain_keepouts"] == "major_non_annotation"
    assert constrained["existing_buildings"]["measured_area_m2"] == 8500
    issues = {issue["code"]: issue for issue in constrained["issues"]}
    assert issues["facility_area_exceeds_available_area"]["deficit_m2"] == 500
    assert constrained["budget"]["allocation_fraction"] > 1
    # The 20% road estimate remains a soft planning reference, not a draw quota.
    assert constrained["road"]["budget_mode"] == "soft_upper_reference"
    assert constrained["budget"]["geometric_feasibility_proven"] is False


def test_missing_floor_area_falls_back_transparently_and_explicit_floor_input_is_used():
    missing = build_program(10000, 4000, overrides={"area_basis": "building_floor_area"})
    supplied = build_program(10000, 4000, overrides={
        "area_basis": "building_floor_area", "building_floor_area_m2": 20000,
    })
    assert missing["facilities"][0]["target_area_m2"] == 500
    assert supplied["facilities"][0]["target_area_m2"] == 1000
    assert missing["facilities"][0]["area_calculation"]["basis"] == "site_area"
    assert any(issue["code"] == "area_basis_fallback" for issue in missing["issues"])
    assert not any(issue["code"] == "area_basis_fallback" for issue in supplied["issues"])
    assert set(f["id"] for f in supplied["facilities"]) == {
        "rebar", "formwork", "turnover", "masonry", "office", "equipment",
    }
