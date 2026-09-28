"""Real Baigiao drawing retest via the web API; no synthetic drawing or answers.

Run with services started: .venv/bin/python dev_log/engineering_retest.py on
The label documents which version was actually started, it does not toggle code.
Runtime evidence is kept in normal data/ task directories, never copied to Git.
"""
import argparse
import json
from pathlib import Path
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'data/agent-tasks/786b4ced67ef4d1198bfe8bdf0359aee/input/drawing.dxf'
PROMPT = '''此前对话背景：我已经让你从白桥一号园（二期）总平面施工图中导出图框内内容，再导出目前输入的 v0009《建筑红线内_施工场地布置底图.dxf》。上一轮只分析、不改图时，你建议在厂房之间的场地中部空地安排塔吊、钢筋/模板加工棚、材料堆场和临时施工道路，塔吊面向 7#～10# 厂房；其他设施按需要考虑。这些是上一轮的建议，上一轮对边界、尺度、空地面积的推断不作为已经确认的条件，请结合当前原文件自行核对。

本轮请求：
可以，按照你的想法，对场地中间的最大空地部分进行布置，按照施工要求布置必要施工设施（包括不限于你说的塔吊（覆盖 7#~10# 厂房）、钢筋/模板加工棚、材料堆场、临时施工道路），并生成施工场布建议文件。'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('label', choices=['on', 'off'])
    args = parser.parse_args()
    client = httpx.Client(base_url='http://127.0.0.1:8000', timeout=40, trust_env=False)
    project = client.post('/api/projects', json={'name': f'工程实测0928_模块{args.label}_白桥v0009'})
    project.raise_for_status()
    upload = client.post(f"/api/projects/{project.json()['id']}/drawings",
                         files={'file': ('建筑红线内_施工场地布置底图.dxf', SOURCE.read_bytes())})
    upload.raise_for_status()
    ids = upload.json()
    for _ in range(240):
        job = client.get('/api/jobs/' + ids['job_id']).json()
        if job['status'] == 'succeeded':
            break
        if job['status'] in {'failed', 'interrupted', 'cancelled'}:
            raise RuntimeError(job)
        time.sleep(1)
    else:
        raise TimeoutError('Real drawing import did not complete')
    session = client.post(f"/api/drawings/{ids['drawing_id']}/sessions")
    session.raise_for_status()
    session_id = session.json()['id']
    response = client.post(f'/api/sessions/{session_id}/messages', json={'text': PROMPT, 'expected_revision': 'r0001'})
    response.raise_for_status()
    record = {**ids, 'project_id': project.json()['id'], 'session_id': session_id,
              'task_id': response.json()['task_id'], 'label': args.label, 'started': time.time()}
    output = ROOT / 'data' / f'engineering-retest-{args.label}.json'
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    print(json.dumps(record, ensure_ascii=False), flush=True)
    previous = None
    for _ in range(1200):
        result = client.get(f'/api/sessions/{session_id}/artifacts').json()
        history = client.get(f'/api/sessions/{session_id}/messages').json()
        tools = [(part.get('tool'), part.get('state', {}).get('status'))
                 for message in history for part in message['parts'] if part['type'] == 'tool']
        progress = {'status': result['task']['status'], 'tools': tools}
        if progress != previous:
            print(json.dumps(progress, ensure_ascii=False), flush=True)
            previous = progress
        if result['task']['status'] not in {'running', 'preparing', 'collecting', 'stopping'}:
            record.update(elapsed_seconds=round(time.time() - record['started']), result=result,
                          answers=[part['text'] for m in history if m['role'] == 'assistant'
                                   for part in m['parts'] if part['type'] == 'text'])
            output.write_text(json.dumps(record, ensure_ascii=False, indent=2))
            print(json.dumps(record, ensure_ascii=False), flush=True)
            return
        time.sleep(2)
    raise TimeoutError('Engineering task still running; inspect the recorded session before deciding whether to abort.')


if __name__ == '__main__':
    main()
