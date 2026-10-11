"""Transparent concept-layout budgets; estimates are not statutory design limits.

No CAD coordinates or project object IDs belong here. The caller supplies the
measured site and occupied union areas in square metres. This module does not
scale existing buildings, normalize the allocation percentages, or reduce a
facility target when the site is too small.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import re

DEFAULT_RULES = Path(__file__).with_name("site_layout_rules.yaml")


def _number(value, name, *, minimum=0.0, positive=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(value) or value < minimum or (positive and value <= 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else f'>= {minimum}'}")
    return value


def _fraction(value, name):
    value = _number(value, name)
    if value > 1:
        raise ValueError(f"{name} must be a fraction between 0 and 1, not a percentage")
    return value


def _merge(base, override):
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_rules(path=None):
    """Load JSON-compatible YAML by default; accept ordinary YAML if available."""
    text = Path(path or DEFAULT_RULES).read_text(encoding="utf-8")
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml
        except ImportError:
            raise ValueError("Ordinary YAML requires PyYAML; the supplied rules use dependency-free JSON YAML") from None
        result = yaml.safe_load(text)
    if not isinstance(result, dict) or result.get("schema_version") != "site-layout-rules/0.1":
        raise ValueError("Expected a site-layout-rules/0.1 mapping")
    facilities = result.get("facilities")
    if not isinstance(facilities, list) or not facilities:
        raise ValueError("rules.facilities must be a non-empty list")
    identities = [item.get("id") for item in facilities if isinstance(item, dict)]
    if len(identities) != len(facilities) or not all(isinstance(i, str) and i for i in identities):
        raise ValueError("Each facility needs a non-empty string id")
    if len(identities) != len(set(identities)):
        raise ValueError("Facility ids must be unique")
    return result


def build_program(site_area_m2, occupied_area_m2, overrides=None, rules=None):
    """Build unshrunk facility targets and an explicit, non-normalized budget.

    Overrides example::

        {"facilities": {"rebar": {"area_m2": 900},
                         "office": {"site_area_fraction": .03}},
         "road": {"width_m": 6}, "reserve": {"selected_fraction": .05}}

    Each facility may also override ``shape``, ``split``, ``preferences`` and
    ``clearance_m`` (a clear distance, not a search-grid interval). ``planning``
    overrides numerical search parameters independently of facility clearances.
    ``area_basis='building_floor_area'`` with ``building_floor_area_m2`` is a
    future input route, not an inferred floor area; missing inputs fall back
    transparently to the user-authorized site-area heuristic.
    """
    rules = deepcopy(rules if rules is not None else load_rules())
    overrides = deepcopy(overrides or {})
    if not isinstance(overrides, dict):
        raise ValueError("overrides must be a mapping")
    allowed = {"facilities", "road", "reserve", "area_basis", "building_floor_area_m2", "planning"}
    if set(overrides) - allowed:
        raise ValueError(f"Unknown program overrides: {sorted(set(overrides) - allowed)}")
    site_area = _number(site_area_m2, "site_area_m2", positive=True)
    occupied_area = _number(occupied_area_m2, "occupied_area_m2")
    if occupied_area > site_area * (1 + 1e-9):
        raise ValueError("occupied_area_m2 must be the obstacle union clipped to the site boundary")
    by_id = overrides.get("facilities", {})
    if not isinstance(by_id, dict):
        raise ValueError("overrides.facilities must be a mapping keyed by facility id")
    known = {spec["id"] for spec in rules["facilities"]}
    if set(by_id) - known:
        raise ValueError(f"Unknown facility ids: {sorted(set(by_id) - known)}")
    global_basis = overrides.get("area_basis", rules.get("area_basis", "site_area"))
    floor_area = overrides.get("building_floor_area_m2")
    if floor_area is not None:
        floor_area = _number(floor_area, "building_floor_area_m2", positive=True)
    planning_overrides = overrides.get("planning", {})
    if not isinstance(planning_overrides, dict):
        raise ValueError("overrides.planning must be a mapping")
    if set(planning_overrides) - set(rules.get("planning", {})):
        raise ValueError(f"Unknown planning override fields: {sorted(set(planning_overrides) - set(rules.get('planning', {})))}")
    planning = _merge(rules.get("planning", {}), planning_overrides)
    for key in ("grid_step_m", "road_grid_step_m", "frontage_max_gap_m"):
        planning[key] = _number(planning[key], f"planning.{key}", positive=True)
    for key in ("site_edge_clearance_m", "obstacle_clearance_m"):
        planning[key] = _number(planning[key], f"planning.{key}")
    for key in ("candidate_budget", "aspect_ratio_samples", "placement_order_trials", "access_candidate_limit"):
        value = planning[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"planning.{key} must be a positive integer")
    if planning["placement_order_trials"] > 3:
        raise ValueError("planning.placement_order_trials supports at most three distinct orders")
    if planning.get("uncertain_keepouts") not in {"major_non_annotation", "none"}:
        raise ValueError("planning.uncertain_keepouts must be major_non_annotation or none")
    patterns = planning.get("uncertain_excluded_layer_patterns")
    if not isinstance(patterns, list) or not all(isinstance(pattern, str) and pattern for pattern in patterns):
        raise ValueError("planning.uncertain_excluded_layer_patterns must be a list of non-empty regex strings")
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error:
            raise ValueError(f"Invalid uncertain layer pattern: {pattern!r}") from None
    facilities, issues = [], []
    for configured in rules["facilities"]:
        identity = configured["id"]
        supplied = by_id.get(identity, {})
        if not isinstance(supplied, dict):
            raise ValueError(f"facilities.{identity} must be a mapping")
        allowed_facility = {"area_m2", "enabled", "area_basis", "site_area_fraction", "area_fraction",
                            "shape", "split", "clearance_m", "preferences"}
        if set(supplied) - allowed_facility:
            raise ValueError(f"Unknown facility override fields for {identity}: {sorted(set(supplied) - allowed_facility)}")
        spec = _merge(_merge(rules.get("facility_defaults", {}), configured), supplied)
        enabled = spec.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"{identity}.enabled must be true or false")
        ratio = _fraction(spec.get("area_fraction", spec["site_area_fraction"]), f"{identity}.area_fraction")
        requested_basis = spec.get("area_basis", global_basis)
        if requested_basis not in {"site_area", "building_floor_area"}:
            raise ValueError(f"Unsupported area basis: {requested_basis}")
        basis, base_area = requested_basis, site_area
        if basis == "building_floor_area" and floor_area is not None:
            base_area = floor_area
        elif basis == "building_floor_area":
            basis = "site_area"
            issues.append({"code": "area_basis_fallback", "facility_id": identity,
                           "message": "No building floor area supplied; using the configured site-area heuristic.",
                           "requested_basis": requested_basis, "used_basis": basis})
        if not enabled:
            area, source = 0.0, "explicitly_disabled"
        elif "area_m2" in supplied:
            area = _number(supplied["area_m2"], f"{identity}.area_m2", positive=True)
            source = "explicit_user_area"
        else:
            area, source = base_area * ratio, "user_provided_heuristic"
            if area <= 0:
                raise ValueError(f"{identity} is enabled but has zero area; use enabled=false to omit it")
        shape = deepcopy(spec["shape"])
        bounds = shape.get("aspect_ratio_range")
        if shape.get("kind") != "rectangle" or not isinstance(bounds, list) or len(bounds) != 2:
            raise ValueError(f"{identity}.shape must declare a rectangle and two aspect-ratio bounds")
        low, high = [_number(v, f"{identity}.aspect_ratio", minimum=1) for v in bounds]
        preferred = _number(shape.get("preferred_aspect_ratio", low), f"{identity}.preferred_aspect_ratio", minimum=1)
        if low > high or not low <= preferred <= high:
            raise ValueError(f"{identity}.preferred_aspect_ratio must lie within its ordered bounds")
        shape.update(aspect_ratio_range=[low, high], preferred_aspect_ratio=preferred)
        split = deepcopy(spec["split"])
        if not isinstance(split.get("allowed"), bool):
            raise ValueError(f"{identity}.split.allowed must be true or false")
        max_parts = split.get("max_parts")
        if isinstance(max_parts, bool) or not isinstance(max_parts, int) or max_parts < 1:
            raise ValueError(f"{identity}.split.max_parts must be a positive integer")
        split["min_part_area_m2"] = _number(split.get("min_part_area_m2", 0), f"{identity}.min_part_area_m2")
        if not split["allowed"]:
            split["max_parts"] = 1
        clearance = _number(spec.get("clearance_m", 0), f"{identity}.clearance_m")
        preferences = deepcopy(spec.get("preferences", {}))
        if not isinstance(preferences, dict) or preferences.get("access") not in {"entry", "workfaces"}:
            raise ValueError(f"{identity}.preferences.access must be entry or workfaces")
        facilities.append({"id": identity, "name": spec["name"], "enabled": enabled,
                           "target_area_m2": area, "area_source": source,
                           "area_calculation": {"requested_basis": requested_basis, "basis": basis,
                                                "basis_area_m2": base_area, "fraction": ratio,
                                                "explicit_area_m2": supplied.get("area_m2"),
                                                "formula_applied": source == "user_provided_heuristic"},
                           "shape": shape, "split": split, "clearance_m": clearance, "preferences": preferences,
                           "clearance_source": "user_override" if "clearance_m" in supplied else spec.get("clearance_source"),
                           "area_policy": "do_not_reduce_without_recorded_user_override"})
    road_overrides = overrides.get("road", {})
    reserve_overrides = overrides.get("reserve", {})
    if not isinstance(road_overrides, dict) or not isinstance(reserve_overrides, dict):
        raise ValueError("road and reserve overrides must be mappings")
    if set(road_overrides) - {"width_m", "site_area_fraction"}:
        raise ValueError("Road overrides support width_m and site_area_fraction")
    if set(reserve_overrides) - {"selected_fraction"}:
        raise ValueError("Reserve overrides support selected_fraction")
    road = _merge(rules["road"], road_overrides)
    road["site_area_fraction"] = _fraction(road["site_area_fraction"], "road.site_area_fraction")
    road["width_m"] = _number(road["width_m"], "road.width_m", positive=True)
    road["target_area_m2"] = site_area * road["site_area_fraction"]
    if "width_m" in road_overrides:
        road["width_source"] = "user_override"
    reserve = _merge(rules["reserve"], reserve_overrides)
    reserve["selected_fraction"] = _fraction(reserve["selected_fraction"], "reserve.selected_fraction")
    reserve["target_area_m2"] = site_area * reserve["selected_fraction"]
    facility_area = sum(f["target_area_m2"] for f in facilities)
    free_area = max(0.0, site_area - occupied_area)
    requested_area = facility_area + road["target_area_m2"] + reserve["target_area_m2"]
    balance = free_area - requested_area
    if facility_area > free_area + 1e-8:
        issues.append({"code": "facility_area_exceeds_available_area", "severity": "conflict",
                       "deficit_m2": facility_area - free_area,
                       "message": "Facility target areas alone exceed available area; targets have not been reduced."})
    if balance < -1e-8:
        issues.append({"code": "soft_budget_exceeds_available_area", "severity": "warning",
                       "deficit_m2": -balance,
                       "message": "Facilities plus soft road/reserve allowances exceed available area; do not normalize ratios or shrink facilities."})
    return {"schema_version": "site-program/0.1", "rules_source": rules.get("source"),
            "status": "budget_conflict" if any(i.get("severity") == "conflict" for i in issues) else "estimated",
            "facilities": facilities, "road": road, "reserve": reserve,
            "planning": planning, "planning_source": rules.get("planning_source"),
            "planning_overrides": planning_overrides,
            "existing_buildings": {"measured_area_m2": occupied_area, "measured_fraction": occupied_area / site_area,
                                   "reference_fraction_range": rules["existing_buildings"]["reference_fraction_range"],
                                   "reference_is_target": False},
            "budget": {"site_area_m2": site_area, "occupied_area_m2": occupied_area,
                       "available_area_m2": free_area, "facility_target_area_m2": facility_area,
                       "road_soft_area_m2": road["target_area_m2"], "reserve_soft_area_m2": reserve["target_area_m2"],
                       "allocation_including_occupancy_m2": occupied_area + requested_area,
                       "allocation_fraction": (occupied_area + requested_area) / site_area,
                       "remaining_after_soft_allowances_m2": balance,
                       "fractions_normalized": False, "geometric_feasibility_proven": False},
            "issues": issues, "excluded_this_round": rules.get("excluded_this_round", []),
            "limits": rules.get("limits", [])}
