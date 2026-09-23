"""Project navigation preserves file identity, provenance and conversation targets."""
from tests.test_agent_files import app_client as app_client, prepare, send, wait_turn
from tests.test_api import sample_bytes, wait_job


def test_project_versions_include_generated_files_without_retargeting_reads(app_client):
    client, agent = app_client
    source, session, original = prepare(client)
    first = client.get('/api/projects/default/workbench').json()
    assert len(first['versions']) == 1
    assert first['versions'][0]['label'] == 'v0001_input'
    assert first['sessions'][0]['drawing_id'] == source
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    assert result['task']['status'] == 'completed'
    file = next(f for f in result['artifacts'] if f['drawing_id'])
    row = client.app.state.store.one('SELECT * FROM artifacts WHERE id=?', (file['id'],))
    assert wait_job(client, row['job_id'])['status'] == 'succeeded'
    second = client.get('/api/projects/default/workbench').json()
    assert [(v['number'], v['name']) for v in second['versions']] == [(1, 'input.dxf'), (2, 'generated.dxf')]
    assert second['versions'][0] == first['versions'][0]
    assert second['versions'][1]['artifact_id'] == file['id']
    assert second['sessions'][0]['id'] == session
    assert second['sessions'][0]['drawing_id'] == source  # Browsing has no side effects.
    assert len(second['artifacts']) == 3  # Non-CAD reports remain available.
    assert all('path' not in f and 'sha256' not in f for f in second['artifacts'])
    opened = client.post(f"/api/artifacts/{file['id']}/open").json()
    third = client.get('/api/projects/default/workbench').json()
    assert third['sessions'][0]['drawing_id'] == opened['drawing_id']
    assert third['sessions'][0]['id'] == session
    assert third['versions'] == second['versions']
    assert client.get(f'/api/drawings/{source}/revisions/r0001/download').content == original
    assert len(agent.contexts) == 1


def test_projects_are_isolated_and_names_can_change_without_changing_files(app_client):
    client, agent = app_client
    source, _, _ = prepare(client)
    created = client.post('/api/projects', json={'name': '  另一个工程  '})
    assert created.status_code == 201
    project = created.json()
    assert project['name'] == '另一个工程'
    assert client.get(f"/api/projects/{project['id']}/workbench").json()['versions'] == []
    data, _, _ = sample_bytes()
    upload = client.post(f"/api/projects/{project['id']}/drawings", files={'file': ('same-name.dxf', data)}).json()
    assert wait_job(client, upload['job_id'])['status'] == 'succeeded'
    view = client.get(f"/api/projects/{project['id']}/workbench").json()
    assert [v['drawing_id'] for v in view['versions']] == [upload['drawing_id']]
    assert view['versions'][0]['label'] == 'v0001_same-name'
    assert view['sessions'] == view['artifacts'] == []
    renamed = client.patch(f"/api/projects/{project['id']}", json={'name': '二期工程'})
    assert renamed.status_code == 200
    refreshed = client.get(f"/api/projects/{project['id']}/workbench").json()
    assert refreshed['project']['name'] == '二期工程'
    assert refreshed['versions'] == view['versions']
    assert client.get('/api/projects/default/workbench').json()['versions'][0]['drawing_id'] == source
    assert client.post('/api/projects', json={'name': '   '}).status_code == 422
    assert client.patch('/api/projects/default', json={'name': '   '}).status_code == 422
    assert client.get('/api/projects/missing/workbench').status_code == 404
    assert client.patch('/api/projects/missing', json={'name': '工程'}).status_code == 404
    assert not agent.contexts


def test_unfinished_outputs_never_appear_as_project_files(app_client):
    client, _ = app_client
    _, session, _ = prepare(client)
    assert send(client, session).status_code == 202
    result = wait_turn(client, session)
    file = next(f for f in result['artifacts'] if f['drawing_id'])
    row = client.app.state.store.one('SELECT * FROM artifacts WHERE id=?', (file['id'],))
    assert wait_job(client, row['job_id'])['status'] == 'succeeded'
    client.app.state.store.execute("UPDATE agent_tasks SET status='failed' WHERE id=?", (result['task']['id'],))
    view = client.get('/api/projects/default/workbench').json()
    assert len(view['versions']) == 1
    assert not view['artifacts']
