"""JSON bridge: reuse the project's geometry environment from the existing MCP."""
from __future__ import annotations

import json
import sys

from .delivery import (baseline_file, entry_for, feedback_summary, load_contract, mark_feedback, read_context,
                       register, resolve_file, review_file)
from .geometry import inspect_dxf
from .layout import build_layout


def run(request: dict) -> dict:
    context = read_context(request['context_path'])
    params = request.get('params') or {}
    baseline = baseline_file(context)
    action = request.get('action')
    if action == 'site_rules':
        from .spatial_program import site_rules
        return {'result': site_rules(context, params)}
    if action in {'spatial_facts', 'spatial_constraints'}:
        from .spatial_tools import inspect_spatial, query_constraints
        handler = inspect_spatial if action == 'spatial_facts' else query_constraints
        return {'result': handler(context, params)}
    if action == 'feedback':
        path = resolve_file(context, params['path'])
        mark_feedback(context, path, params, image_sha256=params['preview']['image_sha256'])
        return {'result': {'status': 'passed'}}
    if action == 'inspect':
        path = resolve_file(context, params.get('path') or baseline)
        result = inspect_dxf(str(path), handles=params.get('handles'), limit=params.get('limit', 30))
        return {'result': result}
    if action == 'layout':
        source = resolve_file(context, params.get('source') or baseline)
        target = resolve_file(context, params.get('target') or 'site-layout.dxf', write=True)
        if source.suffix.lower() != '.dxf' or target.suffix.lower() != '.dxf':
            raise ValueError('当前参数化布局只处理 DXF；DWG 先由现有格式服务转换。')
        contract = load_contract(context)
        facts = params.get('facts') or contract.get('site_facts')
        if facts is None:
            raise ValueError('先通过 cad_spatial_facts 获取当前输入证据，并提供 spatial-evidence/1 采用约定；不能猜测单位、场界和空地。')
        result = build_layout(str(source), str(target), facts=facts, components=params.get('components') or [], baseline=str(baseline), context=context)
        if result.get('status') == 'passed':
            source_entry = entry_for(context, source)
            register(context, target, profile='site_layout', facts=facts, components=result.get('components'),
                     allowed_changes=[*source_entry.get('allowed_changes', []), *result.get('allowed_changes', [])])
            result['next_step'] = '调用 cad_review(profile="site_layout", render=True)，检查实际文件和同源预览后再交付。'
        return {'result': result}
    if action == 'review':
        path = resolve_file(context, params.get('path') or '')
        if path.suffix.lower() != '.dxf':
            raise ValueError('本轮工程检查仅支持实际 DXF 文件，不把其他格式标为已通过。')
        old = entry_for(context, path)
        # The GUI's default profile must not silently downgrade an established site plan.
        profile = params.get('profile') or old.get('profile', 'general')
        entry = register(context, path, profile=profile, facts=params.get('facts'),
                         components=params.get('components'), allowed_changes=params.get('allowed_changes'))
        result = review_file(context, path, entry=entry, render=params.get('render', True))
        from pathlib import Path
        report_path = resolve_file(context, Path(context['scratch_directory']) / 'cad-review-reports' /
                                   (result['file_sha256'] + '.json'), write=True)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        response = {'result': {**feedback_summary(result), 'full_report_path': str(report_path)}}
        if result.get('preview'):
            response['preview'] = result['preview']['path']
        return response
    raise ValueError('未知的 CAD 领域动作。')


def main():
    try:
        request = json.load(sys.stdin)
        response = run(request)
    except (ValueError, KeyError, OSError, TypeError) as exc:
        response = {'result': {'status': 'unknown', 'success': False, 'error': str(exc),
                               'issues': [{'code': 'request_rejected', 'message': str(exc)}]}}
    print(json.dumps(response, ensure_ascii=False, allow_nan=False))


if __name__ == '__main__':
    main()
