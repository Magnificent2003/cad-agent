"""Task-scoped, file-derived spatial facts and constraint queries for MCP.

No project review file, successful layout or prescribed road network is loaded.
Full geometry stays in the task scratch area; the model queries bounded slices.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from shapely.geometry import shape

from .delivery import baseline_file, resolve_file
from .geometry import digest


FACTS_SCHEMA = 'spatial-task-facts/0.1'
CATEGORIES = {'retained', 'suppressed', 'uncertain', 'non_building'}
REPORT_SCHEMA = 'spatial-task-constraint-report/0.1'
RESPONSE_BYTE_BUDGET = 38_000


def _count(value, name, maximum, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f'{name} 必须是 {minimum}～{maximum} 之间的整数。')
    return value


def _path(context, facts_id):
    if not isinstance(facts_id, str) or not re.fullmatch(r'[a-f0-9]{64}', facts_id):
        raise ValueError('facts_id 必须使用本轮 cad_spatial_facts 返回的完整编号。')
    return resolve_file(context, Path(context['scratch_directory']) / 'spatial-facts' / (facts_id + '.json'), write=True)


def _serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def _algorithm_hash():
    names = ('spatial_tools.py', 'spatial_autonomy.py', 'spatial_extract.py', 'spatial_facts.py', 'geometry.py', 'tolerances.py')
    fingerprint = hashlib.sha256()
    for name in names:
        fingerprint.update(name.encode())
        fingerprint.update(Path(__file__).with_name(name).read_bytes())
    return fingerprint.hexdigest()


def _load(context, facts_id):
    path = _path(context, facts_id)
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != facts_id:
        raise ValueError('空间事实内容已改变，请重新分析当前图纸，不沿用旧编号。')
    saved = json.loads(payload)
    if saved.get('schema') != FACTS_SCHEMA or saved.get('task_id') != context['task_id']:
        raise ValueError('空间事实不属于当前任务。')
    if saved.get('algorithm_sha256') != _algorithm_hash():
        raise ValueError('空间算法已更新，请从当前图纸重新提取事实。')
    source = resolve_file(context, saved['source_path'])
    if digest(source) != saved['analysis']['source']['sha256']:
        raise ValueError('图纸已修改，旧空间事实失效，请重新调用 cad_spatial_facts。')
    return saved['analysis']


def _brief(item, *, geometry=False):
    keep = ('id', 'label', 'layer', 'category', 'confidence', 'machine_supported', 'reason_codes', 'covered_by',
            'conflicts_with', 'context_role', 'area_native2', 'human_confirmed', 'status', 'engineering_role_confirmed')
    value = {key: item[key] for key in keep if key in item}
    if item.get('geometry'):
        value['bounds_native'] = list(shape(item['geometry']).bounds)
        if geometry:
            value['geometry_native'] = item['geometry']
    return value



def _issue_brief(issue):
    # Source inventories can contain thousands of IDs. Keep the diagnostic and
    # counts in the observation; the source-bound full record stays on disk.
    brief = {}
    for key, value in issue.items():
        if isinstance(value, list) and len(value) > 5:
            brief[key] = value[:5]
            brief[key + '_total'] = len(value)
        else:
            brief[key] = value
    return brief


def _issue_summary(issues):
    by_code = {}
    for item in issues:
        by_code.setdefault(item.get('code', 'unknown'), item)
    return {'total': len(issues), 'counts': dict(Counter(i.get('code', 'unknown') for i in issues)),
            'examples': [_issue_brief(item) for item in list(by_code.values())[:12]],
            'scope': '每类问题展示一项，实体编号列表最多展示5个；完整问题可用section=issues分页查询。'}


def _page(items, offset, limit):
    return {'offset': offset, 'returned': len(items[offset:offset + limit]), 'matched_total': len(items),
            'next_offset': offset + limit if offset + limit < len(items) else None}


def _wire_size(value):
    # Match the MCP bridge's JSON separators, including its spaces.
    return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8'))


def _constraint_algorithm_hash():
    fingerprint = hashlib.sha256()
    for name in ('spatial_constraints.py', 'spatial_tools.py', 'tolerances.py'):
        fingerprint.update(name.encode())
        fingerprint.update(Path(__file__).with_name(name).read_bytes())
    return fingerprint.hexdigest()


def _report_path(context, report_id):
    if not isinstance(report_id, str) or not re.fullmatch(r'[a-f0-9]{64}', report_id):
        raise ValueError('report_id 必须使用本轮约束检查返回的完整报告编号。')
    return resolve_file(context, Path(context['scratch_directory']) / 'spatial-constraint-reports' / (report_id + '.json'), write=True)


def _save_report(context, facts_id, analysis, result):
    saved = {'schema': REPORT_SCHEMA, 'task_id': context['task_id'], 'facts_id': facts_id,
             'source_sha256': analysis['source']['sha256'],
             'constraint_algorithm_sha256': _constraint_algorithm_hash(), 'result': result}
    payload = _serialized(saved)
    report_id = hashlib.sha256(payload).hexdigest()
    target = _report_path(context, report_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return report_id, saved


def _load_report(context, report_id, facts_id):
    payload = _report_path(context, report_id).read_bytes()
    if hashlib.sha256(payload).hexdigest() != report_id:
        raise ValueError('约束报告内容已改变，请重新检查完整候选集合。')
    saved = json.loads(payload)
    if saved.get('schema') != REPORT_SCHEMA or saved.get('task_id') != context['task_id']:
        raise ValueError('约束报告不属于当前任务。')
    if facts_id and facts_id != saved.get('facts_id'):
        raise ValueError('报告与传入的 facts_id 不一致。')
    # Revalidate source bytes and A's algorithm even when merely viewing a page.
    analysis = _load(context, saved['facts_id'])
    if analysis['source']['sha256'] != saved.get('source_sha256'):
        raise ValueError('约束报告对应的源图已改变，请重新提取并检查。')
    if saved.get('constraint_algorithm_sha256') != _constraint_algorithm_hash():
        raise ValueError('约束算法已更新，请重新检查完整候选集合。')
    return saved


def _proposal_statuses(result, *, include_details=True):
    statuses = []
    for index, item in enumerate(result.get('proposals', [])):
        identity = item.get('id')
        entry = {'proposal_index': index, 'id': identity[:120] if isinstance(identity, str) else identity,
                 'status': item['status'], 'conflict_count': len(item.get('conflicts', [])),
                 'unresolved_count': len(item.get('unresolved', [])),
                 'intersection_count': len(item.get('intersections', []))}
        if isinstance(identity, str) and len(identity) > 120:
            entry['id_display_truncated'] = True
        if 'area_m2' in item:
            entry['area_m2'] = item['area_m2']
        if include_details:
            if 'placement' in item:
                entry['placement'] = item['placement']
            boundary = item.get('boundary_check', {})
            alignment = item.get('alignment', {})
            entry['measurements'] = {
                'outside_area_m2': boundary.get('outside_area_m2'),
                'boundary_gap_m': boundary.get('distance_to_boundary_m'),
                'required_clearance_m': item.get('clearance_m'),
                'maximum_axis_error_deg': alignment.get('maximum_edge_error_deg'),
                'axis_alignment_required': alignment.get('required'),
                'nearest_outlines': item.get('nearest_outlines')}
        statuses.append(entry)
    return statuses


def _evaluation_status(result):
    statuses = {item['status'] for item in result.get('proposals', [])}
    if statuses & {'invalid_proposal', 'conflict'}:
        return 'conflict'
    if result.get('status') == 'constraints_incomplete' or 'requires_review' in statuses:
        return 'requires_review'
    if not statuses:
        return 'observation_only'
    if statuses == {'coverage_not_evaluated'}:
        return 'coverage_not_evaluated'
    return 'no_detected_conflict'


def _report_binding(saved, report_id):
    return {'facts_id': saved['facts_id'], 'report_id': report_id,
            'source_sha256': saved['source_sha256'],
            'constraint_algorithm_sha256': saved['constraint_algorithm_sha256'],
            'evaluation_status': _evaluation_status(saved['result'])}


def _summary_response(saved, report_id, obstacle_offset, obstacle_limit):
    full = saved['result']
    result = deepcopy({key: value for key, value in full.items() if key not in {'proposals', 'pairwise', 'analysis_issues'}})
    result.update(_report_binding(saved, report_id))
    result['analysis_issues'] = _issue_summary(full.get('analysis_issues', []))
    result['proposals'] = _proposal_statuses(full)
    pairs = full.get('pairwise', [])
    result['pairwise_summary'] = {'checked_pairs': len(pairs), 'conflicting_pairs': sum(bool(row['conflict']) for row in pairs),
                                'allowed_road_pairs': sum(bool(row['road_junction_allowed']) for row in pairs)}
    samples = []
    for index, item in enumerate(full.get('proposals', [])):
        for key in ('conflicts', 'unresolved'):
            for problem in item.get(key, [])[:1]:
                samples.append({'proposal_index': index, 'proposal_id': item.get('id'), 'kind': key, 'detail': problem})
    result['problem_samples'] = samples[:12]
    obstacles = result['obstacles_summary']
    items = sorted(obstacles['items'], key=lambda item: (item['tier'] != 'reserved', item['id']))
    obstacles['items'] = items[obstacle_offset:obstacle_offset + obstacle_limit]
    obstacles['page'] = _page(items, obstacle_offset, obstacle_limit)
    obstacles['scope'] = '仅摘要分页；全部独立占地及本次完整候选集合均已参与检查。'
    result['response_scope'] = '全部候选状态与计数完整保留；问题只展示少量样本，完整结果已经落盘。'
    result['next_step'] = '查看明细：仅传 report_id，可选 proposal_id 与 offset/limit；改位置后须连同其他当前候选一起重新检查。报告读取不会累计新候选。'
    result['response_byte_budget'] = RESPONSE_BYTE_BUDGET
    # Trim display-only detail, never checks or the complete proposal statuses.
    while _wire_size(result) > RESPONSE_BYTE_BUDGET and obstacles['items']:
        obstacles['items'].pop()
        consumed = len(obstacles['items'])
        obstacles['page'] = _page(items, obstacle_offset, consumed)
        obstacles['page']['next_offset'] = obstacle_offset + consumed if consumed else obstacle_offset
        result['response_trimmed'] = True
    while _wire_size(result) > RESPONSE_BYTE_BUDGET and result['problem_samples']:
        result['problem_samples'].pop()
        result['response_trimmed'] = True
    if _wire_size(result) > RESPONSE_BYTE_BUDGET:
        for key in ('geometry_wcs', 'geometry_local_m'):
            result.get('boundary', {}).pop(key, None)
        for container, key in ((result.get('units', {}), 'evidence'),
                               (result.get('frame', {}), 'boundary_support')):
            if isinstance(container.get(key), list):
                container[key + '_total'] = len(container[key])
                container[key] = []
        result['analysis_issues'].pop('examples', None)
        result['assumptions_total'] = len(result.get('assumptions', []))
        result['assumptions'] = []
        result['response_trimmed'] = True
    if _wire_size(result) > RESPONSE_BYTE_BUDGET:
        # Even invalid, arbitrarily verbose input retains one status per index.
        result = {**_report_binding(saved, report_id), 'status': full['status'],
                  'summary': full.get('summary', {}), 'proposals': _proposal_statuses(full),
                  'pairwise_summary': result['pairwise_summary'], 'response_trimmed': True,
                  'response_scope': '只保留完整状态与计数；所有检查细节请按report_id分页读取。',
                  'next_step': '传report_id与proposal_id读取该候选，offset/limit用于记录分页；不需要重新提交候选。'}
    if _wire_size(result) > RESPONSE_BYTE_BUDGET:
        result['proposals'] = _proposal_statuses(full, include_details=False)
        result['placement_details_omitted_due_to_budget'] = True
        result['response_scope'] = '完整状态与计数均保留；本批坐标和量测较多，请按report_id与proposal_id读取实际锚点、中心、角点和净距。'
    if _wire_size(result) > RESPONSE_BYTE_BUDGET:
        raise ValueError('候选状态包含过长的标识信息，请使用简短字符串id。完整检查已保存。')
    return result


def _record_parts(path, value):
    record = {'path': path, 'value': value}
    if _wire_size(record) <= 24_000:
        return [record]
    # Rare oversized individual values remain retrievable, not silently cut.
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    chunks = [text[index:index + 4000] for index in range(0, len(text), 4000)]
    return [{'path': path, 'encoding': 'json_text_fragment', 'fragment_index': index,
             'fragment_total': len(chunks), 'text': chunk}
            for index, chunk in enumerate(chunks)]


def _detail_records(result, proposal_id):
    records = []
    if not proposal_id:
        for key, value in result.items():
            if key in {'proposals', 'pairwise'}:
                continue
            if key == 'obstacles_summary':
                records.extend(_record_parts([key], {name: item for name, item in value.items() if name != 'items'}))
                for index, item in enumerate(value.get('items', [])):
                    records.extend(_record_parts([key, 'items', index], item))
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    records.extend(_record_parts([key, index], item))
            else:
                records.extend(_record_parts([key], value))
    selected = [(index, item) for index, item in enumerate(result.get('proposals', []))
                if not proposal_id or item.get('id') == proposal_id]
    if proposal_id and not selected:
        raise ValueError('该约束报告不存在指定proposal_id，请按默认摘要中的id查询。')
    for index, item in selected:
        prefix = ['proposals', index]
        metadata = {key: value for key, value in item.items()
                    if key not in {'geometry_local_m', 'geometry_wcs', 'conflicts', 'unresolved', 'intersections', 'distances'}}
        records.extend(_record_parts(prefix, metadata))
        for key in ('conflicts', 'unresolved', 'intersections', 'distances'):
            for row_index, value in enumerate(item.get(key, [])):
                records.extend(_record_parts([*prefix, key, row_index], value))
        for key in ('geometry_local_m', 'geometry_wcs'):
            if key in item:
                records.extend(_record_parts([*prefix, key], item[key]))
    if not proposal_id:
        for index, item in enumerate(result.get('pairwise', [])):
            records.extend(_record_parts(['pairwise', index], item))
    return records


def _detail_response(saved, report_id, proposal_id, offset, limit):
    records = _detail_records(saved['result'], proposal_id)
    response = {**_report_binding(saved, report_id), 'status': 'report_page',
                'proposal_id': proposal_id, 'records': [], 'page': {},
                'scope': '此页读取同一次完整检查，未重新计算或累计候选；offset按明细记录计数。',
                'fragment_note': '仅超长单项使用json_text_fragment；按相同path与fragment_index顺序拼接text后解析JSON。'}
    for record in records[offset:offset + limit]:
        response['records'].append(record)
        response['page'] = _page(records, offset, len(response['records']))
        if _wire_size(response) > RESPONSE_BYTE_BUDGET:
            response['records'].pop()
            break
    response['page'] = _page(records, offset, len(response['records']))
    if offset < len(records) and not response['records']:
        raise ValueError('报告标识过长，无法在单页内展示；请使用简短proposal_id查询。')
    if _wire_size(response) > RESPONSE_BYTE_BUDGET:
        raise ValueError('报告分页超出显示预算，请缩短查询标识。')
    return response


def _bounded_facts_response(response, context, selected, offset):
    """A stays paginated; large requested geometry cannot erase all evidence."""
    if _wire_size(response) <= RESPONSE_BYTE_BUDGET:
        return response
    response['response_trimmed'] = True
    response['full_facts_path'] = str(_path(context, response['facts_id']))
    response['display_note'] = '本页按显示字节预算缩短；next_offset继续查询。超长单个几何仍保存在完整事实文件，可通过现有Python工具只读取指定id，不要打印全文。'
    if 'outlines' not in response:
        response['issues'] = [{'code': item.get('code'), 'message': str(item.get('message', ''))[:500]}
                              for item in response.get('issues', [])]
    else:
        while _wire_size(response) > RESPONSE_BYTE_BUDGET and len(response['outlines']) > 1:
            response['outlines'].pop()
        response['page'] = _page(selected, offset, len(response['outlines']))
        if _wire_size(response) > RESPONSE_BYTE_BUDGET:
            for item in response['outlines']:
                if 'geometry_native' in item:
                    item.pop('geometry_native')
                    item['geometry_omitted_due_to_size'] = True
    if _wire_size(response) > RESPONSE_BYTE_BUDGET:
        response['boundary_candidates'] = [{key: item[key] for key in ('id', 'label', 'layer', 'status', 'area_native2') if key in item}
                                           for item in response.get('boundary_candidates', [])]
        selected_boundary = response.get('selected_boundary')
        if isinstance(selected_boundary, dict):
            response['selected_boundary'] = {key: value for key, value in selected_boundary.items() if key != 'geometry'}
        response.get('diagnostics', {}).pop('examples', None)
    if _wire_size(response) > RESPONSE_BYTE_BUDGET:
        response['assumptions'] = []
        response['units'] = {key: value for key, value in response.get('units', {}).items() if key != 'evidence'}
        if 'outlines' in response:
            response['outlines'] = [{key: item[key] for key in ('id', 'category', 'machine_supported', 'human_confirmed') if key in item}
                                    for item in response['outlines']]
    if _wire_size(response) > RESPONSE_BYTE_BUDGET:
        raise ValueError('图纸摘要仍超过显示预算，请按更少的ids和limit=1读取，或用当前任务Python只读取指定事实。')
    return response


def inspect_spatial(context, params):
    facts_id = params.get('facts_id') or ''
    if facts_id:
        if params.get('path') or params.get('boundary_id'):
            raise ValueError('查询已有事实时只传 facts_id；切换文件或场界应重新分析并取得新编号。')
        analysis = _load(context, facts_id)
    else:
        source = resolve_file(context, params.get('path') or baseline_file(context))
        if source.suffix.lower() != '.dxf':
            raise ValueError('空间事实入口读取 DXF；DWG 请使用当前任务已转换的 DXF 副本。')
        from .spatial_autonomy import analyze_spatial
        analysis = analyze_spatial(str(source), boundary_id=params.get('boundary_id') or None)
        saved = {'schema': FACTS_SCHEMA, 'task_id': context['task_id'], 'source_path': str(source),
                 'algorithm_sha256': _algorithm_hash(), 'analysis': analysis}
        payload = _serialized(saved)
        facts_id = hashlib.sha256(payload).hexdigest()
        target = _path(context, facts_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    offset = _count(params.get('offset', 0), 'offset', 1_000_000)
    limit = _count(params.get('limit', 15), 'limit', 40, minimum=1)
    section = params.get('section', 'outlines')
    if section not in {'outlines', 'issues'}:
        raise ValueError('section 支持 outlines 或 issues。')
    category = params.get('category') or ''
    if category and category not in CATEGORIES:
        raise ValueError('category 支持 retained、suppressed、uncertain、non_building。')
    ids = params.get('ids') or []
    if not isinstance(ids, list) or len(ids) > 40 or not all(isinstance(value, str) for value in ids):
        raise ValueError('ids 是至多40个源实例编号的列表。')
    if section == 'issues':
        issues = analysis.get('issues', [])
        response = {'status': 'analyzed', 'facts_id': facts_id, 'issues': [_issue_brief(item) for item in issues[offset:offset + limit]],
                    'page': _page(issues, offset, limit), 'diagnostics': _issue_summary(issues)}
        return _bounded_facts_response(response, context, issues, offset)
    records = analysis['classifications']
    boundary_records = [{**item, 'category': 'boundary_candidate', 'human_confirmed': False}
                        for item in analysis.get('boundary_candidates', [])]
    known = {item['id'] for item in [*records, *boundary_records]}
    if set(ids) - known:
        raise ValueError('未在本轮空间事实找到这些编号：' + ', '.join(sorted(set(ids) - known)))
    rank = {'retained': 0, 'uncertain': 1, 'suppressed': 2, 'non_building': 3}
    queryable = [*records, *boundary_records] if ids else records
    selected = sorted((item for item in queryable if (not category or item['category'] == category)
                       and (not ids or item['id'] in ids)),
                      key=lambda item: (rank.get(item['category'], 9), item['id']))
    boundaries = [{key: item[key] for key in ('id', 'label', 'layer', 'status', 'area_native2', 'geometry') if key in item}
                  for item in analysis.get('boundary_candidates', [])]
    response = {'status': 'analyzed', 'facts_id': facts_id, 'source': analysis['source'],
            'summary': analysis.get('summary', {}), 'counts': dict(Counter(item['category'] for item in records)),
            'units': analysis.get('units', {}), 'boundary_candidates': boundaries,
            'selected_boundary': analysis.get('selected_boundary'),
            'assumptions': analysis.get('assumptions', []), 'diagnostics': _issue_summary(analysis.get('issues', [])),
            'outlines': [_brief(item, geometry=bool(ids)) for item in selected[offset:offset + limit]],
            'page': _page(selected, offset, limit),
            'scope': '仅依据当前DXF生成；未载入人工确认记录、既有布局或项目专属主轴。候选和待定不是已核实建筑。',
            'next_step': '需要施工参照轴、局部坐标或候选范围的几何约束时，使用本次 facts_id 调用 cad_spatial_constraints。需要单个轮廓坐标时，以 facts_id 和 ids 再次查询本工具。'}

    return _bounded_facts_response(response, context, selected, offset)

def query_constraints(context, params):
    facts_id = params.get('facts_id') or ''
    report_id = params.get('report_id') or ''
    proposal_id = params.get('proposal_id') or ''
    if not isinstance(proposal_id, str):
        raise ValueError('proposal_id 必须为候选字符串编号。')
    if report_id:
        if params.get('proposals') is not None:
            raise ValueError('读取报告时不再传proposals；修改候选后用facts_id提交完整集合重新检查。')
        offset = _count(params.get('offset', 0), 'offset', 1_000_000)
        limit = _count(params.get('limit', 15), 'limit', 40, minimum=1)
        saved = _load_report(context, report_id, facts_id)
        return _detail_response(saved, report_id, proposal_id, offset, limit)
    if proposal_id or params.get('offset', 0) or params.get('limit', 15) != 15:
        raise ValueError('proposal_id与offset/limit只用于report_id报告查询；占地摘要使用obstacle_offset/obstacle_limit。')
    offset = _count(params.get('obstacle_offset', 0), 'obstacle_offset', 1_000_000)
    limit = _count(params.get('obstacle_limit', 12), 'obstacle_limit', 30, minimum=1)
    analysis = _load(context, facts_id)
    from .spatial_constraints import evaluate_constraints
    result = evaluate_constraints(analysis, proposals=params.get('proposals'))
    report_id, saved = _save_report(context, facts_id, analysis, result)
    return _summary_response(saved, report_id, offset, limit)
