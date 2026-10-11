"""Resolve an agent-adopted spatial contract from the task's actual baseline.

The public facts object is a reference and adoption rationale, never an accepted
polygon payload. Each delivery check derives A/B again from the baseline bytes;
reference faces remain machine evidence, not fabricated human confirmation.
"""
from __future__ import annotations

from pathlib import Path

from shapely.geometry import shape

from .geometry import FactsError, Site, _number

SCHEMA = 'spatial-evidence/1'


def is_spatial_evidence(facts):
    return isinstance(facts, dict) and facts.get('schema') == SCHEMA


def resolve_spatial_site(context, baseline, facts):
    from .delivery import baseline_file
    from .spatial_autonomy import analyze_spatial
    from .spatial_constraints import evaluate_constraints
    from .spatial_tools import _load

    if context is None:
        raise FactsError('机器空间事实需要当前任务上下文，不能脱离任务自行提供几何。', 'missing_task_context')
    allowed = {'schema', 'facts_id', 'adopted_by', 'assumptions', 'clearance_m'}
    if not is_spatial_evidence(facts) or set(facts) - allowed:
        raise FactsError('机器事实仅接受schema、facts_id、adopted_by、assumptions及clearance_m，不接受自填单位、轮廓或确认标记。', 'invalid_spatial_contract', 'failed')
    if facts.get('adopted_by') != 'agent':
        raise FactsError('adopted_by应为agent，表示依据图内证据采用工作假设，不代表人工确认。', 'adoption_required')
    assumptions = facts.get('assumptions')
    if not isinstance(assumptions, list) or not 1 <= len(assumptions) <= 20 or any(
            not isinstance(note, str) or not note.strip() or len(note) > 1000 for note in assumptions):
        raise FactsError('assumptions需要1至20条非空采用理由，每条不超过1000字符。', 'adoption_required')
    baseline = Path(baseline).resolve()
    expected = baseline_file(context).resolve()
    if baseline != expected:
        raise FactsError('场布契约必须绑定本任务原始DXF基准。', 'wrong_baseline', 'failed')
    cached = _load(context, facts.get('facts_id'))
    if Path(cached['source']['path']).resolve() != baseline:
        raise FactsError('facts_id必须来自本任务原始输入；修改后图纸的事实不能替代基准。', 'wrong_evidence_source', 'failed')
    selected = cached.get('selected_boundary')
    if not isinstance(selected, dict) or not selected.get('id'):
        raise FactsError('原图尚未选定工作场界，先通过A查询并选择有效候选。', 'boundary_unresolved')
    # A cached file is query provenance, not authority to invent geometry.
    # Re-derive from the immutable input; no model-supplied outline is consumed.
    analysis = analyze_spatial(str(baseline), boundary_id=selected['id'])
    if analysis['source']['sha256'] != cached['source']['sha256']:
        raise FactsError('空间事实与基准文件版本不同。', 'stale_facts', 'failed')
    observation = evaluate_constraints(analysis)
    frame = observation['frame']
    if observation['status'] != 'constraints_ready' or not frame['ready']:
        raise FactsError('当前图纸的单位、主轴或工作场界仍无法形成可执行约束。', 'spatial_constraints_incomplete')
    boundary_record = analysis['selected_boundary']
    by_id = {item['id']: item for item in analysis['classifications']}
    descriptors = observation['obstacles_summary']['items']
    obstacles = [(item['id'], shape(by_id[item['id']]['geometry'])) for item in descriptors]
    clearance = _number(facts.get('clearance_m', 0), 'clearance_m', nonnegative=True)
    evidence = {'schema': SCHEMA, 'facts_id': facts['facts_id'], 'adopted_by': 'agent',
                'assumptions': list(assumptions), 'human_confirmed': False,
                'semantics_confirmed': False, 'construction_phase_confirmed': False,
                'legal_boundary_confirmed': False, 'engineering_compliance_verified': False,
                'source_rederived': True, 'units': observation['units'], 'frame': frame,
                'boundary_id': boundary_record['id'],
                'reserved_count': sum(item['tier'] == 'reserved' for item in descriptors),
                'uncertain_keepout_count': sum(item['tier'] == 'uncertain' for item in descriptors),
                'constraint_tiers': {item['id']: item['tier'] for item in descriptors},
                'scope': '仅以当前原图机器证据作为工作约束；保留及主要待定均避让，未识别对象和工程规范仍未验收。'}
    return Site(boundary=shape(boundary_record['geometry']), obstacles=obstacles,
                metres_per_unit=frame['metres_per_unit'], origin=tuple(frame['origin_wcs']),
                angle_deg=frame['axis_deg'], source_sha256=analysis['source']['sha256'],
                clearance_m=clearance, boundary_handles=(boundary_record['id'],),
                reference_mode='spatial_evidence', evidence=evidence)


def verify_spatial_references(path, site):
    """Re-extract source-instance paths from actual saved or intermediate DXF."""
    from .spatial_extract import extract_drawing
    extracted = extract_drawing(path)
    records = {item['id']: item for item in extracted['polygons']}
    expected = dict(site.obstacles)
    expected[site.boundary_handles[0]] = site.boundary
    failures = []
    for identity, baseline in expected.items():
        record = records.get(identity)
        if record is None:
            failures.append({'code': 'site_reference_changed', 'message': '保存文件缺少基准实例轮廓。', 'handles': [identity]})
            continue
        actual = shape(record['geometry'])
        area = actual.symmetric_difference(baseline).area * site.metres_per_unit ** 2
        distance = actual.hausdorff_distance(baseline) * site.metres_per_unit
        if area > 1e-6 or distance > 1e-6:
            failures.append({'code': 'site_reference_changed', 'message': '保存文件的实际实例轮廓已偏离基准。',
                             'handles': [identity], 'symmetric_difference_m2': area, 'distance_m': distance})
    return failures
