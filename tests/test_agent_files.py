"""Exercise file handoff, publication, continuation, cancellation and no fallback."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from agent.session import terminal_assistant
from backend.app import freecad
from backend.app.main import create_app
from backend.app.config import Settings
from backend.app.store import now
from tests.test_api import sample_bytes, wait_job


def reply(mid, finish='stop'):
    return {'info': {'id': mid, 'role': 'assistant', 'finish': finish, 'time': {'completed': 1}},
            'parts': [{'type': 'text', 'text': 'generated'}]}


class FileAgent:
    def __init__(self):
        self.contexts = []
        self.finished = True
        self.turn = 0
        self.tool_mode = 'freecad'
        self.sessions = 0
        self.aborted = False
        self.release_abort = True

    async def health(self):
        return {'healthy': True}

    async def mcp_status(self):
        return {self.tool_mode: {'status': 'connected'}}

    async def events(self, ready=None):
        if ready:
            ready.set()
        while True:
            await asyncio.sleep(60)
            yield {}

    async def close(self):
        pass

    async def create_session(self, title):
        self.sessions += 1
        return {'id': f'session{self.sessions}'}

    async def session_status(self):
        return {}

    async def messages(self, session):
        return [reply('old'), reply(f'turn{self.turn}', 'stop' if self.finished else 'tool-calls')]

    async def prompt_async(self, session, text, system=None):
        context = json.loads(system.split('：', 1)[1])
        self.contexts.append(context)
        self.turn += 1
        output = Path(context['output_directory'])
        # Simulates arbitrary model-generated output, not a production edit API.
        original = Path(context['input_files'][0]['path'])
        (output / 'generated.dxf').write_bytes(original.read_bytes())
        (output / 'report.csv').write_text('kind,count\nLINE,1\n')
        (output / 'nested').mkdir()
        (output / 'nested' / 'custom.result').write_bytes(b'non-CAD deliverable')
        (output / 'not-a-deliverable.dxf').symlink_to(original)
        (Path(context['scratch_directory']) / 'unfinished.dxf').write_text('partial')

    async def abort(self, session):
        self.aborted = True
        return self.release_abort


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    agent = FileAgent()
    monkeypatch.setattr(freecad, 'idle', AsyncMock(return_value=True))
    settings = Settings(_env_file=None, cad_data_dir=tmp_path, oda_executable='', cad_preview_dpi=72)
    with TestClient(create_app(settings, agent)) as client:
        client.app.state.file_tasks.poll_interval = .01
        yield client, agent


def prepare(client):
    content, _, _ = sample_bytes()
    uploaded = client.post('/api/drawings', files={'file': ('input.dxf', content)}).json()
    assert wait_job(client, uploaded['job_id'])['status'] == 'succeeded'
    drawing = uploaded['drawing_id']
    response = client.post(f'/api/drawings/{drawing}/sessions')
    assert response.status_code == 201, response.text
    return drawing, response.json()['id'], content


def send(client, session):
    return client.post(f'/api/sessions/{session}/messages', json={'text': '处理文件并生成成果', 'expected_revision': 'r0001'})


def wait_turn(client, session):
    import time
    for _ in range(500):
        result = client.get(f'/api/sessions/{session}/artifacts').json()
        if result['task'] and result['task']['status'] not in {'preparing', 'running', 'collecting', 'stopping'}:
            return result
        time.sleep(.01)
    pytest.fail('file turn did not finish')


def test_web_file_workflow_keeps_original_and_continues_same_session_on_output(app_client):
    client, agent = app_client
    drawing, session, original = prepare(client)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'completed', result
    files = {f['name']: f for f in result['artifacts']}
    assert set(files) == {'generated.dxf', 'report.csv', 'nested/custom.result'}
    assert client.get(files['report.csv']['download_url']).content.startswith(b'kind,count')
    assert client.get(f'/api/drawings/{drawing}/revisions/r0001/download').content == original
    artifact = client.app.state.store.one('SELECT * FROM artifacts WHERE id=?', (files['generated.dxf']['id'],))
    assert wait_job(client, artifact['job_id'])['status'] == 'succeeded'
    opened = client.post(f"/api/artifacts/{artifact['id']}/open")
    assert opened.status_code == 200, opened.text
    assert opened.json()['session_id'] == session
    assert opened.json()['drawing_id'] != drawing
    assert send(client, session).status_code == 202
    second = wait_turn(client, session)
    assert second['task']['status'] == 'completed'
    assert agent.contexts[1]['drawing_id'] == opened.json()['drawing_id']
    assert agent.contexts[1]['output_directory'] != agent.contexts[0]['output_directory']
    assert len(agent.contexts[1]['previous_artifacts']) == 3
    assert not (Path(agent.contexts[0]['output_directory']) / 'unfinished.dxf').exists()


def test_wrong_engine_and_legacy_session_fail_without_fallback(app_client):
    client, agent = app_client
    drawing, session, _ = prepare(client)
    agent.tool_mode = 'cad'
    rejected = send(client, session)
    assert rejected.status_code == 503 and not agent.contexts
    for endpoint in ('query', 'edit', 'export', 'render'):
        assert client.post(f'/internal/drawings/{drawing}/{endpoint}', json={}).status_code in {404, 405}
    client.app.state.store.execute('UPDATE sessions SET engine=? WHERE id=?', ('legacy', session))
    agent.tool_mode = 'freecad'
    assert send(client, session).status_code == 409
    assert not agent.contexts


def test_busy_freecad_serializes_sessions_and_cancel_does_not_publish_late_files(app_client, monkeypatch):
    import time
    client, agent = app_client
    drawing, session, _ = prepare(client)
    other = client.post(f'/api/drawings/{drawing}/sessions').json()['id']
    agent.finished = False
    assert send(client, session).status_code == 202
    assert send(client, other).status_code == 409
    monkeypatch.setattr(freecad, 'idle', AsyncMock(return_value=False))
    stopped = client.post(f'/api/sessions/{session}/abort')
    assert stopped.status_code == 200
    time.sleep(.05)
    assert client.get(f'/api/sessions/{session}/status').json()['status'] == 'busy'
    assert client.get(f'/api/sessions/{session}/artifacts').json()['artifacts'] == []
    monkeypatch.setattr(freecad, 'idle', AsyncMock(return_value=True))
    result = wait_turn(client, session)
    assert result['task']['status'] == 'cancelled' and result['artifacts'] == []
    assert agent.aborted


def test_input_damage_or_output_directory_replacement_is_rejected(app_client, monkeypatch, tmp_path):
    client, agent = app_client
    _, session, _ = prepare(client)
    original_prompt = agent.prompt_async

    async def damaged_input(*args, **kwargs):
        await original_prompt(*args, **kwargs)
        source = Path(agent.contexts[-1]['input_files'][0]['path'])
        source.chmod(0o600)
        source.write_text('damaged')

    monkeypatch.setattr(agent, 'prompt_async', damaged_input)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'failed'
    assert result['artifacts'] == []


def test_restart_archives_legacy_sessions_and_interrupts_file_tasks(app_client):
    client, _ = app_client
    drawing, session, _ = prepare(client)
    store = client.app.state.store
    store.execute('INSERT INTO agent_tasks (id,session_id,drawing_id,source_revision,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                  ('unfinished', session, drawing, 'r0001', 'running', now(), now()))
    store.initialize()
    assert store.one('SELECT status FROM agent_tasks WHERE id=?', ('unfinished',))['status'] == 'interrupted'
    assert not client.app.state.file_tasks.active()


def test_terminal_detection_requires_a_new_completed_assistant_reply():
    previous = {'old'}
    assert terminal_assistant([reply('old')], previous) is None
    assert terminal_assistant([reply('old'), reply('new', 'tool-calls')], previous) is None
    assert terminal_assistant([reply('old'), reply('new')], previous)['info']['id'] == 'new'


def test_output_directory_symlink_is_not_collected(app_client, monkeypatch, tmp_path):
    import shutil
    client, agent = app_client
    _, session, _ = prepare(client)
    external = tmp_path / 'unrelated'
    external.mkdir()
    (external / 'private.txt').write_text('must not publish')
    normal_prompt = agent.prompt_async

    async def swap_directory(*args, **kwargs):
        await normal_prompt(*args, **kwargs)
        output = Path(agent.contexts[-1]['output_directory'])
        shutil.rmtree(output)
        output.symlink_to(external, target_is_directory=True)

    monkeypatch.setattr(agent, 'prompt_async', swap_directory)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'failed'
    assert result['artifacts'] == []


def test_analysis_only_turn_needs_no_artifact(app_client, monkeypatch):
    client, agent = app_client
    _, session, _ = prepare(client)

    async def analysis(session, text, system=None):
        agent.turn += 1

    monkeypatch.setattr(agent, 'prompt_async', analysis)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'completed'
    assert result['artifacts'] == []
