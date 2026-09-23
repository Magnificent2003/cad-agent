"""Regression tests for empty/truncated answers without restarting file work."""
import json
from pathlib import Path

import pytest

from tests.test_agent_files import app_client as app_client
from tests.test_agent_files import prepare, reply, send, wait_turn


def incomplete(mid, finish='length'):
    message = reply(mid, finish)
    message['parts'] = [{'type': 'reasoning', 'text': 'internal deliberation'}]
    return message


@pytest.mark.parametrize('finish', ['length', 'stop'])
def test_incomplete_reply_resumes_same_task_and_publishes_once(app_client, monkeypatch, finish):
    client, agent = app_client
    _, session, _ = prepare(client)
    initial = agent.prompt_async
    continuations = []

    async def prompt(session_id, text, system=None, *, internal=False, preview=None):
        if not internal:
            return await initial(session_id, text, system, preview=preview)
        context = json.loads(system.split('：', 1)[1])
        assert context == agent.contexts[0]
        assert context['request'] == '处理文件并生成成果'
        binding = client.app.state.file_tasks.binding_path(session_id)
        assert json.loads(binding.read_text()) == context
        continuations.append(context)
        agent.turn += 1

    async def messages(session_id):
        return [reply('old'), incomplete('turn1', finish) if agent.turn == 1 else reply(f'turn{agent.turn}')]

    monkeypatch.setattr(agent, 'prompt_async', prompt)
    monkeypatch.setattr(agent, 'messages', messages)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'completed', result
    assert len(continuations) == 1
    assert len(result['artifacts']) == 3
    assert {item['task_id'] for item in client.app.state.store.all('SELECT task_id FROM artifacts')} == {result['task']['id']}
    assert not client.app.state.file_tasks.binding_path(session).exists()
    audit = json.loads((client.app.state.file_tasks.root(result['task']['id']) / 'recovery.json').read_text())
    assert len(audit) == 1 and audit[0]['finish'] == finish


def test_repeated_truncation_fails_instead_of_publishing_files(app_client, monkeypatch):
    client, agent = app_client
    _, session, _ = prepare(client)
    initial = agent.prompt_async

    async def prompt(session_id, text, system=None, *, internal=False, preview=None):
        if internal:
            agent.turn += 1
        else:
            await initial(session_id, text, system, preview=preview)

    async def messages(session_id):
        return [reply('old'), incomplete(f'turn{agent.turn}')]

    monkeypatch.setattr(agent, 'prompt_async', prompt)
    monkeypatch.setattr(agent, 'messages', messages)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'failed'
    assert '截断' in result['task']['error']
    assert result['artifacts'] == []
    assert agent.turn == 1 + client.app.state.file_tasks.max_recoveries
    assert not client.app.state.file_tasks.binding_path(session).exists()


def test_cancel_during_recovery_does_not_publish_or_resume_again(app_client, monkeypatch):
    client, agent = app_client
    _, session, _ = prepare(client)
    initial = agent.prompt_async

    async def prompt(session_id, text, system=None, *, internal=False, preview=None):
        if internal:
            agent.turn += 1
            tasks = client.app.state.file_tasks
            tasks.set_status(tasks.active(session_id)['id'], 'stopping')
        else:
            await initial(session_id, text, system, preview=preview)

    async def messages(session_id):
        return [reply('old'), incomplete(f'turn{agent.turn}')]

    monkeypatch.setattr(agent, 'prompt_async', prompt)
    monkeypatch.setattr(agent, 'messages', messages)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'cancelled'
    assert agent.aborted and agent.turn == 2
    assert result['artifacts'] == []


def test_new_turn_replaces_current_binding_and_keeps_original_request(app_client, monkeypatch):
    client, agent = app_client
    _, session, _ = prepare(client)
    initial = agent.prompt_async
    bindings = []

    async def prompt(session_id, text, system=None, *, preview=None):
        bindings.append(json.loads(client.app.state.file_tasks.binding_path(session_id).read_text()))
        await initial(session_id, text, system, preview=preview)

    monkeypatch.setattr(agent, 'prompt_async', prompt)
    assert send(client, session).status_code == 202
    assert wait_turn(client, session)['task']['status'] == 'completed'
    response = client.post(f'/api/sessions/{session}/messages', json={'text': '再说明一下', 'expected_revision': 'r0001'})
    assert response.status_code == 202
    assert wait_turn(client, session)['task']['status'] == 'completed'
    assert bindings[0]['task_id'] != bindings[1]['task_id']
    assert bindings[1]['request'] == '再说明一下'
    assert Path(bindings[0]['input_files'][0]['path']).read_bytes() == Path(bindings[1]['input_files'][0]['path']).read_bytes()


