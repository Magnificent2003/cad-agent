"""Read-only, evidence-linked spatial facts for review; no layout or CAD edits.

Number/name containment supplies identity candidates, never approval to occupy land.
A table/geometry scale consensus can rank alternatives but never silently replace
INSUNITS. No project handles, coordinates, building numbers, or desired scale are
encoded in this algorithm. All rules/limits are reported with the result.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import re
import statistics

from shapely import affinity
from shapely.geometry import Point, shape

from .geometry import UNIT_METRES, digest
from .spatial_extract import extract_drawing

BOUNDARY_LAYER = re.compile(r"红线|场界|用地界|用地边界|site[-_ ]?(?:boundary|bnd)|^boundary$", re.I)
BUILDING_LAYER = re.compile(r"建筑轮廓|建筑外轮廓|SITE[-_ ]?OUTL|BLDG[-_ ]?(?:OUTL|OUTLINE)|BUILDING[-_ ]?OUTLINE", re.I)
BUILDING_NAME = re.compile(r"厂房|办公楼|食堂|配电房|配电室|门卫|仓库|宿舍|综合楼|教学楼|住宅楼|building|warehouse", re.I)
NUMBER = re.compile(r"^\s*(\d{1,3})\s*[#＃号]\s*$")
TABLE_NAME = re.compile(r"^\s*(\d{1,3})\s*[#＃号]\s*(.+)")
TABLE_TITLE = re.compile(r"指标表|一览表|明细表|统计表")
AREA_HEADER = re.compile(r"^占地面积\s*[（(].*(?:㎡|m[²2]|平方米).*[）)]$", re.I)
NUMERIC = re.compile(r"^\d+(?:,\d{3})*(?:\.\d+)?$")
SCALE_RELATIVE_TOLERANCE = 1e-4
AREA_RELATIVE_TOLERANCE = 1e-4


def axis_metrics(geometry):
    """Orthogonal axes are not interchangeable with the selected long edge."""
    polygon = shape(geometry)
    origin = polygon.bounds[:2]
    local = affinity.translate(polygon, -origin[0], -origin[1])
    rectangle = local.minimum_rotated_rectangle
    coords = list(rectangle.exterior.coords)
    edges = []
    for a, b in zip(coords, coords[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        edges.append((math.hypot(dx, dy), math.degrees(math.atan2(dy, dx)) % 180))
    longest = max(edges)
    return {"area_native": polygon.area, "axis_deg": longest[1] % 90,
            "long_edge_angle_deg": longest[1],
            "edge_lengths_native": [longest[0], min(e[0] for e in edges)],
            "rectangularity": polygon.area / rectangle.area if rectangle.area else 0,
            "axis_method": "minimum_rotated_rectangle_for_reference_only"}


def _feature(record, kind, label, status="candidate", **extra):
    properties = {"kind": kind, "label": label, "status": status,
                  "source": record["source"], "layer": record["effective_layer"],
                  "geometry_status": record["geometry_status"],
                  "role_status": "pending_engineering_confirmation", "phase": "unknown",
                  "width_native": record.get("width_native"), "width_wcs_max": record.get("width_wcs_max"),
                  "elevation": record.get("elevation"), "visibility": record.get("visibility"),
                  "evidence": [], **axis_metrics(record["geometry"]), **extra}
    return {"type": "Feature", "id": record["id"], "geometry": record["geometry"], "properties": properties}


def _project(text, header):
    theta = math.radians(header.get("rotation_deg", 0))
    x, y = (text["position"][i] - header["position"][i] for i in (0, 1))
    return (x * math.cos(theta) + y * math.sin(theta), -x * math.sin(theta) + y * math.cos(theta))


def read_area_table(texts):
    """Associate table rows and columns, not nearest arbitrary number on the page."""
    rows, warnings = [], []
    table_roots = {t["source"]["root_handle"] for t in texts if TABLE_TITLE.search(t["text"])}
    for header in texts:
        if not AREA_HEADER.match(header["text"].strip()):
            continue
        root = header["source"]["root_handle"]
        table_roots.add(root)
        same = [t for t in texts if t["source"]["root_handle"] == root]
        h = max(float(header.get("height_native", 0)), 1e-9)
        headings = [(_project(t, header)[0], t) for t in same
                    if abs(_project(t, header)[1]) < h * .6 and t["id"] != header["id"]
                    and not NUMERIC.fullmatch(t["text"].strip())]
        left = max((x for x, _ in headings if x < 0), default=-4 * h) / 2
        right = min((x for x, _ in headings if x > 0), default=4 * h) / 2
        for label in same:
            match = TABLE_NAME.match(label["text"])
            if not match or not BUILDING_NAME.search(match[2]):
                continue
            lx, ly = _project(label, header)
            if lx >= left or not -80 * h < ly < -h:
                continue
            candidates = [t for t in same if NUMERIC.fullmatch(t["text"].strip())
                          and left < _project(t, header)[0] < right
                          and abs(_project(t, header)[1] - ly) < h * .6]
            if len(candidates) != 1:
                warnings.append({"code": "ambiguous_table_cell", "message": "占地面积表行列未得到唯一数值。",
                                 "source_ids": [header["id"], label["id"], *[t["id"] for t in candidates]]})
                continue
            value = candidates[0]
            area = float(value["text"].replace(",", ""))
            if area <= 0:
                continue
            rows.append({"number": match[1], "name": match[2].strip(), "area_m2": area,
                         "header": {"id": header["id"], "text": header["text"], "source": header["source"]},
                         "label": {"id": label["id"], "text": label["text"], "source": label["source"]},
                         "cell": {"id": value["id"], "text": value["text"], "position": value["position"], "source": value["source"]},
                         "method": "same_block_header_column_and_row_alignment"})
    return rows, table_roots, warnings



def read_site_annotations(texts):
    """Recover explicitly labelled land-area cells and stage notes, without adjudicating them."""
    area_rows = []
    for label in texts:
        if not re.fullmatch(r"(?:建设)?用地面积", label["text"].strip()):
            continue
        height = max(float(label.get("height_native", 0)), 1e-9)
        same_row = [t for t in texts if t["source"]["root_handle"] == label["source"]["root_handle"]
                    and 0 < _project(t, label)[0] < 60 * height
                    and abs(_project(t, label)[1]) < .6 * height]
        unit_cells = [t for t in same_row if re.fullmatch(r"㎡|m[²2]|平方米", t["text"].strip(), re.I)]
        if len(unit_cells) != 1:
            continue
        unit_cell = unit_cells[0]
        values = [t for t in same_row if NUMERIC.fullmatch(t["text"].strip())
                  and _project(t, label)[0] < _project(unit_cell, label)[0]]
        if len(values) != 1:
            continue
        value = values[0]
        area_rows.append({"name": label["text"], "area_m2": float(value["text"].replace(",", "")),
                          "label": label, "cell": value, "unit_cell": unit_cell,
                          "status": "table_assertion_not_boundary_identity",
                          "method": "same_block_row_with_explicit_square_metre_unit"})
    notes = [{"text": t["text"], "id": t["id"], "source": t["source"],
              "status": "source_annotation_not_current_phase_confirmation"}
             for t in texts if re.search(r"拆除|保留", t["text"])]
    return area_rows, notes


def _same_building_name(anchors, table_name):
    # Conservative first-stage rule: aliases/abbreviations stay unconfirmed.
    normalize = lambda name: re.sub(r"[\s·,，。;；:：()（）]", "", name).casefold()
    names = {normalize(anchor["text"]) for anchor in anchors}
    return names == {normalize(table_name)}


def _scale_consensus(groups, rows, polygon_map):
    by_number = defaultdict(list)
    for row in rows:
        by_number[row["number"]].append(row)
    pairs = []
    for number, candidates in groups.items():
        if len(by_number[number]) != 1:
            continue
        row = by_number[number][0]
        for candidate in candidates:
            if not _same_building_name(candidate["name_anchors"], row["name"]):
                continue
            area = polygon_map[candidate["polygon_id"]].area
            scale = math.sqrt(row["area_m2"] / area)
            pairs.append({"number": number, "polygon_id": candidate["polygon_id"], "scale": scale,
                          "area_native": area, "table_area_m2": row["area_m2"], "table_cell_id": row["cell"]["id"]})
    clusters = []
    for seed in pairs:
        members = [p for p in pairs if abs(p["scale"] / seed["scale"] - 1) <= SCALE_RELATIVE_TOLERANCE]
        unique = {p["number"] for p in members}
        clusters.append((len(unique), members))
    if not clusters:
        return None, [], pairs
    best_count = max(c[0] for c in clusters)
    best = [m for count, m in clusters if count == best_count]
    if best_count < 3:
        return None, [], pairs
    centers = [statistics.median(p["scale"] for p in m) for m in best]
    if max(centers) / min(centers) - 1 > 2 * SCALE_RELATIVE_TOLERANCE:
        return None, [], pairs
    members = best[0]
    if len({round(p["table_area_m2"], 2) for p in members}) < 2:
        return None, [], pairs
    return statistics.median(p["scale"] for p in members), members, pairs


def construct_facts(extracted):
    records = extracted["polygons"]
    polygons = {r["id"]: shape(r["geometry"]) for r in records}
    by_id = {r["id"]: r for r in records}
    texts = [t for t in extracted["texts"] if t["geometry_status"] == "anchor_only"]
    rows, table_roots, unresolved = read_area_table(texts)
    site_rows, design_notes = read_site_annotations(texts)
    boundary_records = [r for r in records if BOUNDARY_LAYER.search(r["effective_layer"])]
    boundary_ids = {r["id"] for r in boundary_records}
    features = {r["id"]: _feature(r, "boundary_candidate", f"场界候选 {i + 1}", evidence=[
        {"kind": "layer_hint", "value": r["effective_layer"]},
        {"kind": "geometry", "value": "有效闭合参考边界；法律/工程用途未核实"}]) for i, r in enumerate(boundary_records)}
    numbers = [t for t in texts if NUMBER.match(t["text"]) and t["source"]["root_handle"] not in table_roots]
    names = [t for t in texts if BUILDING_NAME.search(t["text"]) and t["source"]["root_handle"] not in table_roots]
    groups = defaultdict(list)
    for record in records:
        pid = record["id"]
        if pid in boundary_ids:
            continue
        if record["geometry_status"] != "valid_reference_boundary":
            continue  # Keep unresolved clipped geometry as context, never as supported evidence.
        polygon = polygons[pid]
        number_anchors = [t for t in numbers if polygon.covers(Point(t["position"]))]
        name_anchors = [t for t in names if polygon.covers(Point(t["position"]))]
        identities = {NUMBER.match(t["text"])[1] for t in number_anchors}
        if len(identities) == 1 and name_anchors:
            number = next(iter(identities))
            groups[number].append({"polygon_id": pid, "number": number,
                                   "number_anchors": number_anchors, "name_anchors": name_anchors})
        elif BUILDING_LAYER.search(record["effective_layer"]):
            features[pid] = _feature(record, "building_candidate", "建筑轮廓图层候选", primary=False,
                                    evidence=[{"kind": "layer_hint", "value": record["effective_layer"]}])
    scale, consensus, pairs = _scale_consensus(groups, rows, polygons)
    declared = UNIT_METRES.get(extracted["units_code"])
    conflict = scale is not None and declared is not None and not math.isclose(scale, declared, rel_tol=1e-4)
    units = {"status": "conflict" if conflict else "candidate" if scale is not None else "unresolved",
             "declared_code": extracted["units_code"], "declared_metres_per_unit": declared,
             "candidate_metres_per_unit": scale, "confirmed": False,
             "evidence": consensus, "method": "independent_numbered_building_table_area_scale_consensus",
             "note": "单位候选不覆盖文件头，不修改输入；需审查冲突。"}
    if conflict:
        unresolved.append({"code": "units_conflict", "message": "多栋编号建筑的占地面积支持单位候选，但与文件头声明冲突；不自动修正。",
                           "source_ids": [p["polygon_id"] for p in consensus]})
    if scale is None:
        unresolved.append({"code": "units_unresolved", "message": "未得到至少三栋、两种面积的共同尺度证据。", "source_ids": []})
    for boundary in boundary_records:
        if scale is None or len(site_rows) != 1 or site_rows[0]["area_m2"] <= 0:
            continue
        boundary_area = polygons[boundary["id"]].area * scale ** 2
        table_area = site_rows[0]["area_m2"]
        comparison = {"candidate_area_m2": boundary_area, "table_area_m2": table_area,
                      "table_cell_id": site_rows[0]["cell"]["id"],
                      "relative_difference": boundary_area / table_area - 1,
                      "meaning": "conditional on candidate scale; same engineering role not established"}
        features[boundary["id"]]["properties"]["land_area_comparison"] = comparison
        if abs(comparison["relative_difference"]) > AREA_RELATIVE_TOLERANCE:
            unresolved.append({"code": "boundary_area_disagreement",
                               "message": "场界候选在候选尺度下面积与建设用地面积表值不同；不能假定两者指同一范围，不能据此再次缩放图纸。",
                               "source_ids": [boundary["id"], site_rows[0]["cell"]["id"]], **comparison})
    if design_notes:
        unresolved.append({"code": "design_phase_unconfirmed",
                           "message": "图中存在拆除/保留说明；未据此确认具体对象当前是否拆除或禁止占用。",
                           "source_ids": [note["id"] for note in design_notes]})
    primary_features = []
    for number, candidates in sorted(groups.items(), key=lambda item: int(item[0])):
        supports = [p for p in pairs if p["number"] == number and scale is not None
                    and abs(p["scale"] / scale - 1) <= SCALE_RELATIVE_TOLERANCE]
        selected = supports[0]["polygon_id"] if len(supports) == 1 else candidates[0]["polygon_id"] if len(candidates) == 1 else None
        if selected is None:
            unresolved.append({"code": "ambiguous_building_outline", "message": f"{number}号有多个轮廓候选，现有证据不能唯一选择。",
                               "source_ids": [c["polygon_id"] for c in candidates]})
        for candidate in candidates:
            pid = candidate["polygon_id"]
            primary = pid == selected
            table_rows = [r for r in rows if r["number"] == number]
            error = None
            if scale is not None and len(table_rows) == 1:
                error = polygons[pid].area * scale ** 2 / table_rows[0]["area_m2"] - 1
            name_consistent = (_same_building_name(candidate["name_anchors"], table_rows[0]["name"])
                               if len(table_rows) == 1 else None)
            supported = primary and name_consistent is True and error is not None and abs(error) <= AREA_RELATIVE_TOLERANCE
            if name_consistent is False:
                unresolved.append({"code": "building_name_conflict",
                                   "message": f"{number}号面内名称与表中建筑名称不一致或存在多种名称；不能仅凭编号和面积判定支持。",
                                   "source_ids": [pid, table_rows[0]["label"]["id"], *[t["id"] for t in candidate["name_anchors"]]]})
            evidence = [{"kind": "contained_number", "id": t["id"], "text": t["text"], "position": t["position"], "source": t["source"]}
                        for t in candidate["number_anchors"]]
            evidence += [{"kind": "contained_name", "id": t["id"], "text": t["text"], "position": t["position"], "source": t["source"]}
                         for t in candidate["name_anchors"]]
            if len(table_rows) == 1:
                evidence.append({"kind": "table_area", "row": table_rows[0], "relative_error_at_candidate_scale": error})
            f = _feature(by_id[pid], "building_candidate", f"{number}# {candidate['name_anchors'][0]['text'].strip()}",
                         "supported" if supported else "candidate", primary=primary, number=number,
                         evidence=evidence, area_relative_error=error,
                         identity_status="number_and_name_spatially_associated", table_name_consistent=name_consistent,
                         selection_method="unique_table_area_scale_consensus" if supported else "unique_label_containment" if primary else "unselected_alternative",
                         occluded_by=[])
            features[pid] = f
            if primary:
                primary_features.append(f)
                if error is not None and abs(error) > AREA_RELATIVE_TOLERANCE:
                    unresolved.append({"code": "building_area_conflict", "message": f"{number}号编号和名称可关联，但轮廓面积与表值相差 {error * 100:.4f}%，需核实外墙/投影口径。", "source_ids": [pid]})
    found_numbers = set(groups)
    for row in rows:
        if row["number"] not in found_numbers:
            unresolved.append({"code": "table_building_not_located", "message": f"表中 {row['number']}# {row['name']} 未得到编号与名称共同支持的图面轮廓；不按面积相等强行认领。",
                               "source_ids": [row["label"]["id"]]})
    spatial_relations = []
    for building in primary_features:
        footprint = shape(building["geometry"])
        for boundary in boundary_records:
            site = polygons[boundary["id"]]
            outside_area = footprint.difference(site).area
            relation = {"building_id": building["id"], "boundary_id": boundary["id"],
                        "outside_area_native2": outside_area,
                        "outside_fraction": outside_area / footprint.area,
                        "covered_by_candidate_boundary": site.covers(footprint),
                        "status": "geometry_only_not_engineering_violation"}
            spatial_relations.append(relation)
            if relation["outside_fraction"] > AREA_RELATIVE_TOLERANCE:
                unresolved.append({"code": "building_outside_boundary_candidate",
                                   "message": f"{building['properties']['number']}号参考轮廓有 {relation['outside_fraction'] * 100:.2f}% 在候选场界外；需核实场界角色，不自动裁剪建筑或判定违规。",
                                   "source_ids": [building["id"], boundary["id"]], **relation})
    masks = [(r, shape(r["geometry"])) for r in extracted["wipeouts"]]
    for f in features.values():
        if f["properties"]["kind"] != "building_candidate":
            continue
        poly = shape(f["geometry"])
        f["properties"]["occluded_by"] = [
            {"id": r["id"], "xy_fraction": poly.intersection(mask).area / poly.area,
             "meaning": "XY mask overlap only; draw order and visibility not inferred"}
            for r, mask in masks if poly.intersection(mask).area > 0]
    for r, _ in masks:
        features[r["id"]] = _feature(r, "wipeout", "遮罩范围", evidence=[{"kind": "display_mask", "value": "不是建筑占用面，也不是可用区域"}])
    # Only clip the presentation scope, never crop/repair source CAD geometry.
    relevant = [polygons[r["id"]] for r in boundary_records] + [shape(f["geometry"]) for f in primary_features]
    if not relevant:
        raise ValueError("No defensible scene extent: no boundary candidate or labelled building. Provide task scope before rendering.")
    bounds = [min(g.bounds[0] for g in relevant), min(g.bounds[1] for g in relevant),
              max(g.bounds[2] for g in relevant), max(g.bounds[3] for g in relevant)]
    from shapely.geometry import box
    scope_shape = box(*bounds)
    for record in records:
        if record["id"] in features or not polygons[record["id"]].intersects(scope_shape):
            continue
        # Background is not classified evidence: omit redundant per-face metrics/matrices.
        features[record["id"]] = {"type": "Feature", "id": record["id"], "geometry": record["geometry"],
            "properties": {"kind": "context_outline", "label": "未分类闭合线", "status": "unclassified",
                           "layer": record["effective_layer"], "geometry_status": record["geometry_status"],
                           "source": {key: record["source"][key] for key in ("root_handle", "entity_handle", "insert_path", "block_path")}}}
    grouped_issues = defaultdict(list)
    for issue in extracted["issues"]:
        grouped_issues[(issue["code"], issue.get("entity_type", ""), issue.get("layer", ""), issue["message"])].append(issue["id"])
    for (code, entity_type, layer, message), ids in grouped_issues.items():
        unresolved.append({"code": code, "entity_type": entity_type, "layer": layer,
                           "message": message, "count": len(ids), "source_ids": ids})
    unresolved.extend([
        {"code": "boundary_role_unconfirmed", "message": "场界来自图层语义候选；用途、有效阶段及合法用地范围需核实。", "source_ids": sorted(boundary_ids)},
        {"code": "scene_coverage_incomplete", "message": "部分A原型仅支持已提取参考面及文字证据；未分类线、曲线、专业对象和阶段语义未全部解决，不能输出已确认可布置空地。", "source_ids": []},
    ])
    supported_features = [f for f in primary_features if f["properties"]["status"] == "supported"]
    angle = None
    if supported_features:
        values = [f["properties"]["axis_deg"] for f in supported_features]
        if max(values) - min(values) < .5:
            angle = statistics.median(values)
    boundary_angle = features[boundary_records[0]["id"]]["properties"]["axis_deg"] if len(boundary_records) == 1 else None
    difference = None if angle is None or boundary_angle is None else abs((angle - boundary_angle + 45) % 90 - 45)
    edges = []
    for record in boundary_records:
        coordinates = record["geometry"]["coordinates"][0]
        for i, (a, b) in enumerate(zip(coordinates, coordinates[1:]), 1):
            edges.append({"id": f"{record['id']}:edge:{i}", "boundary_id": record["id"], "start": a, "end": b,
                          "length_native": math.dist(a, b), "angle_deg": math.degrees(math.atan2(b[1]-a[1], b[0]-a[0])) % 180,
                          "status": "reference_candidate_not_selected"})
    return {"schema_version": "spatial-facts/0.1", "source": {"name": Path(extracted["source_path"]).name,
                "sha256": extracted["source_sha256"], "units_code": extracted["units_code"],
                "modelspace_entity_count": extracted["inventory"]["root_entities"]},
            "scope": {"bounds_native": bounds, "origin": bounds[:2], "boundary_ids": sorted(boundary_ids), "status": "candidate",
                      "coordinate_system": "source WCS XY; no reprojection or source edit", "free_space_certified": False},
            "units": units, "features": list(features.values()), "boundary_edges": edges, "area_table": rows,
            "site_area_table": site_rows, "design_notes": design_notes, "spatial_relations": spatial_relations,
            "axes": {"status": "candidate", "building_angle_deg": angle, "boundary_angle_deg": boundary_angle,
                     "difference_deg": difference, "support_features": [f["id"] for f in supported_features],
                     "meaning": "orthogonal axis family modulo 90; inspect each long_edge_angle_deg, do not assume the family angle is the long edge"},
            "summary": {"building_candidates": sum(f["properties"]["kind"] == "building_candidate" for f in features.values()),
                        "numbered_buildings": len(primary_features), "area_supported_buildings": len(supported_features),
                        "boundary_candidates": len(boundary_records), "unresolved_count": len(unresolved),
                        "unresolved_entity_instances": len(extracted["issues"]), "engineering_confirmed_buildings": 0},
            "inventory": extracted["inventory"], "unresolved": unresolved,
            "method": {"stage": "A only; no layout action", "scale_cluster_relative_tolerance": SCALE_RELATIVE_TOLERANCE,
                       "area_relative_tolerance": AREA_RELATIVE_TOLERANCE,
                       "name_match": "normalized_exact_match; abbreviations and divergent names stay unresolved", "minimum_scale_support_buildings": 3,
                       "semantic_association_plane": "WCS XY projection; source Z is retained, not interpreted as floor or physical contact",
                       "selection": "number+name containment, then independent table area consensus; never largest polygon or hardcoded handle",
                       "limitations": [*extracted["limitations"],
                           "Table parsing requires same-root blocks, explicit area headers/units and aligned cells; unsupported tables remain unknown.",
                           "Number/name anchors only support candidate identity; nested rooms, phases and legal boundary roles need review.",
                           "Area consensus is a ranking hypothesis, not independent unit certification or population accuracy."]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--prefix", default="spatial")
    parser.add_argument("--expect-sha256", help="Refuse a different source/version before analysis")
    args = parser.parse_args()
    if not re.fullmatch(r"[\w-]+", args.prefix):
        parser.error("prefix must be a filename stem, not a path")
    before = digest(args.source)
    if args.expect_sha256 and before != args.expect_sha256:
        parser.error("source SHA256 differs from the pinned review version")
    extracted = extract_drawing(args.source)
    if extracted["source_sha256"] != before:
        raise RuntimeError("Source changed while preparing extraction")
    facts = construct_facts(extracted)
    if digest(args.source) != before:
        raise RuntimeError("Source changed during analysis")
    args.output.mkdir(parents=True, exist_ok=True)
    destination = args.output / f"{args.prefix}_facts.json"
    destination.write_text(json.dumps(facts, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    from .spatial_report import render_spatial_report
    paths = render_spatial_report(facts, args.output, prefix=args.prefix)
    print(json.dumps({"facts": str(destination), **paths, "summary": facts["summary"], "units": facts["units"]["status"],
                      "source_unchanged": digest(args.source) == before}, ensure_ascii=False))


if __name__ == "__main__":
    main()
