"""A quiet engine is not a completed user task after internal compaction."""
import json
import time

import pytest

from backend.app import freecad
from tests.test_agent_files import app_client as app_client, prepare, reply, send, wait_turn


def summary(mid, *, error=False):
    result = reply(mid)
    result['info'].update(summary=True, agent='compaction')
    result['parts'][0]['text'] = 'Internal objective and work state'
    if error:
        result['info']['error'] = {'name': 'ContextOverflowError', 'data': {'message': 'internal error'}}
    return result


@pytest.mark.parametrize('stalled', ['summary', 'empty-history', 'unfinished-message'])
def test_idle_without_user_reply_resumes_same_task_after_grace(app_client, monkeypatch, stalled):
    client, agent = app_client
    _, session, _ = prepare(client)
    tasks = client.app.state.file_tasks
    tasks.idle_reply_grace = .03
    initial = agent.prompt_async
    resumed = []
    begun = []

    async def prompt(session_id, text, system=None, *, internal=False, preview=None):
        if not internal:
            begun.append(time.monotonic())
            return await initial(session_id, text, system, preview=preview)
        context = json.loads(system.split('：', 1)[1])
        assert context == agent.contexts[0]
        assert json.loads(tasks.binding_path(session_id).read_text()) == context
        resumed.append((time.monotonic(), context))
        agent.turn += 1

    async def messages(session_id):
        # Existing summary failures must never fail a new task.
        history = [reply('old'), summary('old-error', error=True)]
        if agent.turn == 1:
            if stalled == 'summary':
                history.append(summary('compaction'))
            if stalled == 'unfinished-message':
                pending = reply('pending', None)
                pending['info']['time'] = {'created': 1}
                pending['parts'] = []
                history.append(pending)
        if agent.turn >= 2:
            history.append(reply('final'))
        return history

    monkeypatch.setattr(agent, 'prompt_async', prompt)
    monkeypatch.setattr(agent, 'messages', messages)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'completed', result
    assert len(resumed) == 1 and resumed[0][0] - begun[0] >= tasks.idle_reply_grace
    assert len(result['artifacts']) == 3
    assert client.app.state.store.one('SELECT COUNT(*) AS count FROM agent_tasks')['count'] == 1
    audit = json.loads((tasks.root(result['task']['id']) / 'recovery.json').read_text())
    assert audit[0]['finish'] == 'idle-without-answer'
    assert not tasks.binding_path(session).exists()


def test_repeated_internal_only_stops_fail_with_bounded_recovery(app_client, monkeypatch):
    client, agent = app_client
    _, session, _ = prepare(client)
    tasks = client.app.state.file_tasks
    tasks.idle_reply_grace = .03
    initial = agent.prompt_async

    async def prompt(session_id, text, system=None, *, internal=False, preview=None):
        if internal:
            agent.turn += 1
        else:
            await initial(session_id, text, system, preview=preview)

    async def messages(session_id):
        return [reply('old'), summary(f'compact-{agent.turn}')]

    monkeypatch.setattr(agent, 'prompt_async', prompt)
    monkeypatch.setattr(agent, 'messages', messages)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'failed'
    assert '正式答复' in result['task']['error']
    assert agent.turn == 1 + tasks.max_recoveries
    assert result['artifacts'] == []
    assert not tasks.binding_path(session).exists()
    audit = json.loads((tasks.root(result['task']['id']) / 'recovery.json').read_text())
    assert len(audit) == tasks.max_recoveries


def test_new_internal_error_fails_without_publishing_or_waiting_for_grace(app_client, monkeypatch):
    client, agent = app_client
    _, session, _ = prepare(client)
    tasks = client.app.state.file_tasks
    tasks.idle_reply_grace = 60

    async def messages(session_id):
        return [reply('old'), summary(f'compact-{agent.turn}', error=agent.turn == 1)]

    monkeypatch.setattr(agent, 'messages', messages)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'failed'
    assert '内部整理' in result['task']['error']
    assert result['artifacts'] == [] and agent.turn == 1
    assert not (tasks.root(result['task']['id']) / 'recovery.json').exists()


@pytest.mark.parametrize('busy_component', ['agent', 'freecad'])
def test_transient_idle_then_busy_does_not_start_extra_turn(app_client, monkeypatch, busy_component):
    client, agent = app_client
    _, session, _ = prepare(client)
    tasks = client.app.state.file_tasks
    tasks.idle_reply_grace = .03
    checks = 0
    initial = agent.prompt_async

    async def prompt(session_id, text, system=None, *, internal=False, preview=None):
        assert not internal, 'A busy engine or FreeCAD must never be resumed concurrently'
        await initial(session_id, text, system, preview=preview)

    async def readiness():
        nonlocal checks
        if not agent.turn:
            return True
        checks += 1
        # First an idle blip, then enough busy polls to exceed the grace period.
        return checks == 1 or checks >= 10

    async def agent_status():
        return {session: {'type': 'idle' if await readiness() else 'busy'}}

    async def messages(session_id):
        return [reply('old'), reply('final') if checks >= 10 else summary(f'compact-{agent.turn}')]

    monkeypatch.setattr(agent, 'prompt_async', prompt)
    monkeypatch.setattr(agent, 'messages', messages)
    if busy_component == 'agent':
        monkeypatch.setattr(agent, 'session_status', agent_status)
    else:
        monkeypatch.setattr(freecad, 'idle', readiness)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'completed', result
    assert checks >= 11 and agent.turn == 1
    assert not (tasks.root(result['task']['id']) / 'recovery.json').exists()


def test_cancel_wins_during_idle_without_reply_readiness(app_client, monkeypatch):
    client, agent = app_client
    _, session, _ = prepare(client)
    tasks = client.app.state.file_tasks
    tasks.idle_reply_grace = 0

    async def messages(session_id):
        return [reply('old'), summary(f'compact-{agent.turn}')]

    async def status():
        if agent.turn:
            tasks.set_status(tasks.active(session)['id'], 'stopping')
        return {}

    monkeypatch.setattr(agent, 'messages', messages)
    monkeypatch.setattr(agent, 'session_status', status)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'cancelled'
    assert result['artifacts'] == [] and agent.turn == 1
    assert agent.aborted
