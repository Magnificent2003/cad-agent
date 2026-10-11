"""Expose configurable area estimates, never project layouts, to the CAD agent."""
from __future__ import annotations

import hashlib
from shapely.geometry import shape
from shapely.ops import unary_union

from .site_program import DEFAULT_RULES, build_program, load_rules
from .spatial_constraints import evaluate_constraints
from .spatial_tools import _load


def site_rules(context, params):
    rules = load_rules()
    keep = ("schema_version", "source", "description", "area_basis", "fallback_area_basis",
            "existing_buildings", "road", "reserve", "facility_defaults", "facilities",
            "area_policy", "excluded_this_round", "limits")
    report = {"status": "rules_only", "rules": {key: rules[key] for key in keep if key in rules},
              "rules_sha256": hashlib.sha256(DEFAULT_RULES.read_bytes()).hexdigest(),
              "rules_location": "cad_domain/site_layout_rules.yaml",
              "scope": "配置中的比例是用户提供的概念估算，数值可调整；不是规范定额，不提供道路路径或布置坐标。",
              "cad_written": False}
    facts_id = params.get("facts_id") or ""
    if not facts_id:
        if params.get("overrides"):
            raise ValueError("计算覆盖参数后的面积任务书需要当前facts_id；只读规则可省略。")
        return report
    analysis = _load(context, facts_id)
    observation = evaluate_constraints(analysis)
    report.update(facts_id=facts_id, source_sha256=analysis["source"]["sha256"])
    if not observation["frame"]["ready"] or not observation["boundary"].get("geometry_wcs"):
        report.update(status="area_basis_unresolved", issues=observation["issues"],
                      next_step="先解决当前图纸的工作单位或场界证据；不猜测面积或采用历史图纸。")
        return report
    boundary = shape(observation["boundary"]["geometry_wcs"])
    scale = observation["frame"]["metres_per_unit"]
    classified = analysis["classifications"]
    def merged(predicate):
        return unary_union([shape(item["geometry"]).intersection(boundary)
                            for item in classified if predicate(item)])
    reserved = merged(lambda item: item["category"] == "retained")
    named = merged(lambda item: item.get("machine_supported") is True)
    uncertain = merged(lambda item: item["category"] == "uncertain" and item.get("display_priority") != "minor")
    site_area = boundary.area * scale**2
    program = build_program(site_area, reserved.area * scale**2,
                            overrides=params.get("overrides"), rules=rules)
    # An MCP argument proves what the caller supplied, not who authorized it.
    for facility in program.get("facilities", []):
        if facility.get("area_source") == "explicit_user_area":
            facility["area_source"] = "explicit_caller_area"
        if facility.get("clearance_source") == "user_override":
            facility["clearance_source"] = "caller_override"
        facility["area_policy"] = "调整必须保留依据与差额；工具参数不证明用户明确提供或批准。"
    if program.get("road", {}).get("width_source") == "user_override":
        program["road"]["width_source"] = "caller_override"
    program["override_provenance"] = {
        "caller": "agent", "values": params.get("overrides") or {},
        "user_authorization_inferred": False,
        "meaning": "覆盖值由本次工具调用提供；来自用户明确要求还是Agent自主假设，须对照任务说明。"}
    # Avoid calling provisional occupied area "confirmed existing buildings".
    occupancy = program.pop("existing_buildings")
    occupancy["meaning"] = "机器支持轮廓和几何保留候选的场界内并集；不是已确认建筑面积，也不是总建筑面积。"
    program["reserved_occupancy"] = occupancy
    program.pop("planning", None)
    program.pop("planning_overrides", None)
    program.pop("planning_source", None)
    report.update(status=program["status"], program=program,
                  spatial_area_measurements={
                      "boundary_area_m2": site_area,
                      "machine_supported_footprints_m2": named.area * scale**2,
                      "retained_union_m2": reserved.area * scale**2,
                      "major_uncertain_additional_m2": uncertain.difference(reserved).area * scale**2,
                      "after_retained_and_major_uncertain_m2": boundary.difference(reserved.union(uncertain)).area * scale**2,
                      "remaining_area_is_certified_free_space": False},
                  next_step="按目标面积自主选择尺寸、分区和道路；占地只是面积预算，须检查完整空间几何。比例可依任务调整并说明依据，不能为了通过检查隐瞒缩小或遗漏设施。")
    return report
