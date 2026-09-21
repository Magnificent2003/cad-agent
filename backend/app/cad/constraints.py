"""Explicit project/demo geometric checks; never manufacture normative limits."""

from __future__ import annotations

from itertools import combinations
from typing import Any, Literal

from pydantic import BaseModel, Field
from shapely.geometry import LineString, Polygon

from .errors import CadError
from .reader import finite, load, units


class ClauseEvidence(BaseModel):
    evidence_id: str
    standard_id: str
    edition: str
    clause_id: str
    text: str
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    scope: dict[str, Any] = Field(default_factory=dict)
    effective_status: str
    source_locator: str


class ConstraintSpec(BaseModel):
    constraint_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    targets: list[str]
    predicate: Literal["within_site", "no_overlap", "min_distance"]
    parameters: dict[str, Any] = Field(default_factory=dict)
    unit: str | None = None
    applies_if: bool | dict[str, Any] = True
    enforcement: Literal["hard", "soft"] = "hard"
    compile_status: Literal["ready", "needs_review"] = "ready"
    source_kind: Literal["project_requirement", "demo_rule", "clause"] = "project_requirement"


class CheckResult(BaseModel):
    constraint_id: str
    revision_id: str
    entity_ids: list[str]
    status: Literal["pass", "fail", "unknown", "not_applicable"]
    measured_value: Any = None
    unit: str | None = None
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)


def entity_geometry(entity):
    kind = entity.dxftype()
    if kind == "LINE":
        start, end = entity.dxf.start, entity.dxf.end
        if start.z != 0 or end.z != 0:
            raise CadError("Geometry is not on the XY plane.", "unknown_geometry")
        geometry = LineString([(start.x, start.y), (end.x, end.y)])
    elif kind == "LWPOLYLINE":
        if tuple(entity.dxf.extrusion) != (0, 0, 1) or entity.dxf.elevation != 0:
            raise CadError("Polyline coordinates require a nontrivial OCS transform.", "unknown_geometry")
        vertices = list(entity.get_points("xyb"))
        if any(value[2] for value in vertices):
            raise CadError("Arc segments need an explicitly approved discretization tolerance.", "unknown_geometry")
        points = [(value[0], value[1]) for value in vertices]
        geometry = Polygon(points) if entity.closed else LineString(points)
    else:
        raise CadError("Target type has no supported exact 2D geometry.", "unknown_geometry")
    if geometry.is_empty or not geometry.is_valid:
        raise CadError("Target geometry is empty or invalid.", "unknown_geometry")
    return geometry


def check_drawing(source: str, constraints: list[dict], revision_id: str = "") -> dict:
    from pydantic import ValidationError
    if not isinstance(constraints, list) or len(constraints) > 100:
        raise CadError("Provide at most 100 explicit constraints.", "invalid_constraints")
    doc = load(source)
    drawing_units = units(doc)
    direct = {entity.dxf.handle: entity for layout in doc.layouts for entity in layout}
    results = []
    for raw in constraints:
        try:
            spec = ConstraintSpec.model_validate(raw)
        except ValidationError as exc:
            raise CadError("Invalid constraint specification.", "invalid_constraints") from exc
        result = CheckResult(constraint_id=spec.constraint_id, revision_id=revision_id, entity_ids=spec.targets, status="unknown", unit=spec.unit, reason="Not evaluated.", evidence_ids=spec.evidence_ids)
        if spec.applies_if is False:
            result.status, result.reason = "not_applicable", "Explicit applicability condition is false."
        elif spec.compile_status != "ready" or spec.applies_if is not True:
            result.reason = "Constraint compilation or applicability needs review."
        elif spec.source_kind == "clause" and not spec.evidence_ids:
            result.reason = "A clause-based constraint requires evidence identifiers."
        elif not drawing_units["confirmed"] or not spec.unit:
            result.reason = "Drawing and constraint units must be explicitly known."
        elif spec.unit != drawing_units["name"]:
            result.reason = "Constraint unit differs from native drawing units."
        elif not spec.targets:
            result.reason = "Explicit target handles are required."
        else:
            try:
                geometries = [entity_geometry(direct[handle.upper()]) for handle in spec.targets]
                if spec.predicate == "within_site":
                    site_handle = spec.parameters.get("site_handle")
                    if not site_handle:
                        raise CadError("within_site requires parameters.site_handle.")
                    site = entity_geometry(direct[str(site_handle).upper()])
                    if not isinstance(site, Polygon) or not all(isinstance(shape, Polygon) for shape in geometries):
                        raise CadError("Site and footprints must be closed polygons; a centerline lacks footprint width.")
                    outside_area = sum(shape.difference(site).area for shape in geometries)
                    passed = all(site.covers(shape) for shape in geometries)
                    result.measured_value = {"outside_area": outside_area, "area_unit": f"{spec.unit}²"}
                elif spec.predicate == "no_overlap":
                    if len(geometries) < 2 or not all(isinstance(shape, Polygon) for shape in geometries):
                        raise CadError("no_overlap requires two or more closed footprints.")
                    overlaps = [first.intersection(second).area for first, second in combinations(geometries, 2)]
                    passed = all(area == 0 for area in overlaps)
                    result.measured_value = {"maximum_overlap_area": max(overlaps), "area_unit": f"{spec.unit}²"}
                else:
                    if len(geometries) < 2 or "minimum" not in spec.parameters:
                        raise CadError("min_distance requires two targets and parameters.minimum.")
                    minimum = finite(spec.parameters["minimum"], "minimum distance")
                    if minimum < 0:
                        raise CadError("Minimum distance must be nonnegative.")
                    measured = min(first.distance(second) for first, second in combinations(geometries, 2))
                    passed = measured >= minimum
                    result.measured_value = measured
                result.status = "pass" if passed else "fail"
                result.reason = f"Evaluated explicit {spec.source_kind}; this is not an overall regulatory compliance decision."
            except (CadError, KeyError, ValueError, TypeError) as exc:
                result.reason = "A target handle was not found in this revision." if isinstance(exc, KeyError) else str(exc)
        results.append(result.model_dump())
    return {"revision_id": revision_id, "checks": results, "units": drawing_units, "rag_status": "not_configured", "regulatory_review": "not_run"}


check_geometry = check_drawing
