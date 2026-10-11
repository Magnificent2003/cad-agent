"""Read an existing task's DXF and public tool trace; write only audit JSON.

No model calls, CAD edits, publication, or replacement layout is performed.
Areas and road adjacency come from saved entities, using the task's explicitly
adopted working coordinate frame. This is not engineering-code certification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ezdxf  # noqa: E402
from shapely import affinity  # noqa: E402
from shapely.geometry import LineString, Polygon, shape  # noqa: E402
from shapely.ops import unary_union  # noqa: E402
from cad_domain.spatial_constraints import _adopt_units  # noqa: E402

TOLERANCE_M = 1e-6


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def decoded_result(part):
    text = part.get('output', '')
    try:
        return json.JSONDecoder().raw_decode(text[text.index('{'):])[0]
    except (ValueError, TypeError):
        return {}


def audit(context_path, target, trace_path, manifest_path=None):
    context = json.loads(context_path.read_text())
    scratch = Path(context['scratch_directory'])
    source = Path(next(item['path'] for item in context['input_files'] if item['format'] == 'dxf'))
    key = target.relative_to(Path(context['output_directory'])).as_posix()
    contract = json.loads((scratch / 'cad-review.json').read_text())['entries'][key]
    # A timed-out run may have a newer agent-authored manifest than the last
    # registered review. Measure it explicitly, without changing that review.
    registered_sha = contract.get('checked_sha256')
    if manifest_path:
        contract = json.loads(manifest_path.read_text())
    facts_id = contract['facts']['facts_id']
    analysis = json.loads((scratch / 'spatial-facts' / (facts_id + '.json')).read_text())['analysis']
    units = _adopt_units(analysis['units'])
    scale = units['adopted_metres_per_unit']
    if scale is None:
        raise ValueError('No adopted metre scale; refusing to invent one for the audit')
    frame = analysis['axis_frame']
    origin, angle = frame['local_frame']['origin_wcs'], frame['axis_deg']
    cosine, sine = math.cos(math.radians(-angle)), math.sin(math.radians(-angle))

    def local_point(x, y):
        dx, dy = x - origin[0], y - origin[1]
        return ((cosine * dx - sine * dy) * scale, (sine * dx + cosine * dy) * scale)

    def local_shape(geometry):
        return affinity.scale(affinity.rotate(affinity.translate(geometry, -origin[0], -origin[1]),
                                             -angle, origin=(0, 0)), scale, scale, origin=(0, 0))

    doc = ezdxf.readfile(target)
    facilities, roads, actual, non_occupancy = {}, {}, [], []
    for spec in contract['components']:
        entity = doc.entitydb[spec['handle']]
        if spec['role'] not in {'facility', 'road'}:
            non_occupancy.append({'id': spec['id'], 'role': spec['role'], 'handle': spec['handle'],
                                  'entity_type': entity.dxftype(), 'area_measured': False})
            continue
        if entity.dxftype() != 'LWPOLYLINE':
            raise ValueError(f"Unsupported actual component type: {entity.dxftype()}")
        points = [local_point(x, y) for x, y in entity.get_points('xy')]
        item = {key: spec[key] for key in ('id', 'handle', 'role')}
        label = doc.entitydb.get(spec.get('label_handle', ''))
        item['name'] = spec.get('name') or (label.dxf.text if label and label.dxftype() == 'TEXT' else spec['id'])
        if spec['role'] == 'road':
            width = entity.dxf.const_width * scale
            polygon = LineString(points).buffer(width / 2, cap_style=2, join_style=2)
            roads[spec['id']] = polygon
            item.update(actual_width_m=width, actual_centerline_m=points)
        else:
            if not entity.closed or any(abs(v[4]) > 1e-12 for v in entity.get_points()):
                raise ValueError('Facility audit currently requires a closed straight-sided footprint')
            polygon = Polygon(points)
            facilities[spec['id']] = polygon
        if not polygon.is_valid or polygon.is_empty:
            raise ValueError('Invalid saved occupied shape; no repair applied')
        item['actual_area_m2'] = polygon.area
        actual.append(item)

    road_union = unary_union(list(roads.values()))
    edges, graph = [], {key: set() for key in roads}
    pair_gaps = []
    for index, key in enumerate(roads):
        for other in list(roads)[index + 1:]:
            gap = roads[key].distance(roads[other])
            pair_gaps.append({'a': key, 'b': other, 'gap_m': gap})
            if gap <= TOLERANCE_M:
                edges.append({'a': key, 'b': other, 'gap_m': gap})
                graph[key].add(other)
                graph[other].add(key)
    seen, components = set(), []
    for key in graph:
        if key in seen:
            continue
        stack, component = [key], []
        while stack:
            vertex = stack.pop()
            if vertex in seen:
                continue
            seen.add(vertex)
            component.append(vertex)
            stack.extend(graph[vertex] - seen)
        components.append(sorted(component))

    trace = json.loads(trace_path.read_text())
    calls = [part for message in trace for part in message.get('parts', [])
             if part.get('type') == 'tool' and part.get('tool', '').endswith('cad_site_rules')]
    programs = [(part, decoded_result(part).get('program', {})) for part in calls]
    programs = [(part, program) for part, program in programs if program.get('facilities')]
    budgets = []
    if programs:
        first, last = programs[0][1], programs[-1][1]
        for configured in first['facilities']:
            identity = configured['id']
            aliases = {identity, 'equip'} if identity == 'equipment' else {identity}
            matches = [item for item in actual if item['role'] == 'facility'
                       and aliases.intersection(re.split(r'[-_]', item['id'].lower()))]
            adopted = next(item for item in last['facilities'] if item['id'] == identity)
            measured = sum(item['actual_area_m2'] for item in matches) if matches else None
            budgets.append({'facility_id': identity, 'name': configured['name'],
                            'initial_target_m2': configured['target_area_m2'],
                            'adopted_target_m2': adopted['target_area_m2'],
                            'actual_area_m2': measured, 'component_ids': [item['id'] for item in matches],
                            'association_method': 'component ID token; equip aliases equipment',
                            'meets_initial_target': None if measured is None else measured + 1e-6 >= configured['target_area_m2'],
                            'meets_adopted_target': None if measured is None else measured + 1e-6 >= adopted['target_area_m2']})

    selected = analysis['selected_boundary']
    if isinstance(selected, str):
        selected = next(item for item in analysis['boundary_candidates'] if item['id'] == selected)
    boundary = local_shape(shape(selected['geometry']))
    outlined = [local_shape(shape(item['geometry'])) for item in analysis['classifications']
                if item['category'] == 'retained' or item['category'] == 'uncertain' and item.get('display_priority') != 'minor']
    clearance = float(contract['facts'].get('clearance_m', 0))
    free = boundary.buffer(-clearance).difference(unary_union([p.buffer(clearance) for p in outlined]))
    model = doc.modelspace()
    order = list(model.get_redraw_order())
    visibility = []
    added_handles = {handle for spec in contract['components'] for key, handle in spec.items()
                     if key in {'handle', 'label_handle'}}
    for index, entity in enumerate(model):
        if entity.dxftype() != 'WIPEOUT' and entity.dxf.handle not in added_handles:
            continue
        layer = doc.layers.get(entity.dxf.layer)
        item = {'handle': entity.dxf.handle, 'type': entity.dxftype(), 'modelspace_index': index,
                'layer': entity.dxf.layer, 'layer_off': layer.is_off(), 'layer_frozen': layer.is_frozen(),
                'entity_invisible': entity.dxf.get('invisible', 0)}
        if entity.dxftype() == 'TEXT':
            item.update(text=entity.dxf.text, text_height_m=entity.dxf.height * scale)
        visibility.append(item)
    source_hash, target_hash = digest(source), digest(target)
    return {
        'task_id': context['task_id'], 'context_path': str(context_path), 'trace_path': str(trace_path),
        'source': {'path': str(source), 'sha256': source_hash, 'matches_facts': source_hash == analysis['source']['sha256']},
        'delivery': {'path': str(target), 'sha256': target_hash, 'matches_review': target_hash == registered_sha,
                     'measurement_manifest': str(manifest_path) if manifest_path else 'registered contract'},
        'working_frame': {'origin_wcs': origin, 'axis_deg': angle, 'metres_per_unit': scale,
                          'unit_adoption_basis': units['adoption_basis'], 'engineering_confirmed': False},
        'area_targets': budgets, 'actual_components': actual, 'non_occupancy_components': non_occupancy,
        'facility_total_m2': sum(p.area for p in facilities.values()),
        'area_rule_calls': [{'tool_part_id': part['id'], 'input': part.get('input')} for part, _ in programs],
        'adopted_assumptions': contract['facts'].get('assumptions', []),
        'roads': {'linear_tolerance_m': TOLERANCE_M, 'union_area_m2': road_union.area,
                  'edges': edges, 'pair_gaps': pair_gaps, 'connected_components': components,
                  'cycle_rank': len(edges) - len(roads) + len(components),
                  'facility_to_new_road_gap_m': {key: polygon.distance(road_union) if roads else None
                                               for key, polygon in facilities.items()}},
        'remaining_geometry': {'boundary_id': selected['id'], 'boundary_area_m2': boundary.area,
                               'outline_count': len(outlined), 'clearance_m': clearance,
                               'remaining_area_after_clearances_m2': free.area,
                               'rectangle_packing_or_road_feasibility_proven': False},
        'display_evidence': {'modelspace_entity_count': len(model), 'redraw_order_entry_count': len(order),
                             'entities': visibility, 'meaning': 'With no redraw mapping, the renderer uses modelspace order.'},
        'scope': ['Read-only measurement of the actual saved DXF; no model calls or CAD edits.',
                  'Road topology covers new registered corridors only; existing streets and entrances are not certified.',
                  'Budget overrides are agent decisions, not proof that the user supplied or approved their values.',
                  'Remaining polygon area does not prove rectangular placement feasibility or a capacity upper bound.',
                  'No engineering-code, vehicle-turning, construction-phase, or legal-boundary certification.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--context', required=True, type=Path)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--trace', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--manifest', type=Path, help='Agent-authored manifest for an unpublished candidate; does not register or publish it')
    args = parser.parse_args()
    report = audit(args.context.resolve(), args.target.resolve(), args.trace.resolve(), args.manifest.resolve() if args.manifest else None)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({'audit': str(args.output), 'delivery_sha256': report['delivery']['sha256'],
                      'facility_total_m2': report['facility_total_m2'],
                      'road_components': len(report['roads']['connected_components']),
                      'road_cycle_rank': report['roads']['cycle_rank']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
