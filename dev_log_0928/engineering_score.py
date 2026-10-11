"""Read-only, case-specific Baiqiao v0009 review; not a production validator.

Usage: .venv/bin/python dev_log/engineering_score.py SOURCE.dxf RESULT.dxf
Numbers use native drawing coordinates. No statutory compliance is inferred.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
from pathlib import Path

import ezdxf
from ezdxf.disassemble import recursive_decompose
from ezdxf.path import make_path
from ezdxf.lldxf.tagwriter import TagCollector
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

BUILDINGS = {"7#": "1204", "8#": "11EE", "9#": "127A", "10#": "1253"}
EPS = 1e-6


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def curve(entity):
    """Flatten bulges/arcs in native world XY; retain their curved footprint."""
    if entity.dxftype() not in {"LWPOLYLINE", "POLYLINE", "CIRCLE", "ARC", "ELLIPSE", "LINE"}:
        return None
    path = make_path(entity)
    points = [(float(p.x), float(p.y)) for p in path.flattening(distance=0.001, segments=16)]
    return LineString(points) if len(points) >= 2 else None


def polygon(entity):
    if entity.dxftype() == "INSERT":
        polygons = [polygon(child) for child in recursive_decompose([entity])]
        polygons = [p for p in polygons if p is not None and p.is_valid]
        return unary_union(polygons) if polygons else None
    if entity.dxftype() == "CIRCLE":
        return Point(entity.dxf.center.x, entity.dxf.center.y).buffer(
            entity.dxf.radius, quad_segs=1024
        )
    line = curve(entity)
    if line is not None and line.is_closed:
        return Polygon(line.coords)
    return None


def rectangle_width(shape):
    """Width is measurable from a rectangular outline, even const_width=0."""
    if shape is None or shape.geom_type != "Polygon" or not shape.is_valid:
        return None
    coords = list(shape.exterior.coords)
    if len(coords) != 5:
        return None
    box = shape.minimum_rotated_rectangle
    if abs(box.area - shape.area) > max(EPS, shape.area * 1e-6):
        return None
    return round(min(Point(a).distance(Point(b)) for a, b in zip(coords, coords[1:])), 6)


def descriptor(entity):
    return {"handle": entity.dxf.handle, "layer": entity.dxf.layer}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("result", type=Path)
    parser.add_argument("--coverage-handles", nargs="*", default=[], help="Manually verified coverage geometry, e.g. tower-layer circles with adjacent radius labels")
    parser.add_argument("--reference-handles", nargs="*", default=[], help="Non-occupying reference symbols, e.g. gate-opening strips, verified from labels")
    args = parser.parse_args()
    hashes = {"source": digest(args.source), "result": digest(args.result)}
    source, result = ezdxf.readfile(args.source), ezdxf.readfile(args.result)
    source_entities = {e.dxf.handle: e for e in source.modelspace()}
    result_entities = {e.dxf.handle: e for e in result.modelspace()}
    buildings = {}
    for name, handle in BUILDINGS.items():
        candidates = [
            polygon(e) for e in recursive_decompose([source.entitydb[handle]])
            if e.dxftype() == "LWPOLYLINE" and e.closed
        ]
        candidates = [p for p in candidates if p is not None and p.is_valid]
        buildings[name] = max(candidates, key=lambda p: p.area)
    new = [e for h, e in result_entities.items() if h not in source_entities]
    facilities, roads, coverage, unclassified = [], [], [], []
    classification_notes = []
    reference_geometry = []
    for entity in new:
        layer = entity.dxf.layer.lower()
        shape = polygon(entity)
        item = descriptor(entity)
        if entity.dxf.handle in args.reference_handles:
            reference_geometry.append(item)
        elif (entity.dxf.handle in args.coverage_handles or any(word in layer for word in ("覆盖", "coverage", "cover"))) and shape is not None:
            coverage.append(shape)
        elif any(word in layer for word in ("道路", "临路", "road")):
            line = curve(entity)
            width = float(entity.dxf.get("const_width", 0)) if entity.dxftype() == "LWPOLYLINE" else 0
            if width > 0 and line is not None:
                surface = line.buffer(width / 2, cap_style="flat", join_style="mitre")
                representation = "width-bearing centerline"
                centerline = line
                measured_width = width
            elif shape is not None and shape.is_valid:
                surface = shape
                representation = "closed road outline; surface interpretation needs label/visual confirmation"
                centerline = None
                measured_width = rectangle_width(shape)
            else:
                surface = None
                representation = "unresolved road geometry; do not infer zero physical width"
                centerline = line
                measured_width = None
            item.update(width_native=measured_width, dxf_constant_width=width,
                        representation=representation,
                        centerline_simple=centerline.is_simple if centerline is not None else None,
                        boundary_simple=line.is_simple if line is not None else None,
                        centerline=centerline, geometry=surface)
            roads.append(item)
        elif shape is not None and shape.is_valid:
            facilities.append({**item, "geometry": shape})
            if entity.dxftype() == "INSERT":
                classification_notes.append({**item, "note": "Facility footprint unions closed virtual children; manually confirm block semantics"})
        elif entity.dxftype() not in {"TEXT", "MTEXT", "ATTRIB"}:
            detail = {**item, "type": entity.dxftype()}
            if entity.dxftype() == "INSERT":
                detail["virtual_child_types"] = [child.dxftype() for child in recursive_decompose([entity])]
            unclassified.append(detail)
    collisions = []
    for facility in facilities:
        for name, building in buildings.items():
            area = facility["geometry"].intersection(building).area
            if area > EPS:
                collisions.append({"facility": facility["handle"], "layer": facility["layer"],
                                   "building": name, "overlap_native_area": round(area, 6),
                                   "facility_fraction": round(area / facility["geometry"].area, 6)})
    mutual = []
    for first, second in itertools.combinations(facilities, 2):
        area = first["geometry"].intersection(second["geometry"]).area
        if area > EPS:
            mutual.append({"handles": [first["handle"], second["handle"]], "overlap_native_area": round(area, 6)})
    road_details = []
    for road in roads:
        surface = road["geometry"]
        item = {k: v for k, v in road.items() if k not in {"geometry", "centerline"}}
        item["building_centerline_crossings"] = {
            name: round(road["centerline"].intersection(building).length, 6)
            for name, building in buildings.items()
            if road["centerline"] is not None and road["centerline"].intersection(building).length > EPS
        }
        item["building_surface_overlaps"] = {
            name: round(surface.intersection(building).area, 6)
            for name, building in buildings.items()
            if surface is not None and surface.is_valid and surface.intersection(building).area > EPS
        }
        item["facility_surface_overlaps"] = {
            facility["handle"]: round(surface.intersection(facility["geometry"]).area, 6)
            for facility in facilities
            if surface is not None and surface.is_valid and surface.intersection(facility["geometry"]).area > EPS
        }
        road_details.append(item)
    coverage_union = unary_union(coverage)
    road_surfaces = [r["geometry"] for r in roads if r["geometry"] is not None and r["geometry"].is_valid]
    road_union = unary_union(road_surfaces)
    road_component_count = 0 if road_union.is_empty else (len(road_union.geoms) if hasattr(road_union, "geoms") else 1)
    unchanged = lambda a, b: TagCollector.dxftags(a) == TagCollector.dxftags(b)
    changed = [h for h, e in source_entities.items() if h in result_entities and not unchanged(e, result_entities[h])]
    output = {
        "case": "Baiqiao v0009; four factory footprints from original transformed INSERTs",
        "scope": "Native geometry only; no inferred scale, legal-boundary confirmation, or full construction-code approval",
        "hashes": hashes,
        "source_entities": len(source_entities), "result_entities": len(result_entities),
        "deleted_source_handles": sorted(set(source_entities) - set(result_entities)),
        "changed_source_handles": changed,
        "building_handles": BUILDINGS,
        "facility_count": len(facilities),
        "facility_building_overlaps": collisions,
        "facility_facility_overlaps": mutual,
        "roads": road_details, "new_road_network_connected_components": road_component_count,
        "coverage_entity_count": len(coverage),
        "building_coverage_fraction": {name: round(building.intersection(coverage_union).area / building.area, 6) for name, building in buildings.items()},
        "classification": "Layer names propose roles only; inspect labels and visible result before accepting role-dependent metrics",
        "curve_flattening_tolerance_native": 0.001,
        "classification_notes": classification_notes,
        "manual_coverage_handles": args.coverage_handles,
        "nonoccupying_reference_geometry": reference_geometry,
        "unclassified_new_geometry": unclassified,
        "source_unchanged_during_read": digest(args.source) == hashes["source"],
        "result_unchanged_during_read": digest(args.result) == hashes["result"],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
