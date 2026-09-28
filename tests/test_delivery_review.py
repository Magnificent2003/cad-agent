"""Review saved geometry and acknowledge only current, delivered image bytes."""
import hashlib
import json
from pathlib import Path

import ezdxf
from PIL import Image
import pytest

from cad_domain import delivery
from cad_domain.geometry import digest
from cad_domain.layout import build_layout
from cad_domain.worker import run


@pytest.fixture
def task(tmp_path, monkeypatch):
    monkeypatch.delenv('CAD_AGENT_CONTEXT_DIR', raising=False)
    root = tmp_path / 'agent-tasks/task1'
    for folder in ('input', 'output', 'scratch'):
        (root / folder).mkdir(parents=True)
    source = root / 'input/drawing.dxf'
    doc = ezdxf.new('R2018')
    doc.units = 6
    boundary = doc.modelspace().add_lwpolyline([(0, 0), (40, 0), (40, 30), (0, 30)], close=True)
    doc.saveas(source)
    context = {'task_id': 'task1', 'session_id': 'session1', 'output_directory': str(root / 'output'),
               'scratch_directory': str(root / 'scratch'),
               'input_files': [{'format': 'dxf', 'path': str(source), 'sha256': digest(source)}]}
    binding = tmp_path / 'agent-contexts/session1.json'
    binding.parent.mkdir()
    binding.write_text(json.dumps(context))
    facts = {'source_sha256': digest(source),
             'units': {'status': 'confirmed', 'metres_per_unit': 1, 'evidence': 'Fixture author confirmed metres.'},
             'boundary': {'status': 'confirmed', 'handles': [boundary.dxf.handle], 'evidence': 'Fixture parcel.'},
             'obstacles': {'status': 'confirmed', 'handles': [], 'evidence': 'Fixture contains boundary only.'}}
    return context, binding, source, facts


def laid_out(task):
    context, _, source, facts = task
    target = Path(context['output_directory']) / 'layout.dxf'
    result = build_layout(source, target, facts=facts, components=[{'id': 'A', 'kind': 'rectangle', 'width_m': 4, 'depth_m': 3}])
    assert result['status'] == 'passed', result
    entry = delivery.register(context, target, profile='site_layout', facts=facts, components=result['components'])
    return target, entry


def fake_renderer(monkeypatch):
    calls = []
    def render(source, output, **kwargs):
        calls.append(source)
        Path(output).mkdir(parents=True, exist_ok=True)
        Image.new('RGB', (64, 48), 'blue').save(Path(output) / 'preview.png')
        return {'warnings': []}
    monkeypatch.setattr('backend.app.cad.renderer.render_drawing', render)
    return calls


def test_modified_file_requires_review_but_identical_copy_does_not(task):
    context, _, source, _ = task
    output = Path(context['output_directory']) / 'copy.dxf'
    output.write_bytes(source.read_bytes())
    assert delivery.review_delivery(context)['status'] == 'passed'
    doc = ezdxf.readfile(output)
    doc.modelspace().add_line((1, 1), (2, 2))
    doc.saveas(output)
    result = delivery.review_delivery(context)
    assert result['status'] == 'unknown'
    assert result['issues'][0]['code'] == 'review_required'
    delivery.register(context, output, profile='general')
    assert delivery.review_delivery(context)['status'] == 'passed'


def test_site_requires_matching_feedback_and_rechecks_actual_file(task, monkeypatch):
    fake_renderer(monkeypatch)
    context = task[0]
    target, entry = laid_out(task)
    report = delivery.review_file(context, target, entry=entry, render=True)
    assert report['status'] == 'passed', report
    assert delivery.review_delivery(context)['status'] == 'unknown'
    delivery.mark_feedback(context, target, report, image_sha256=report['preview']['image_sha256'])
    assert delivery.review_delivery(context)['status'] == 'passed'
    # Move geometry after review: old status and image must not permit publication.
    doc = ezdxf.readfile(target)
    doc.entitydb[entry['components'][0]['handle']].translate(100, 0, 0)
    doc.saveas(target)
    assert delivery.review_delivery(context)['status'] == 'failed'
    with pytest.raises(ValueError, match='反馈已失效'):
        delivery.mark_feedback(context, target, report, image_sha256=report['preview']['image_sha256'])


def test_worker_review_does_not_ack_before_transport(task, monkeypatch):
    fake_renderer(monkeypatch)
    context, binding, _, _ = task
    target, _ = laid_out(task)
    result = run({'context_path': str(binding), 'action': 'review',
                  'params': {'path': str(target), 'profile': 'site_layout'}})['result']
    assert result['status'] == 'passed', result
    assert delivery.review_delivery(context)['status'] == 'unknown'
    ack = run({'context_path': str(binding), 'action': 'feedback', 'params': result})
    assert ack['result']['status'] == 'passed'
    assert delivery.review_delivery(context)['status'] == 'passed'


def test_corrupt_cache_regenerated_and_hash_checked(task, monkeypatch):
    calls = fake_renderer(monkeypatch)
    target, _ = laid_out(task)
    context = task[0]
    preview = delivery.render_snapshot(context, target)
    Path(preview['path']).write_bytes(b'not a PNG')
    repaired = delivery.render_snapshot(context, target)
    assert len(calls) == 2
    assert repaired['image_sha256'] == hashlib.sha256(Path(repaired['path']).read_bytes()).hexdigest()
    delivery.render_snapshot(context, target)
    assert len(calls) == 2
    Image.new('RGB', (64, 48), 'red').save(preview['path'])
    delivery.render_snapshot(context, target)
    assert len(calls) == 3


