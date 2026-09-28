"""Task-bound DXF review contracts and artifact checks shared by MCP and backend.

This is a file/geometry acceptance gate, not a construction-code certification.
The agent still chooses actions and can use arbitrary Python through FreeCAD MCP.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .geometry import check_dxf, digest

CONTRACT = 'cad-review.json'


def read_context(path: str | Path) -> dict:
    """Accept a current backend binding, never an arbitrary model-supplied JSON file."""
    path = Path(path).absolute()
    expected = os.environ.get('CAD_AGENT_CONTEXT_DIR')
    if expected and path.parent.resolve() != Path(expected).resolve():
        raise ValueError('任务上下文不在当前后端绑定目录中。')
    if path.is_symlink() or path.parent.name != 'agent-contexts':
        raise ValueError('需要当前会话的后端任务绑定。')
    context = json.loads(path.read_text(encoding='utf-8'))
    task_id, session_id = context.get('task_id', ''), context.get('session_id', '')
    if not task_id or not session_id or path.name != session_id + '.json':
        raise ValueError('任务绑定与会话不一致。')
    if any(not (c.isalnum() or c in '_-') for c in task_id + session_id):
        raise ValueError('无效任务标识。')
    root = path.parent.parent / 'agent-tasks' / task_id
    for field, name in [('output_directory', 'output'), ('scratch_directory', 'scratch')]:
        if Path(context[field]).resolve() != (root / name).resolve() or (root / name).is_symlink():
            raise ValueError('任务目录与后端绑定不一致。')
    for entry in context['input_files']:
        p = Path(entry['path'])
        if p.is_symlink() or p.resolve().parent != (root / 'input').resolve():
            raise ValueError('输入文件不属于当前任务。')
    return context


def resolve_file(context: dict, path: str | Path, *, write=False) -> Path:
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(context['output_directory'] if write else context['scratch_directory']) / p
    resolved = p.resolve()
    roots = [Path(context[k]).resolve() for k in ('scratch_directory', 'output_directory')]
    if any(resolved.is_relative_to(root) and resolved != root for root in roots):
        # Refuse symlinks even if their current destination happens to be in scope.
        if p.is_symlink() or any(a.is_symlink() for a in p.parents):
            raise ValueError('不通过符号链接读写任务文件。')
        return resolved
    if not write:
        known = [item['path'] for item in context['input_files']]
        known += [item['path'] for item in context.get('previous_artifacts', [])]
        if str(resolved) in known and not p.is_symlink():
            return resolved
    raise ValueError('文件不在本轮输入、临时或输出范围中。')


def baseline_file(context: dict) -> Path:
    entry = next((item for item in context['input_files'] if item.get('format') == 'dxf'), None)
    if not entry:
        raise ValueError('当前任务没有可检查的 DXF 输入。')
    p = Path(entry['path'])
    if digest(p) != entry['sha256']:
        raise ValueError('输入副本已变化，不能以变化后的输入作为验收基准。')
    return p


def contract_path(context: dict) -> Path:
    return Path(context['scratch_directory']) / CONTRACT


def load_contract(context: dict) -> dict:
    path = contract_path(context)
    if not path.exists():
        return {'schema': 1, 'task_id': context['task_id'], 'entries': {}}
    if path.is_symlink():
        raise ValueError('检查契约不能是符号链接。')
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('schema') != 1 or data.get('task_id') != context['task_id'] or not isinstance(data.get('entries'), dict):
        raise ValueError('检查契约不属于当前任务或格式无效。')
    return data


def save_contract(context: dict, data: dict):
    target = contract_path(context)
    temporary = target.with_suffix('.tmp')
    if target.is_symlink() or temporary.is_symlink():
        raise ValueError('检查契约路径不可写。')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(target)


def file_key(context: dict, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(Path(context['output_directory']).resolve()).as_posix()
    except ValueError:
        return None


def _entry_slot(context: dict, path: Path) -> tuple[str, str]:
    key = file_key(context, path)
    if key is not None:
        return 'entries', key
    path = resolve_file(context, path)
    try:
        return 'scratch_entries', path.relative_to(Path(context['scratch_directory']).resolve()).as_posix()
    except ValueError:
        # Original inputs have no declared edit contract.
        return 'input_entries', str(path)


def entry_for(context: dict, path: Path) -> dict:
    contract = load_contract(context)
    group, key = _entry_slot(context, path)
    return contract.get(group, {}).get(key, {})


def register(context: dict, path: Path, *, profile: str, facts=None, components=None, allowed_changes=None) -> dict:
    if profile not in {'general', 'site_layout'}:
        raise ValueError('profile 必须是 general 或 site_layout。')
    contract = load_contract(context)
    group, key = _entry_slot(context, path)
    previous = contract.get(group, {}).get(key, {})
    if previous.get('profile') == 'site_layout' and profile != 'site_layout':
        raise ValueError('已登记的场布成果不能降级为普通文件检查。')
    entry = {
        'profile': profile,
        'facts': facts if facts is not None else previous.get('facts', contract.get('site_facts')),
        'components': components if components is not None else previous.get('components'),
        'allowed_changes': list(dict.fromkeys([*previous.get('allowed_changes', []), *(allowed_changes or [])])),
    }
    if entry['facts'] is not None and profile == 'site_layout':
        contract['site_facts'] = entry['facts']
    # Preserve acknowledgement of unchanged image bytes across non-rendered checks.
    if path.is_file() and previous.get('feedback_sha256') == digest(path):
        for field in ('checked_sha256', 'feedback_sha256', 'preview'):
            if field in previous:
                entry[field] = previous[field]
    # Scratch provenance participates in later edits without publishing scratch files.
    if group != 'input_entries':
        contract.setdefault(group, {})[key] = entry
    save_contract(context, contract)
    return entry


def render_snapshot(context: dict, path: Path, settings: dict | None = None) -> dict:
    """Render saved bytes and validate the exact PNG accepted by image transport."""
    from agent.preview import read_preview
    from backend.app.cad.renderer import render_drawing
    sha = digest(path)
    output = resolve_file(context, Path(context['scratch_directory']) / 'cad-review-previews' / sha[:20], write=True)
    png = resolve_file(context, output / 'preview.png', write=True)
    metadata = resolve_file(context, output / 'preview.json', write=True)
    # Renderer also writes SVG; protect every derived output from symlink escapes.
    resolve_file(context, output / 'preview.svg', write=True)
    if png.is_file() and metadata.is_file():
        try:
            result = json.loads(metadata.read_text(encoding='utf-8'))
            data, _ = read_preview(png)
            if (result.get('drawing_sha256') == sha and result.get('path') == str(png)
                    and result.get('image_sha256') == hashlib.sha256(data).hexdigest()):
                return result
        except (ValueError, OSError):
            pass  # Discard corrupt derived cache and render current drawing again.
    result = render_drawing(str(path), str(output), settings=settings or context.get('review', {}).get('render_settings'))
    data, _ = read_preview(png)
    if digest(path) != sha:
        raise ValueError('渲染期间图纸发生变化，预览已失效。')
    preview = {'path': str(png), 'drawing_sha256': sha,
               'image_sha256': hashlib.sha256(data).hexdigest(), 'warnings': result.get('warnings', [])}
    metadata.write_text(json.dumps(preview, ensure_ascii=False, indent=2), encoding='utf-8')
    return preview


def _has_site_tags(path: Path) -> bool:
    import ezdxf
    doc = ezdxf.readfile(path)
    return any(e.has_xdata('CAD_AGENT_SITE') for e in doc.modelspace())


def review_file(context: dict, path: Path, *, entry: dict, render=False, settings=None) -> dict:
    profile = entry.get('profile', 'general')
    if profile == 'general' and _has_site_tags(path):
        profile = 'site_layout'
    report = check_dxf(str(baseline_file(context)), str(path), profile=profile,
                       facts=entry.get('facts'), components=entry.get('components'),
                       allowed_changes=entry.get('allowed_changes', []))
    report.update(profile=profile, path=str(path), name=path.name)
    if render:
        try:
            preview = render_snapshot(context, path, settings)
            if preview['drawing_sha256'] != report.get('file_sha256'):
                raise ValueError('检查与预览的 DXF 版本不同，必须重新检查。')
            report['preview'] = preview
        except Exception as exc:
            report.setdefault('issues', []).append({'code': 'preview_unavailable', 'message': str(exc), 'handles': []})
            if report.get('status') == 'passed':
                report['status'] = 'unknown'
    return report


def mark_feedback(context: dict, path: Path, report: dict, *, image_sha256: str):
    """Acknowledge bytes accepted by image transport; this is not proof of vision."""
    from agent.preview import read_preview
    preview = report.get('preview') or {}
    data, _ = read_preview(resolve_file(context, preview.get('path', '')))
    if (hashlib.sha256(data).hexdigest() != image_sha256 or preview.get('image_sha256') != image_sha256
            or preview.get('drawing_sha256') != report.get('file_sha256')
            or digest(path) != report.get('file_sha256')):
        raise ValueError('图纸、检查与已发送的预览不一致，反馈已失效。')
    group, key = _entry_slot(context, path)
    contract = load_contract(context)
    entry = contract.get(group, {}).get(key)
    if entry is None:
        return
    entry.update(checked_sha256=report['file_sha256'], feedback_sha256=report['file_sha256'], preview=preview)
    save_contract(context, contract)


def review_delivery(context: dict) -> dict:
    """Independently inspect current outputs; never trust a model's saved 'passed'."""
    root = Path(context['output_directory'])
    issues, files = [], []
    paths = [p for p in sorted(root.rglob('*')) if p.is_file() and not p.is_symlink()
             and p.suffix.lower() == '.dxf' and not any(part.startswith('.') for part in p.relative_to(root).parts)]
    if not paths:
        manifest = contract_path(context)
        return {'status': 'passed', 'files': [], 'issues': [],
                'contract_sha256': digest(manifest) if manifest.is_file() else None,
                'scope': 'No output DXFs to review; this does not certify non-DXF artifacts.'}
    try:
        contract = load_contract(context)
        baseline = baseline_file(context)
    except (ValueError, OSError, KeyError) as exc:
        return {'status': 'failed', 'files': [], 'issues': [{'code': 'review_context_invalid', 'message': str(exc)}]}
    for path in paths:
        name = path.relative_to(root).as_posix()
        try:
            resolve_file(context, path)
            entry = contract['entries'].get(name)
            # An unmodified input copy adds no drawing changes to validate.
            if entry is None and digest(path) == digest(baseline):
                files.append({'name': name, 'path': str(path), 'status': 'passed', 'profile': 'unchanged_copy',
                              'file_sha256': digest(path), 'issues': []})
                continue
            if entry is None:
                report = {'name': name, 'path': str(path), 'status': 'unknown', 'file_sha256': digest(path),
                          'issues': [{'code': 'review_required', 'message': '修改后的 DXF 尚未登记检查范围；调用 cad_review。', 'handles': []}]}
            else:
                report = review_file(context, path, entry=entry)
                report['name'] = name
                if (report.get('status') == 'passed' and report['profile'] == 'site_layout'
                        and entry.get('feedback_sha256') != report.get('file_sha256')):
                    report['status'] = 'unknown'
                    report.setdefault('issues', []).append({
                        'code': 'final_view_required', 'message': '当前成果还没有同源图像反馈；调用 cad_review(render=True) 后复核。', 'handles': []})
            files.append(report)
            issues.extend({**issue, 'file': name} for issue in report.get('issues', []) if report.get('status') != 'passed')
        except Exception as exc:
            issue = {'code': 'review_failed', 'message': str(exc), 'file': name, 'handles': []}
            issues.append(issue)
            files.append({'name': name, 'path': str(path), 'status': 'failed', 'issues': [issue]})
    status = 'failed' if any(item['status'] == 'failed' for item in files) else 'unknown' if any(item['status'] != 'passed' for item in files) else 'passed'
    manifest = contract_path(context)
    result = {'status': status, 'files': files, 'issues': issues,
              'contract_sha256': digest(manifest) if manifest.is_file() else None,
              'scope': 'DXF native preservation and declared 2D layout constraints; not construction-code certification.'}
    resolve_file(context, Path(context['scratch_directory']) / 'delivery-review.json', write=True).write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    return result


def feedback_preview(context: dict, report: dict, settings=None) -> Path | None:
    """Attach one failing/current result to a continuation; remaining files stay explicit."""
    candidates = sorted(report.get('files', []), key=lambda item: item.get('status') == 'passed')
    for item in candidates:
        try:
            path = resolve_file(context, item['path'])
            preview = render_snapshot(context, path, settings)
            if preview['drawing_sha256'] != item.get('file_sha256'):
                continue
            item['preview'] = preview
            return Path(preview['path'])
        except Exception:
            continue
    return None


def feedback_summary(report: dict, *, limit=30) -> dict:
    """Bound model context while retaining full reports in task scratch logs."""
    truncated = []
    def bounded(value, path):
        if isinstance(value, list):
            if len(value) > limit:
                truncated.append({'path': path, 'total': len(value), 'shown': limit})
            return [bounded(item, path + '[]') for item in value[:limit]]
        if isinstance(value, dict):
            return {key: bounded(item, path + '.' + key) for key, item in value.items()}
        return value
    result = bounded(report, 'report')
    if truncated:
        result['truncated_lists'] = truncated
    return result
