"""Part B constraint observations for source-bound Part A analysis.

This module does not plan a layout, write CAD, call a model, or consume a human
review seed. Complete occupied polygons are measured in a shared local metre
frame. Machine-supported and unresolved geometry keep their different meanings.
"""
from __future__ import annotations

from collections import Counter
import math

from shapely import affinity
from shapely.errors import ShapelyError
from shapely.geometry import Polygon, box, mapping, shape

from .tolerances import AREA_TOLERANCE_M2, AXIS_TOLERANCE_DEG, LINEAR_TOLERANCE_M

MAX_PROPOSALS = 50
MAX_VERTICES = 256
COVERAGE_ROLES = {"coverage", "coverage_circle", "tower_coverage"}
OCCUPIED_ROLES = {"facility", "road", "stockyard", "tower_base", "hoist_base"}


def _number(value, field, *, positive=False, nonnegative=False):
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须为有限数值，不能使用布尔值")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0) or (nonnegative and result < 0):
        raise ValueError(f"{field} 必须为有限数值，并满足正数或非负数要求")
    return result


def _point(value, field):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{field} 必须包含两个坐标")
    point = tuple(_number(v, field) for v in value)
    if max(abs(v) for v in point) > 1e9:
        raise ValueError(f"{field} 超出数值计算范围 ±1e9")
    return point


def _polygon(value):
    polygon = shape(value)
    if polygon.geom_type != "Polygon" or polygon.is_empty or not polygon.is_valid or polygon.area <= 0:
        raise ValueError("需要有效、面积为正的多边形；不自动修补无效轮廓")
    if not all(math.isfinite(v) for v in polygon.bounds):
        raise ValueError("多边形坐标必须为有限数值")
    return polygon


def _to_local(geometry, origin, angle, scale):
    return affinity.scale(affinity.rotate(affinity.translate(geometry, -origin[0], -origin[1]),
                                         -angle, origin=(0, 0)), scale, scale, origin=(0, 0))


def _to_world(geometry, origin, angle, scale):
    return affinity.translate(affinity.rotate(affinity.scale(geometry, 1 / scale, 1 / scale,
                                                            origin=(0, 0)), angle, origin=(0, 0)), *origin)


def _adopt_units(units):
    """Adopt evidence as an explicit working assumption, never a header rewrite."""
    result = {**units, "adopted_metres_per_unit": None, "adoption_basis": None,
              "engineering_confirmed": False, "header_conflict": units.get("status") == "conflict"}
    selected = units.get("adopted_metres_per_unit")
    if selected is not None:
        result.update(adopted_metres_per_unit=_number(selected, "adopted_metres_per_unit", positive=True),
                      adoption_basis=units.get("adoption_basis", "upstream_working_assumption"))
        return result
    candidate = units.get("candidate_metres_per_unit")
    evidence = units.get("evidence", [])
    declared = units.get("declared_metres_per_unit")
    if candidate is None and declared is not None and units.get("status") != "conflict":
        result.update(adopted_metres_per_unit=_number(declared, "declared_metres_per_unit", positive=True),
                      adoption_basis="declared_header",
                      adoption_note="未得到独立尺度共识；暂按有效DXF单位声明计算，仍是工作假设，并未验证实际设计单位。")
        return result
    if candidate is None or units.get("method") != "independent_numbered_building_table_area_scale_consensus":
        return result
    candidate = _number(candidate, "candidate_metres_per_unit", positive=True)
    compatible = [item for item in evidence if isinstance(item, dict) and item.get("number") is not None
                  and item.get("table_area_m2") is not None and item.get("scale") is not None
                  and math.isclose(_number(item["scale"], "scale evidence", positive=True), candidate, rel_tol=1e-4)]
    numbers = {str(item["number"]) for item in compatible}
    areas = {round(_number(item["table_area_m2"], "table area", positive=True), 4) for item in compatible}
    if len(numbers) >= 3 and len(areas) >= 2:
        result.update(adopted_metres_per_unit=candidate, adoption_basis="drawing_evidence",
                      adoption_note="依据多栋建筑与面积表的一致性采用米制工作尺度；源图单位声明未修改",
                      evidence_building_count=len(numbers))
        declared = units.get("declared_metres_per_unit")
        if declared is not None:
            result["header_conflict"] = not math.isclose(candidate, _number(declared, "declared scale", positive=True), rel_tol=1e-4)
    return result