def test_preview_parent_symlink_rejected(task, tmp_path, monkeypatch):
    calls = fake_renderer(monkeypatch)
    context = task[0]
    target, _ = laid_out(task)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (Path(context['scratch_directory']) / 'cad-review-previews').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        delivery.render_snapshot(context, target)
    assert not calls and not list(outside.iterdir())


def test_file_changed_between_check_and_render_is_not_passed(task, monkeypatch):
    fake_renderer(monkeypatch)
    target, entry = laid_out(task)
    render = delivery.render_snapshot
    def change_then_render(context, path, settings=None):
        doc = ezdxf.readfile(path)
        doc.modelspace().add_text('changed')
        doc.saveas(path)
        return render(context, path, settings)
    monkeypatch.setattr(delivery, 'render_snapshot', change_then_render)
    report = delivery.review_file(task[0], target, entry=entry, render=True)
    assert report['status'] == 'unknown'
    assert 'preview' not in report


def test_unused_invalid_contract_does_not_block_report_only(task):
    context = task[0]
    delivery.contract_path(context).write_text('corrupt')
    result = delivery.review_delivery(context)
    assert result['status'] == 'passed' and not result['files']


def test_task_paths_and_original_hash_are_enforced(task, tmp_path):
    context, binding, source, _ = task
    assert delivery.read_context(binding) == context
    with pytest.raises(ValueError):
        delivery.resolve_file(context, source, write=True)
    with pytest.raises(ValueError):
        delivery.resolve_file(context, tmp_path / 'elsewhere.dxf', write=True)
    source.write_bytes(b'changed input')
    with pytest.raises(ValueError, match='输入副本已变化'):
        delivery.baseline_file(context)


def test_tagged_layout_cannot_skip_constraints_by_general_profile(task):
    target, entry = laid_out(task)
    with pytest.raises(ValueError, match='不能降级'):
        delivery.register(task[0], target, profile='general')
    report = delivery.review_file(task[0], target, entry={'profile': 'general'})
    assert report['profile'] == 'site_layout' and report['status'] != 'passed'


def test_scratch_layout_keeps_cumulative_baseline_change_provenance(task):
    context, binding, source, facts = task
    scratch = Path(context['scratch_directory'])
    # This task starts from an existing tagged facility, so moving it changes an
    # original native entity rather than merely replacing this task's additions.
    seed = scratch / 'seed.dxf'
    seeded = build_layout(source, seed, facts=facts, components=[
        {'id': 'A', 'kind': 'rectangle', 'width_m': 4, 'depth_m': 3, 'position_m': [2, 2]},
    ])
    assert seeded['status'] == 'passed', seeded
    original_handles = {seeded['components'][0]['handle'], seeded['components'][0]['label_handle']}
    source.write_bytes(seed.read_bytes())
    seed.unlink()
    facts['source_sha256'] = digest(source)
    context['input_files'][0]['sha256'] = digest(source)
    binding.write_text(json.dumps(context))

    candidate = scratch / 'moved.dxf'
    first = run({'context_path': str(binding), 'action': 'layout', 'params': {
        'source': str(source), 'target': str(candidate), 'facts': facts,
        'components': [{'id': 'A', 'operation': 'move', 'position_m': [12, 12]}],
    }})['result']
    assert first['status'] == 'passed', first
    assert original_handles <= set(first['allowed_changes'])
    assert original_handles <= set(delivery.entry_for(context, candidate)['allowed_changes'])
    # Scratch provenance must not make a candidate a promised output file.
    assert delivery.review_delivery(context)['files'] == []

    target = Path(context['output_directory']) / 'final.dxf'
    second = run({'context_path': str(binding), 'action': 'layout', 'params': {
        'source': str(candidate), 'target': str(target), 'components': [],
    }})['result']
    assert second['status'] == 'passed', second
    entry = delivery.entry_for(context, target)
    assert original_handles <= set(entry['allowed_changes'])
    checked = delivery.review_file(context, target, entry=entry)
    assert checked['status'] == 'passed', checked
    assert original_handles <= set(checked['checks']['preservation']['allowed_changed_handles'])


def test_unchanged_nonrendering_review_preserves_delivered_image_ack(task, monkeypatch):
    fake_renderer(monkeypatch)
    context, binding, _, _ = task
    target, _ = laid_out(task)
    reviewed = run({'context_path': str(binding), 'action': 'review', 'params': {
        'path': str(target), 'profile': 'site_layout', 'render': True,
    }})['result']
    run({'context_path': str(binding), 'action': 'feedback', 'params': reviewed})
    assert delivery.review_delivery(context)['status'] == 'passed'
    acknowledged = delivery.entry_for(context, target)['feedback_sha256']

    reread = run({'context_path': str(binding), 'action': 'review', 'params': {
        'path': str(target), 'profile': 'site_layout', 'render': False,
    }})['result']
    assert reread['status'] == 'passed', reread
    assert reread['file_sha256'] == acknowledged
    assert delivery.entry_for(context, target)['feedback_sha256'] == acknowledged
    assert delivery.review_delivery(context)['status'] == 'passed'


def test_feedback_summary_bounds_large_failure_without_changing_evidence():
    report = {'status': 'failed', 'file_sha256': 'abc',
              'issues': [{'code': 'collision', 'handles': [str(i)]} for i in range(1000)],
              'checks': {'preservation': {'added_handles': [str(i) for i in range(1000)]}}}
    summary = delivery.feedback_summary(report)
    assert summary['status'] == 'failed' and summary['file_sha256'] == 'abc'
    assert len(summary['issues']) == 30 and len(report['issues']) == 1000
    assert len(summary['checks']['preservation']['added_handles']) == 30
    assert all(item['total'] == 1000 for item in summary['truncated_lists'])
