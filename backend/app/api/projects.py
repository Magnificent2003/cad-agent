"""Project-level workbench navigation; the agent's file execution is unchanged."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

router = APIRouter(prefix='/api/projects')


def get_project(request, project_id):
    project = request.app.state.store.one('SELECT * FROM projects WHERE id=?', (project_id,))
    if not project:
        raise HTTPException(404, '工程项目不存在。')
    return project


class ProjectName(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@router.patch('/{project_id}')
def rename(project_id: str, body: ProjectName, request: Request):
    get_project(request, project_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(422, '请输入项目名称。')
    request.app.state.store.execute('UPDATE projects SET name=? WHERE id=?', (name, project_id))
    return get_project(request, project_id)


@router.get('/{project_id}/workbench')
def workbench(project_id: str, request: Request):
    project = get_project(request, project_id)
    store = request.app.state.store
    # Existing drawings already carry project_id, including generated variants.
    # Immutable revision timestamps give old and new files one append-only order.
    rows = store.all('''
        SELECT d.id AS drawing_id,d.name,d.current_revision,r.id AS revision_id,
               r.created_at,r.summary,a.id AS artifact_id
        FROM drawings d JOIN revisions r ON r.drawing_id=d.id
        LEFT JOIN artifacts a ON a.drawing_id=d.id
        LEFT JOIN agent_tasks t ON t.id=a.task_id
        WHERE d.project_id=? AND (a.id IS NULL OR t.status='completed')
        ORDER BY r.created_at,d.created_at,d.id,r.number
    ''', (project_id,))
    versions = [{**row, 'key': f"{row['drawing_id']}:{row['revision_id']}",
                 'number': number, 'label': f"v{number:04d}_{Path(row['name']).stem}"}
                for number, row in enumerate(rows, 1)]
    sessions = store.all('''
        SELECT s.id,s.drawing_id,s.title,s.engine,s.busy,s.created_at
        FROM sessions s JOIN drawings d ON d.id=s.drawing_id
        WHERE d.project_id=? ORDER BY s.created_at DESC,s.id
    ''', (project_id,))
    for session in sessions:
        session['status'] = 'busy' if session.pop('busy') else 'idle'
    files = store.all('''
        SELECT a.id,a.name,a.bytes,a.task_id,a.drawing_id,a.session_id,
               j.status AS preview_status,j.error AS preview_error
        FROM artifacts a JOIN agent_tasks t ON t.id=a.task_id
        JOIN drawings d ON d.id=t.drawing_id LEFT JOIN jobs j ON j.id=a.job_id
        WHERE d.project_id=? AND t.status='completed' ORDER BY a.created_at,a.id
    ''', (project_id,))
    for file in files:
        file['preview_status'] = file['preview_status'] or 'unavailable'
        file['download_url'] = f"/api/artifacts/{file['id']}/download"
    return {'project': project, 'versions': versions, 'sessions': sessions, 'artifacts': files}
