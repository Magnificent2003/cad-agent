"""Exercise saved-file acceptance and same-session repair through the live task loop."""
from __future__ import annotations

import json
from pathlib import Path

import ezdxf
from PIL import Image
import pytest

from cad_domain import delivery
from cad_domain.geometry import digest
from tests.test_agent_files import app_client as app_client
from tests.test_agent_files import prepare, send, wait_turn


def _changed_dxf(agent, session_id, system, preview):
    context = json.loads(system.split('：', 1)[1])
    agent.contexts.append(context)
    agent.previews.append(preview)
    agent.turn += 1
    source = Path(context['input_files'][0]['path'])
    output = Path(context['output_directory']) / 'revised.dxf'
    doc = ezdxf.readfile(source)
    line = doc.modelspace().query('LINE').first
    line.dxf.end = (200, 0, 0)
    doc.saveas(output)
    return context, output, line.dxf.handle


def _mock_snapshots(monkeypatch, *, during_render=None):
    rendered = []

    def snapshot(context, path, settings=None):
        rendered.append({'path': str(path), 'sha256': digest(path)})
        target = Path(context['scratch_directory']) / 'review.png'
        Image.new('RGB', (16, 16), 'white').save(target)
        if during_render:
            during_render(context)
        return {'path': str(target), 'drawing_sha256': digest(path), 'warnings': []}

    monkeypatch.setattr(delivery, 'render_snapshot', snapshot)
    return rendered


def _track_saved_reviews(monkeypatch, *, during_review=None):
    original = delivery.review_delivery
    reviews = []

    def review(context):
        result = original(context)
        reviews.append(result)
        if during_review:
            during_review(context)
        return result

    monkeypatch.setattr(delivery, 'review_delivery', review)
    return reviews


def test_unregistered_changed_dxf_is_repaired_in_same_task_and_published_once(app_client, monkeypatch):
    client, agent = app_client
    drawing, session, original = prepare(client)
    tasks = client.app.state.file_tasks
    rendered = _mock_snapshots(monkeypatch)
    reviews = _track_saved_reviews(monkeypatch)
    prompts, continuations, collected = [], [], []
    original_collect = tasks.collect
    changed_handle = None

    def collect(*args, **kwargs):
        collected.append(args[0]['id'])
        return original_collect(*args, **kwargs)

    async def prompt(session_id, text, system=None, *, internal=False, preview=None):
        nonlocal changed_handle
        prompts.append((session_id, internal))
        if not internal:
            _, _, changed_handle = _changed_dxf(agent, session_id, system, preview)
            return False
        context = json.loads(system.split('：', 1)[1])
        assert context == agent.contexts[0]
        assert json.loads(tasks.binding_path(session_id).read_text()) == context
        assert 'review_required' in text
        assert preview is not None and Path(preview).is_file()
        assert client.app.state.store.all('SELECT * FROM artifacts WHERE task_id=?', (context['task_id'],)) == []
        continuations.append(context)
        delivery.register(context, Path(context['output_directory']) / 'revised.dxf',
                          profile='general', allowed_changes=[changed_handle])
        agent.turn += 1
        return False

    monkeypatch.setattr(tasks, 'collect', collect)
    monkeypatch.setattr(agent, 'prompt_async', prompt)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'completed', result
    assert prompts == [(session, False), (session, True)]
    assert len(continuations) == 1 and agent.turn == 2
    assert [review['status'] for review in reviews] == ['unknown', 'passed']
    assert len(rendered) == 1
    assert rendered[0]['sha256'] == reviews[0]['files'][0]['file_sha256']
    assert collected == [result['task']['id']]
    assert [artifact['name'] for artifact in result['artifacts']] == ['revised.dxf']
    assert client.app.state.store.one('SELECT COUNT(*) AS count FROM agent_tasks')['count'] == 1
    assert client.app.state.store.one('SELECT COUNT(*) AS count FROM artifacts')['count'] == 1
    artifact = result['artifacts'][0]
    downloaded = client.get(artifact['download_url']).content
    assert downloaded != original
    assert client.get(f'/api/drawings/{drawing}/revisions/r0001/download').content == original
    attempts = json.loads((tasks.root(result['task']['id']) / 'scratch/review-attempts.json').read_text())
    assert len(attempts) == 1 and attempts[0]['issues'][0]['code'] == 'review_required'
    assert not tasks.binding_path(session).exists()


