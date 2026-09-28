# Historical experiment for commit 2bd013d; not evidence of real engineering quality.
"""One opt-in paid model smoke via the same API as the web UI.

Run only after starting dev.sh. Creates an explicitly named synthetic project;
never edits an existing engineering project. Normal task logs stay under data/.
"""
import io
import json
import math
from pathlib import Path
import time

import ezdxf
import httpx


def main():
    client = httpx.Client(base_url='http://127.0.0.1:8000', timeout=30, trust_env=False)
    doc = ezdxf.new('R2018')
    doc.units = 4
    angle = math.radians(31)
    origin = (500_000, 4_000_000)
    def world(points):
        return [(origin[0] + 1000 * (x * math.cos(angle) - y * math.sin(angle)),
                 origin[1] + 1000 * (x * math.sin(angle) + y * math.cos(angle))) for x, y in points]
    doc.layers.new('CONFIRMED_BOUNDARY', dxfattribs={'color': 3})
    doc.layers.new('EXISTING_BUILDING', dxfattribs={'color': 7})
    boundary = doc.modelspace().add_lwpolyline(world([(0, 0), (40, 0), (40, 30), (0, 30)]), close=True,
                                               dxfattribs={'layer': 'CONFIRMED_BOUNDARY'})
    obstacle = doc.modelspace().add_lwpolyline(world([(16, 10), (24, 10), (24, 20), (16, 20)]), close=True,
                                               dxfattribs={'layer': 'EXISTING_BUILDING'})
    doc.modelspace().add_text('EXISTING', dxfattribs={'insert': (*world([(17, 14)])[0], 0), 'height': 750, 'rotation': 31})
    stream = io.StringIO()
    doc.write(stream)
    project = client.post('/api/projects', json={'name': '开发验证0928_合成场地_非工程成果'})
    project.raise_for_status()
    upload = client.post(f"/api/projects/{project.json()['id']}/drawings", files={'file': ('round1_rotated_site.dxf', stream.getvalue().encode())})
    upload.raise_for_status()
    ids = upload.json()
    for _ in range(120):
        job = client.get('/api/jobs/' + ids['job_id']).json()
        if job['status'] == 'succeeded':
            break
        if job['status'] == 'failed':
            raise RuntimeError(job)
        time.sleep(.5)
    else:
        raise TimeoutError('Import did not finish')
    session = client.post(f"/api/drawings/{ids['drawing_id']}/sessions")
    session.raise_for_status()
    session_id = session.json()['id']
    prompt = (f'这是开发验证用的合成场地。作为出图者，我明确确认：模型单位为毫米；句柄 {boundary.dxf.handle} 是唯一的场地边界；'
              f'句柄 {obstacle.dxf.handle} 是唯一已有建筑障碍，除此之外没有障碍物。原有边线和文字都须保留。'
              '请在边界内布置一间6m×4m的办公室和一间8m×5m的仓库，位置自行选择；设施之间、与已有建筑、与边界至少保持1m间距。'
              '设施长边平行于场地40m长边，图面只标简短名称。原图位于测量坐标系，不得搬动或旋转底图。'
              '请生成一份新的DXF并复核实际成果；不得自行缩小设施或降低间距。无需道路、规范验算或长报告。')
    response = client.post(f'/api/sessions/{session_id}/messages', json={'text': prompt, 'expected_revision': 'r0001'})
    response.raise_for_status()
    record = {**ids, 'project_id': project.json()['id'], 'session_id': session_id,
              'task_id': response.json()['task_id'], 'prompt': prompt}
    Path('/tmp/cad_round1_live.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(json.dumps(record, ensure_ascii=False), flush=True)
    previous = None
    for _ in range(450):
        result = client.get(f'/api/sessions/{session_id}/artifacts').json()
        history = client.get(f'/api/sessions/{session_id}/messages').json()
        tools = [(part.get('tool'), part.get('state', {}).get('status'))
                 for message in history for part in message['parts'] if part['type'] == 'tool']
        progress = {'status': result['task']['status'], 'tools': tools}
        if progress != previous:
            print(json.dumps(progress, ensure_ascii=False), flush=True)
            previous = progress
        if result['task']['status'] not in {'running', 'preparing', 'collecting', 'stopping'}:
            print(json.dumps({'result': result, 'answers': [part['text'] for message in history if message['role'] == 'assistant'
                                for part in message['parts'] if part['type'] == 'text']}, ensure_ascii=False), flush=True)
            return
        time.sleep(2)
    raise TimeoutError('Model smoke is still running; inspect/abort the recorded test session explicitly.')


if __name__ == '__main__':
    main()
