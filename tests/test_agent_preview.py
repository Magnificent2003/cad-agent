"""Attach the current revision's existing PNG without rendering or changing CAD."""
import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import shutil

import httpx
from PIL import Image
import pytest

from agent.client import AgentClient
from agent import preview as preview_module
from backend.app.jobs import drawing_root
from backend.app.store import encode, now
from tests.test_agent_files import app_client as app_client, prepare, reply, send, wait_turn


def png_bytes(color='white'):
    output = BytesIO()
    Image.new('RGB', (32, 24), color).save(output, format='PNG')
    return output.getvalue()


async def test_transport_adds_file_part_and_keeps_original_user_text(tmp_path):
    path = tmp_path / 'overview.png'
    data = png_bytes()
    path.write_bytes(data)
    payloads = []

    def upstream(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(204)

    async with AgentClient(transport=httpx.MockTransport(upstream)) as agent:
        await agent.prompt_async('session1', '简单介绍一下这张施工图', system='trusted context', preview=path)
        await agent.prompt_async('session1', '继续完成', system='trusted context', internal=True, preview=path)
    assert payloads[0]['parts'][0] == {'type': 'text', 'text': '简单介绍一下这张施工图'}
    assert payloads[1]['parts'][0]['synthetic'] is True
    for payload in payloads:
        assert payload['system'] == 'trusted context'
        assert len(payload['parts']) == 2
        attachment = payload['parts'][1]
        assert attachment['type'] == 'file' and attachment['mime'] == 'image/png'
        assert attachment['filename'] == 'overview.png'
        assert attachment['url'].startswith('data:image/png;base64,')
        assert base64.b64decode(attachment['url'].split(',', 1)[1]) == data
    assert payloads[0]['parts'][1] == payloads[1]['parts'][1]


@pytest.mark.parametrize('invalid', ['missing', 'corrupt', 'jpeg', 'too-many-bytes', 'too-many-pixels', 'too-wide'])
async def test_invalid_preview_continues_text_request_without_image(tmp_path, monkeypatch, invalid):
    path = tmp_path / 'overview.png'
    if invalid == 'corrupt':
        path.write_bytes(png_bytes()[:40])
    elif invalid == 'jpeg':
        Image.new('RGB', (32, 24)).save(path, format='JPEG')
    elif invalid == 'too-many-bytes':
        path.write_bytes(b'x' * (preview_module.MAX_PREVIEW_BYTES + 1))
    elif invalid == 'too-many-pixels':
        monkeypatch.setattr(preview_module, 'MAX_PREVIEW_PIXELS', 500)
        path.write_bytes(png_bytes())
    elif invalid == 'too-wide':
        Image.new('RGB', (preview_module.MAX_PREVIEW_EDGE + 1, 1)).save(path, format='PNG')
    payloads = []

    def upstream(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(204)

    async with AgentClient(transport=httpx.MockTransport(upstream)) as agent:
        await agent.prompt_async('session1', '看图', system='current task', preview=path)
    assert payloads[0]['parts'] == [{'type': 'text', 'text': '看图'}]
    assert payloads[0]['system'].startswith('current task')
    assert '未附加预览图片' in payloads[0]['system']


def test_task_copies_existing_preview_and_preserves_cad_input_order(app_client, monkeypatch):
    client, agent = app_client
    drawing, session, _ = prepare(client)
    state = client.app.state
    root = drawing_root(state.settings, drawing)
    original_preview = (root / 'revisions/r0001/preview.png').read_bytes()
    # Keep this a synthetic fixture: no DWG conversion or real drawing is needed.
    (root / 'source/original.dwg').write_bytes(b'test-dwg-input')
    state.store.execute('UPDATE drawings SET source_suffix=? WHERE id=?', ('.dwg', drawing))
    submitted = []
    submit = state.jobs.submit

    def track_job(drawing_id, kind, payload, **kwargs):
        submitted.append(kind)
        return submit(drawing_id, kind, payload, **kwargs)

    monkeypatch.setattr(state.jobs, 'submit', track_job)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'completed', result
    context = agent.contexts[0]
    assert [item['format'] for item in context['input_files']] == ['dxf', 'dwg']
    preview = context['preview']
    assert preview['available'] is True
    assert preview['revision_id'] == 'r0001' and preview['drawing_id'] == drawing
    snapshot = Path(preview['path'])
    assert snapshot == agent.previews[0]
    assert snapshot == state.file_tasks.root(result['task']['id']) / 'input/overview.png'
    assert snapshot.read_bytes() == original_preview
    assert snapshot.stat().st_mode & 0o222 == 0
    assert preview['sha256'] == hashlib.sha256(original_preview).hexdigest()
    assert (root / 'revisions/r0001/preview.png').read_bytes() == original_preview
    stored = state.store.one('SELECT inputs FROM agent_tasks WHERE id=?', (result['task']['id'],))
    assert [item['format'] for item in stored['inputs']] == ['dxf', 'dwg', 'png']
    assert 'render' not in submitted


@pytest.mark.parametrize('has_current_preview', [False, True])
def test_preview_uses_exact_source_revision_without_older_fallback(app_client, has_current_preview):
    client, agent = app_client
    drawing, session, _ = prepare(client)
    state = client.app.state
    root = drawing_root(state.settings, drawing)
    revision = root / 'revisions/r0002'
    revision.mkdir()
    shutil.copy2(root / 'revisions/r0001/drawing.dxf', revision / 'drawing.dxf')
    current_png = png_bytes('blue')
    if has_current_preview:
        (revision / 'preview.png').write_bytes(current_png)
    original = state.store.one('SELECT metadata FROM revisions WHERE drawing_id=? AND id=?', (drawing, 'r0001'))
    state.store.execute('INSERT INTO revisions VALUES (?,?,?,?,?,?)',
                        ('r0002', drawing, 2, now(), 'test revision', encode(original['metadata'])))
    state.store.execute('UPDATE drawings SET current_revision=? WHERE id=?', ('r0002', drawing))
    response = client.post(f'/api/sessions/{session}/messages', json={'text': '描述当前版本', 'expected_revision': 'r0002'})
    assert response.status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'completed', result
    preview = agent.contexts[0]['preview']
    assert preview['revision_id'] == 'r0002' and preview['available'] is has_current_preview
    if has_current_preview:
        assert agent.previews[0].read_bytes() == current_png
    else:
        assert agent.previews == [None]
        assert '没有' in preview['reason']
        assert not (state.file_tasks.root(result['task']['id']) / 'input/overview.png').exists()


def test_corrupt_existing_preview_does_not_fail_file_task(app_client):
    client, agent = app_client
    drawing, session, _ = prepare(client)
    root = drawing_root(client.app.state.settings, drawing)
    (root / 'revisions/r0001/preview.png').write_bytes(b'invalid preview')
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'completed', result
    assert agent.previews == [None]
    assert agent.contexts[0]['preview']['available'] is False
    assert '无效' in agent.contexts[0]['preview']['reason']


def test_preview_snapshot_is_hash_checked_before_publication(app_client, monkeypatch):
    client, agent = app_client
    drawing, session, _ = prepare(client)
    source = drawing_root(client.app.state.settings, drawing) / 'revisions/r0001/preview.png'
    original = source.read_bytes()
    initial = agent.prompt_async

    async def prompt(session_id, text, system=None, *, preview=None):
        await initial(session_id, text, system, preview=preview)
        preview.chmod(0o600)
        preview.write_bytes(png_bytes('red'))

    monkeypatch.setattr(agent, 'prompt_async', prompt)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'failed' and result['artifacts'] == []
    assert source.read_bytes() == original


def test_recovery_reuses_same_preview_snapshot_and_task_context(app_client, monkeypatch):
    client, agent = app_client
    _, session, _ = prepare(client)
    initial = agent.prompt_async
    attachments = []

    async def prompt(session_id, text, system=None, *, internal=False, preview=None):
        attachments.append((preview, preview.read_bytes()))
        if not internal:
            return await initial(session_id, text, system, preview=preview)
        context = json.loads(system.split('：', 1)[1])
        assert context == agent.contexts[0]
        assert preview == Path(context['preview']['path'])
        agent.turn += 1

    async def messages(session_id):
        result = reply(f'turn{agent.turn}', 'length' if agent.turn == 1 else 'stop')
        if agent.turn == 1:
            result['parts'] = []
        return [reply('old'), result]

    monkeypatch.setattr(agent, 'prompt_async', prompt)
    monkeypatch.setattr(agent, 'messages', messages)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'completed', result
    assert len(attachments) == 2 and attachments[0] == attachments[1]