def _proposal_polygon(proposal):
    if "vertices_m" in proposal:
        if any(key in proposal for key in ("position_m", "width_m", "depth_m", "angle_deg")):
            raise ValueError("vertices_m 已给出完整顶点，不能同时指定矩形位置、尺寸或旋转参数")
        vertices = proposal["vertices_m"]
        if not isinstance(vertices, list) or not 3 <= len(vertices) <= MAX_VERTICES:
            raise ValueError(f"vertices_m 需要3至{MAX_VERTICES}个按边界顺序排列的顶点")
        polygon = Polygon([_point(point, "vertices_m") for point in vertices])
    else:
        position = _point(proposal.get("position_m"), "position_m")
        width = _number(proposal.get("width_m"), "width_m", positive=True)
        depth = _number(proposal.get("depth_m"), "depth_m", positive=True)
        if max(width, depth) > 1e9:
            raise ValueError("矩形尺寸超出数值计算范围")
        angle = _number(proposal.get("angle_deg", 0), "angle_deg")
        polygon = affinity.rotate(box(position[0], position[1], position[0] + width, position[1] + depth),
                                  angle, origin=position)
    return _polygon(mapping(polygon))


def _axis_measurement(polygon, world_angle):
    edges = []
    coordinates = list(polygon.exterior.coords)
    for a, b in zip(coordinates, coordinates[1:]):
        length = math.dist(a, b)
        if length <= LINEAR_TOLERANCE_M:
            continue
        angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180
        mod = angle % 90
        edges.append({"length_m": length, "angle_deg_local": angle,
                      "axis_error_deg": min(mod, 90 - mod)})
    longest = max(edges, key=lambda edge: edge["length_m"])
    return {"long_edge_angle_deg_local": longest["angle_deg_local"],
            "long_edge_angle_deg_wcs": None if world_angle is None else (longest["angle_deg_local"] + world_angle) % 180,
            "maximum_edge_error_deg": max(edge["axis_error_deg"] for edge in edges),
            "long_edge_error_deg": longest["axis_error_deg"], "tolerance_deg": AXIS_TOLERANCE_DEG,
            "edge_count": len(edges), "angle_convention": "局部角度相对选定场地主轴；世界角度从WCS的+X方向逆时针计算"}