async def test_internal_resume_is_stored_as_synthetic_text():
    import httpx
    from agent.client import AgentClient

    def upstream(request):
        body = json.loads(request.content)
        assert body['system'] == 'current task'
        assert body['parts'][0]['synthetic'] is True
        assert body['parts'][0]['metadata']['cad_task_recovery'] is True
        return httpx.Response(204)

    async with AgentClient(transport=httpx.MockTransport(upstream)) as agent:
        await agent.prompt_async('session1', 'continue', system='current task', internal=True)


def test_cancel_during_readiness_does_not_overwrite_stopping(app_client, monkeypatch):
    from backend.app import freecad

    client, agent = app_client
    _, session, _ = prepare(client)
    tasks = client.app.state.file_tasks
    cancelled = False

    async def idle():
        nonlocal cancelled
        if agent.turn and not cancelled:
            cancelled = True
            tasks.set_status(tasks.active(session)['id'], 'stopping')
        return True

    monkeypatch.setattr(freecad, 'idle', idle)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert cancelled and agent.aborted
    assert result['task']['status'] == 'cancelled'
    assert result['artifacts'] == []
    assert not tasks.binding_path(session).exists()


@pytest.mark.parametrize('finish', ['content-filter', 'error', 'other'])
@pytest.mark.parametrize('text_present', [True, False])
def test_abnormal_finish_never_publishes_partial_result(app_client, monkeypatch, finish, text_present):
    client, agent = app_client
    _, session, _ = prepare(client)

    async def messages(session_id):
        message = reply(f'turn{agent.turn}', finish)
        if not text_present:
            message['parts'] = []
        return [reply('old'), message]

    monkeypatch.setattr(agent, 'messages', messages)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'failed'
    assert '未正常结束' in result['task']['error']
    assert result['artifacts'] == [] and agent.turn == 1
    assert not client.app.state.file_tasks.binding_path(session).exists()


@pytest.mark.parametrize('model_error', [False, True])
def test_damaged_binding_does_not_break_terminal_state(app_client, monkeypatch, model_error):
    client, agent = app_client
    _, session, _ = prepare(client)
    tasks = client.app.state.file_tasks
    initial = agent.prompt_async

    async def prompt(session_id, text, system=None, *, preview=None):
        await initial(session_id, text, system, preview=preview)
        tasks.binding_path(session_id).write_text('{invalid json')

    async def messages(session_id):
        message = reply(f'turn{agent.turn}')
        if model_error:
            message['info']['error'] = {'name': 'APIError', 'data': {'message': 'upstream failed'}}
        return [reply('old'), message]

    monkeypatch.setattr(agent, 'prompt_async', prompt)
    monkeypatch.setattr(agent, 'messages', messages)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == ('failed' if model_error else 'completed')
    if model_error:
        assert '模型任务失败' in result['task']['error']
        assert result['artifacts'] == []
    assert client.get(f'/api/sessions/{session}/status').json()['status'] == 'idle'
    assert tasks.binding_path(session).exists()  # Unknown content is retained for diagnosis.


def test_restart_cleans_only_bindings_of_known_inactive_tasks(app_client):
    from backend.app.agent_tasks import AgentTasks
    from backend.app.store import now

    client, _ = app_client
    drawing, session, _ = prepare(client)
    state = client.app.state
    tasks, store = state.file_tasks, state.store
    store.execute('INSERT INTO agent_tasks (id,session_id,drawing_id,source_revision,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                  ('old_running', session, drawing, 'r0001', 'running', now(), now()))
    binding = tasks.binding_path(session)
    binding.parent.mkdir(parents=True, exist_ok=True)
    binding.write_text(json.dumps({'task_id': 'old_running', 'session_id': session}))
    unknown = binding.parent / 'unknown.json'
    unknown.write_text(json.dumps({'task_id': 'not-in-database', 'session_id': 'unknown'}))
    damaged = binding.parent / 'damaged.json'
    damaged.write_text('not a task binding')
    store.initialize()
    active_session = client.post(f'/api/drawings/{drawing}/sessions').json()['id']
    store.execute('INSERT INTO agent_tasks (id,session_id,drawing_id,source_revision,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                  ('active_task', active_session, drawing, 'r0001', 'running', now(), now()))
    active_binding = tasks.binding_path(active_session)
    active_binding.write_text(json.dumps({'task_id': 'active_task', 'session_id': active_session}))
    AgentTasks(state.settings, store, state.bus, state.agent, state.jobs)
    assert not binding.exists()
    assert active_binding.exists()
    assert unknown.exists() and damaged.exists()