def test_invalid_saved_geometry_exhausts_review_budget_without_publishing(app_client, monkeypatch):
    client, agent = app_client
    _, session, _ = prepare(client)
    tasks = client.app.state.file_tasks
    tasks.max_review_retries = 2
    rendered = _mock_snapshots(monkeypatch)
    reviews = _track_saved_reviews(monkeypatch)
    internal_sessions = []

    async def prompt(session_id, text, system=None, *, internal=False, preview=None):
        if not internal:
            context, output, _ = _changed_dxf(agent, session_id, system, preview)
            delivery.register(context, output, profile='general')
        else:
            context = json.loads(system.split('：', 1)[1])
            assert context == agent.contexts[0]
            assert client.app.state.store.all('SELECT * FROM artifacts') == []
            internal_sessions.append(session_id)
            agent.turn += 1
        # A model-written "passed" assertion must not replace independent review.
        contract = delivery.load_contract(context)
        contract['entries']['revised.dxf']['status'] = 'passed'
        delivery.save_contract(context, contract)
        (Path(context['scratch_directory']) / 'delivery-review.json').write_text(
            json.dumps({'status': 'passed', 'files': []}))
        return False

    monkeypatch.setattr(agent, 'prompt_async', prompt)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'failed', result
    assert '成果检查未通过' in result['task']['error']
    assert result['artifacts'] == []
    assert client.app.state.store.all('SELECT * FROM artifacts') == []
    assert internal_sessions == [session] * tasks.max_review_retries
    assert agent.turn == 1 + tasks.max_review_retries
    assert [review['status'] for review in reviews] == ['failed'] * (1 + tasks.max_review_retries)
    assert all(review['files'][0]['checks']['preservation']['status'] == 'failed' for review in reviews)
    assert len(rendered) == tasks.max_review_retries
    root = tasks.root(result['task']['id'])
    attempts = json.loads((root / 'scratch/review-attempts.json').read_text())
    assert len(attempts) == tasks.max_review_retries
    saved = json.loads((root / 'scratch/delivery-review.json').read_text())
    assert saved['status'] == 'failed'
    assert not tasks.binding_path(session).exists()
    assert client.get(f'/api/sessions/{session}/status').json()['status'] == 'idle'


@pytest.mark.parametrize('stage', ['review', 'preview'])
def test_cancellation_during_review_does_not_continue_or_publish(app_client, monkeypatch, stage):
    client, agent = app_client
    _, session, _ = prepare(client)
    tasks = client.app.state.file_tasks
    cancelled = []
    prompts = []

    def cancel(context):
        cancelled.append(context['task_id'])
        tasks.set_status(context['task_id'], 'stopping')

    reviews = _track_saved_reviews(monkeypatch, during_review=cancel if stage == 'review' else None)
    rendered = _mock_snapshots(monkeypatch, during_render=cancel if stage == 'preview' else None)

    async def prompt(session_id, text, system=None, *, internal=False, preview=None):
        prompts.append((session_id, internal))
        if not internal:
            _changed_dxf(agent, session_id, system, preview)
        else:
            agent.turn += 1
        return False

    monkeypatch.setattr(agent, 'prompt_async', prompt)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'cancelled', result
    assert cancelled == [result['task']['id']]
    assert prompts == [(session, False)] and agent.turn == 1
    assert agent.aborted
    assert len(reviews) == 1 and reviews[0]['status'] == 'unknown'
    assert len(rendered) == (1 if stage == 'preview' else 0)
    assert result['artifacts'] == []
    assert client.app.state.store.all('SELECT * FROM artifacts') == []
    assert not (tasks.root(result['task']['id']) / 'scratch/review-attempts.json').exists()
    assert not tasks.binding_path(session).exists()