def evaluate_constraints(analysis: dict, proposals: list[dict] | None = None) -> dict:
    """Return observations, or check up to 50 complete proposed occupied shapes.

    Rectangles rotate about their local-metre lower-left ``position_m``.
    ``vertices_m`` defines an ordered local-metre polygon instead. Optional
    ``clearance_m`` applies to boundary and occupied footprints; road-to-road
    junctions are allowed. Axis alignment is required for roads and can be
    requested for other shapes with ``require_axis_alignment=True``.
    """
    if not isinstance(analysis, dict):
        raise ValueError("analysis 必须为部分A的分析结果对象")
    if proposals is None:
        proposals = []
    if not isinstance(proposals, list) or len(proposals) > MAX_PROPOSALS:
        raise ValueError(f"proposals 必须为列表，最多{MAX_PROPOSALS}项")
    result = {"schema_version": "spatial-constraints/0.1", "source": analysis.get("source"),
              "status": "constraints_ready", "issues": [], "proposals": [], "pairwise": [],
              "assumptions": list(analysis.get("assumptions", [])),
              "analysis_issues": analysis.get("issues", []), "cad_written": False,
              "engineering_compliance_verified": False,
              "proposal_conventions": {
                  "rectangle_position_m": "position_m 是旋转前矩形左下角，也是旋转锚点；不是中心。",
                  "centroid_m": "placement.centroid_m 来自实际候选多边形的几何质心，单位米。",
                  "world_coordinates": "centroid_wcs 与 corner_points_wcs 由同一实际多边形转换得到，使用原CAD单位；矩形角点为含闭合末点的5点。",
                  "use_returned_measurements": "向用户报告坐标、角点与净距时直接引用工具回传值，不再自行口算或依赖GUI换算。"},
              "limits": ["机器支持的轮廓仍是暂定占地，不代表已经人工确认。",
                         "未识别、曲线和专业对象可能仍有遗漏；未检出冲突不等于空地已获确认。",
                         "尚未核查建筑施工阶段、法定退距、车辆转弯与设备吊装能力。",
                         "覆盖范围不作为实体占地；塔吊基础应另列占地提案。"],
              "tolerances": {"linear_m": LINEAR_TOLERANCE_M, "area_m2": AREA_TOLERANCE_M2,
                             "axis_deg": AXIS_TOLERANCE_DEG}}
    units = _adopt_units(analysis.get("units", {}))
    result["units"] = units
    scale = units["adopted_metres_per_unit"]
    if scale is None:
        result["issues"].append({"code": "working_scale_unresolved", "message": "尚无可采用的米制尺度，暂时不能将局部米制提案映射到原CAD坐标。"})
    elif units.get("adoption_basis") == "declared_header":
        result["assumptions"].append({"code": "working_scale_from_header_only", "value": scale,
                                      "basis": "declared_header", "independent_scale_verified": False})
    elif units["header_conflict"]:
        result["assumptions"].append({"code": "working_scale_overrides_header", "value": scale,
                                      "basis": units["adoption_basis"], "source_header_changed": False})
    frame = analysis.get("axis_frame", {})
    angle = frame.get("axis_deg")
    origin = (frame.get("local_frame") or {}).get("origin_wcs")
    if angle is not None:
        angle = _number(angle, "axis_deg") % 180
    if origin is not None:
        origin = _point(origin, "origin_wcs")
    frame_ready = angle is not None and origin is not None and scale is not None
    if angle is None or origin is None:
        result["issues"].append({"code": "reference_frame_unresolved", "message": "部分A尚未提供有证据支持的主轴及原坐标原点。"})
    result["frame"] = {"status": frame.get("status", "unresolved"), "ready": frame_ready,
                       "axis_deg": angle, "orthogonal_axis_deg": None if angle is None else (angle + 90) % 180,
                       "origin_wcs": origin, "metres_per_unit": scale,
                       "anchor_ids": frame.get("anchor_ids", []), "boundary_support": frame.get("boundary_support", []),
                       "convention": "local_m = R(-axis)*(WCS-origin)*metres_per_unit; source CAD is never rotated"}
    selected = analysis.get("selected_boundary")
    if isinstance(selected, str):
        selected = next((item for item in analysis.get("boundary_candidates", []) if item.get("id") == selected), None)
    result["boundary"] = {"id": selected.get("id") if isinstance(selected, dict) else None,
                          "status": "working_geometry_only" if selected else "unresolved",
                          "candidate_count": len(analysis.get("boundary_candidates", [])),
                          "selection_reason": analysis.get("boundary_selection_reason", analysis.get("selection_reason")), "legal_role_confirmed": False}
    boundary = None
    if selected and frame_ready:
        boundary = _to_local(_polygon(selected["geometry"]), origin, angle, scale)
        result["boundary"].update(area_m2=boundary.area, geometry_local_m=mapping(boundary), geometry_wcs=selected["geometry"])
    if selected is None:
        result["issues"].append({"code": "working_boundary_unresolved", "message": "尚未选出具有依据的唯一工作场界。"})
    counts = Counter()
    obstacles = []
    for item in analysis.get("classifications", []):
        category = item.get("category", "uncertain")
        counts[category] += 1
        if category in {"suppressed", "non_building"}:
            continue
        if category != "retained" and item.get("display_priority") == "minor":
            continue
        tier = "reserved" if category == "retained" else "uncertain"
        descriptor = {"id": item["id"], "name": item.get("label", item["id"]), "tier": tier,
                      "category": category, "machine_supported": bool(item.get("machine_supported", False)),
                      "human_confirmed": bool(item.get("human_confirmed", False)),
                      "semantic_role_confirmed": bool(item.get("confidence", {}).get("semantic_role_confirmed", False)),
                      "constraint_tier": item.get("constraint_tier"), "layer": item.get("layer"),
                      "reason_codes": item.get("reason_codes", [])}
        if not frame_ready:
            obstacles.append((descriptor, None))
            continue
        try:
            polygon = _to_local(_polygon(item["geometry"]), origin, angle, scale)
        except (ValueError, KeyError, TypeError, ShapelyError) as exc:
            result["issues"].append({"code": "obstacle_geometry_unresolved", "id": item["id"], "message": str(exc)})
            obstacles.append((descriptor, None))
            continue
        descriptor.update(area_m2=polygon.area, centroid_local_m=list(polygon.centroid.coords)[0], bounds_local_m=list(polygon.bounds))
        obstacles.append((descriptor, polygon))
    result["obstacles_summary"] = {"classification_counts": dict(counts), "independent_constraints": len(obstacles),
                                    "reserved_count": sum(item["tier"] == "reserved" for item, _ in obstacles),
                                    "uncertain_count": sum(item["tier"] == "uncertain" for item, _ in obstacles),
                                    "items": [item for item, _ in obstacles],
                                    "suppressed_policy": "由父轮廓代表，不删除源实体",
                                    "minor_uncertain_policy": "不直接升级为建筑，仍不能宣称场景已完整识别"}
    parsed = []
    ids = set()
    for proposal in proposals:
        output = {"id": proposal.get("id") if isinstance(proposal, dict) else None, "status": "no_detected_conflict",
                  "conflicts": [], "unresolved": [], "intersections": [], "distances": [], "site_feasible": None}
        result["proposals"].append(output)
        try:
            if not isinstance(proposal, dict):
                raise ValueError("每个提案必须为对象")
            identity = proposal.get("id")
            if not isinstance(identity, str) or not identity.strip() or len(identity) > 120 or identity in ids:
                raise ValueError("提案id必须是唯一非空字符串，长度不超过120字符")
            ids.add(identity)
            role = proposal.get("role", "facility")
            output.update(name=proposal.get("name", identity), role=role)
            if role not in OCCUPIED_ROLES | COVERAGE_ROLES:
                raise ValueError("未知role，请区分实体设施、道路、基础与不占地的覆盖范围")
            clearance = _number(proposal.get("clearance_m", 0), "clearance_m", nonnegative=True)
            output["clearance_m"] = clearance
            if role in COVERAGE_ROLES and "radius_m" in proposal:
                _point(proposal.get("center_m"), "center_m")
                _number(proposal["radius_m"], "radius_m", positive=True)
                output.update(status="coverage_not_evaluated", note="覆盖圆不计作实体碰撞；本工具尚未计算覆盖率或吊装约束。")
                continue
            polygon = _proposal_polygon(proposal)
            output.update(area_m2=polygon.area, geometry_local_m=mapping(polygon),
                          alignment=_axis_measurement(polygon, angle), requested_angle_deg=proposal.get("angle_deg", 0) if "vertices_m" not in proposal else None)
            rectangle = "vertices_m" not in proposal
            placement = {"shape": "rectangle" if rectangle else "polygon",
                         "anchor_role": "pre_rotation_lower_left_and_rotation_origin" if rectangle else "ordered_vertices_define_shape",
                         "anchor_m": list(_point(proposal["position_m"], "position_m")) if rectangle else None,
                         "centroid_m": list(polygon.centroid.coords)[0],
                         "angle_deg_local": _number(proposal.get("angle_deg", 0), "angle_deg") if rectangle else None}
            output["placement"] = placement
            if frame_ready:
                world_polygon = _to_world(polygon, origin, angle, scale)
                output["geometry_wcs"] = mapping(world_polygon)
                placement["centroid_wcs"] = list(world_polygon.centroid.coords)[0]
                if rectangle:
                    placement["corner_points_wcs"] = [list(point) for point in world_polygon.exterior.coords]
            if role in COVERAGE_ROLES:
                output.update(status="coverage_not_evaluated", note="覆盖几何不登记为地面实体占地")
                continue
            alignment_required = proposal.get("require_axis_alignment", role == "road")
            if not isinstance(alignment_required, bool):
                raise ValueError("require_axis_alignment 必须为布尔值")
            output["alignment"]["required"] = alignment_required
            if alignment_required and output["alignment"]["maximum_edge_error_deg"] > AXIS_TOLERANCE_DEG:
                output["conflicts"].append({"code": "axis_alignment_unmet", "measured_deg": output["alignment"]["maximum_edge_error_deg"], "maximum_deg": AXIS_TOLERANCE_DEG})
            if not frame_ready or boundary is None:
                output["unresolved"].append({"code": "source_frame_or_boundary_unresolved"})
            else:
                outside = polygon.difference(boundary).area
                distance = polygon.distance(boundary.boundary)
                output["boundary_check"] = {"covers_exact": boundary.covers(polygon), "outside_area_m2": outside,
                                             "distance_to_boundary_m": distance, "clearance_m": clearance}
                if outside > AREA_TOLERANCE_M2:
                    output["conflicts"].append({"code": "outside_boundary", "boundary_id": selected["id"], "outside_area_m2": outside})
                if distance + LINEAR_TOLERANCE_M < clearance:
                    output["conflicts"].append({"code": "boundary_clearance_unmet", "measured_m": distance, "required_m": clearance})
                distances = []
                for obstacle, footprint in obstacles:
                    if footprint is None:
                        output["unresolved"].append({"code": "obstacle_geometry_unresolved", "id": obstacle["id"]})
                        continue
                    intersection = polygon.intersection(footprint)
                    gap = polygon.distance(footprint)
                    distances.append({"id": obstacle["id"], "tier": obstacle["tier"], "distance_m": gap})
                    overlapping = intersection.area > AREA_TOLERANCE_M2
                    too_close = gap + LINEAR_TOLERANCE_M < clearance
                    if not overlapping and not too_close:
                        continue
                    detail = {"id": obstacle["id"], "name": obstacle["name"], "tier": obstacle["tier"],
                              "intersection_area_m2": intersection.area, "distance_m": gap,
                              "required_clearance_m": clearance, "proposal_fully_inside_outline": footprint.covers(polygon)}
                    output["intersections"].append(detail)
                    field = "conflicts" if obstacle["tier"] == "reserved" else "unresolved"
                    output[field].append({"code": "reserved_outline_conflict" if field == "conflicts" else "uncertain_outline_intersection", **detail})
                # Compute each tier's nearest outline before shortening the
                # display list; top-ten proximity does not cover both tiers.
                output["nearest_outlines"] = {
                    tier: min((row for row in distances if row["tier"] == tier),
                              key=lambda row: (row["distance_m"], row["id"]), default=None)
                    for tier in ("reserved", "uncertain")}
                output["nearest_outlines_scope"] = "由全部可解算的独立约束计算；未解析或未识别对象仍属于检查范围的局限。"
                output["distances"] = sorted(distances, key=lambda row: (row["distance_m"], row["id"]))[:10]
                output["distance_list_scope"] = "只展示最近10项距离；全部独立约束均已检查，全部相交项另行列出"
            parsed.append((output, polygon, clearance))
        except (ValueError, TypeError, KeyError, OverflowError, ShapelyError) as exc:
            output["status"] = "invalid_proposal"
            output["conflicts"].append({"code": "invalid_proposal", "message": str(exc)})
    for index, (left, a, ca) in enumerate(parsed):
        for right, b, cb in parsed[index + 1:]:
            overlap = a.intersection(b).area
            gap = a.distance(b)
            allowed_junction = left["role"] == right["role"] == "road"
            conflict = not allowed_junction and (overlap > AREA_TOLERANCE_M2 or gap + LINEAR_TOLERANCE_M < max(ca, cb))
            row = {"ids": [left["id"], right["id"]], "intersection_area_m2": overlap, "distance_m": gap,
                   "required_clearance_m": max(ca, cb), "road_junction_allowed": allowed_junction, "conflict": conflict}
            result["pairwise"].append(row)
            if conflict:
                for item in (left, right):
                    item["conflicts"].append({"code": "proposal_pair_conflict", **row})
    for item in result["proposals"]:
        if item["status"] in {"invalid_proposal", "coverage_not_evaluated"}:
            continue
        item["status"] = "conflict" if item["conflicts"] else "requires_review" if item["unresolved"] else "no_detected_conflict"
    if result["issues"]:
        result["status"] = "constraints_incomplete"
    result["summary"] = {"proposal_count": len(proposals), "proposal_statuses": dict(Counter(item["status"] for item in result["proposals"])),
                         "source_unchanged_by_tool": True, "basis": "以公开工作假设和机器提取几何为依据，不代表工程验收"}
    return result
