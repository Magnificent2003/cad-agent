"""Download and adopt agent-generated files without interpreting CAD requests."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .chat import get_session

router = APIRouter(prefix='/api')


def artifact(request, artifact_id):
    row = request.app.state.store.one("SELECT a.* FROM artifacts a JOIN agent_tasks t ON t.id=a.task_id WHERE a.id=? AND t.status='completed'", (artifact_id,))
    if not row:
        raise HTTPException(404, '成果不存在或本轮尚未完成。')
    return row


@router.get('/sessions/{session_id}/artifacts')
def list_artifacts(session_id: str, request: Request):
    get_session(request, session_id)
    state = request.app.state
    rows = state.store.all("SELECT a.* FROM artifacts a JOIN agent_tasks t ON t.id=a.task_id WHERE a.session_id=? AND t.status='completed' ORDER BY a.created_at", (session_id,))
    result = []
    for row in rows:
        job = state.jobs.get(row['job_id']) if row['job_id'] else None
        result.append({'id': row['id'], 'name': row['name'], 'bytes': row['bytes'], 'task_id': row['task_id'],
                       'drawing_id': row['drawing_id'], 'preview_status': job['status'] if job else 'unavailable',
                       'preview_error': job.get('error') if job else None,
                       'download_url': f"/api/artifacts/{row['id']}/download"})
    latest = state.store.one('SELECT id,status,error FROM agent_tasks WHERE session_id=? ORDER BY created_at DESC LIMIT 1', (session_id,))
    return {'artifacts': result, 'task': latest}


@router.get('/artifacts/{artifact_id}/download')
def download(artifact_id: str, request: Request):
    row = artifact(request, artifact_id)
    path = Path(row['path'])
    root = (request.app.state.settings.data_dir / 'artifacts').resolve()
    if not path.resolve().is_relative_to(root) or path.is_symlink() or not path.is_file():
        raise HTTPException(404, '成果文件不可用。')
    return FileResponse(path, filename=Path(row['name']).name, media_type='application/octet-stream',
                        headers={'Content-Security-Policy': "sandbox; default-src 'none'"})


@router.post('/artifacts/{artifact_id}/open')
def open_artifact(artifact_id: str, request: Request):
    row = artifact(request, artifact_id)
    state = request.app.state
    if state.file_tasks.active():
        raise HTTPException(409, '请等待当前任务完成，再切换分析图纸。')
    drawing = state.store.one('SELECT * FROM drawings WHERE id=?', (row['drawing_id'],))
    if not drawing or drawing['status'] != 'ready' or not drawing['current_revision']:
        raise HTTPException(409, '该成果尚未生成可打开的图纸预览，可先下载文件。')
    state.store.execute('UPDATE sessions SET drawing_id=? WHERE id=?', (drawing['id'], row['session_id']))
    return {'drawing_id': drawing['id'], 'session_id': row['session_id']}
