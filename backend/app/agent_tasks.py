"""Hand files to the agent and collect outputs; contains no CAD editing recipes."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import shutil
import time
import uuid

from agent.client import AgentError
from agent.preview import PreviewUnavailable, read_preview
from agent.session import is_internal_message, needs_continuation, terminal_assistant
from . import freecad
from .jobs import JobConflict, drawing_root
from .store import encode, now
from .config import ROOT

logger = logging.getLogger(__name__)
ACTIVE = ('preparing', 'running', 'collecting', 'stopping')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


class AgentTasks:
    def __init__(self, settings, store, bus, agent, jobs):
        self.settings, self.store, self.bus, self.agent, self.jobs = settings, store, bus, agent, jobs
        self.runners = {}
        self.poll_interval = 2
        self.timeout = 3600
        self.max_recoveries = 2
        self.max_review_retries = 2
        self.idle_reply_grace = 8
        self.cleanup_inactive_bindings()

    def root(self, task_id):
        return self.settings.data_dir / 'agent-tasks' / task_id

    def active(self, session_id=None):
        sql = "SELECT * FROM agent_tasks WHERE status IN ('preparing','running','collecting','stopping')"
        return self.store.one(sql + (' AND session_id=?' if session_id else ''), (session_id,) if session_id else ())

    def set_status(self, task_id, status, error=None):
        self.store.execute('UPDATE agent_tasks SET status=?,error=?,updated_at=? WHERE id=?', (status, error, now(), task_id))
        task = self.store.one('SELECT * FROM agent_tasks WHERE id=?', (task_id,))
        busy = status in ACTIVE
        if not busy:
            self.clear_binding(task)
        self.store.execute('UPDATE sessions SET busy=? WHERE id=?', (int(busy), task['session_id']))
        self.bus.publish({'type': 'artifacts.updated', 'properties': {'sessionID': task['session_id']}})
        self.bus.publish({'type': 'session.status', 'properties': {'sessionID': task['session_id'], 'status': {'type': 'busy' if busy else 'idle'}}})

    async def ensure_ready(self):
        mcp = await self.agent.mcp_status()
        if set(mcp) != {'freecad'} or mcp['freecad'].get('status') != 'connected':
            raise AgentError('网页必须连接唯一的 FreeCAD MCP；请重启 bash scripts/dev.sh，旧工具链不会作为备用。', 503)
        if not await freecad.idle():
            raise AgentError('FreeCAD 尚未就绪或仍在执行操作，请等待完成；若窗口已卡住，请重启全部服务。', 503)
        if any(item.get('type') not in {None, 'idle'} for item in (await self.agent.session_status()).values()):
            raise JobConflict('智能体仍有任务在运行，请等待或停止后再提交。')

    def binding_path(self, session_id):
        if not session_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in session_id):
            raise ValueError('无效的智能体会话标识')
        return self.settings.data_dir / 'agent-contexts' / (session_id + '.json')

    def read_binding(self, path):
        # Bindings are auxiliary state. A damaged file must not prevent the
        # authoritative database status and busy flag from reaching a terminal state.
        if path.is_symlink():
            return None
        try:
            context = json.loads(path.read_text())
            if not isinstance(context, dict):
                raise ValueError('任务上下文不是对象')
            return context
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            logger.warning('Cannot read task binding %s (%s)', path.name, type(exc).__name__)
            return None

    def clear_binding(self, task):
        path = self.binding_path(task['session_id'])
        context = self.read_binding(path)
        if context and context.get('task_id') == task['id'] and context.get('session_id') == task['session_id']:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning('Cannot remove task binding %s (%s)', path.name, type(exc).__name__)

    def cleanup_inactive_bindings(self):
        # Store.initialize() interrupts old tasks before this manager starts.
        # Only remove files positively matched to a known, inactive task.
        directory = self.settings.data_dir / 'agent-contexts'
        if directory.is_symlink():
            return
        for path in directory.glob('*.json'):
            context = self.read_binding(path)
            if not context or not isinstance(context.get('task_id'), str):
                continue
            task = self.store.one('SELECT id,session_id,status FROM agent_tasks WHERE id=?', (context['task_id'],))
            if (task and task['status'] not in ACTIVE and context.get('session_id') == task['session_id']
                    and path == self.binding_path(task['session_id'])):
                self.clear_binding(task)

    def prepare(self, task, drawing, text):
        root = self.root(task['id'])
        for directory in ('input', 'output', 'scratch'):
            (root / directory).mkdir(parents=True, exist_ok=True)
        original = drawing_root(self.settings, drawing['id'])
        files = [original / 'revisions' / task['source_revision'] / 'drawing.dxf']
        if task['source_revision'] == 'r0001' and drawing['source_suffix'] == '.dwg':
            files.append(original / 'source/original.dwg')
        inputs = []
        for source in files:
            target = root / 'input' / ('drawing' + source.suffix)
            shutil.copy2(source, target)
            target.chmod(0o444)
            inputs.append({'path': str(target.resolve()), 'sha256': digest(target), 'format': source.suffix[1:]})
        # Reuse only this revision's existing preview; no rendering or fallback
        # to another revision is performed while admitting a model task.
        preview = {'available': False, 'drawing_id': drawing['id'], 'revision_id': task['source_revision'],
                   'note': '只读总览，仅供理解位置和内容；可能省略不支持的对象。精确单位、坐标和修改以 CAD 原文件为准。'}
        checked_inputs = list(inputs)
        try:
            image_data, image_info = read_preview(original / 'revisions' / task['source_revision'] / 'preview.png')
        except PreviewUnavailable as exc:
            preview['reason'] = str(exc)
        else:
            target = root / 'input/overview.png'
            try:
                target.write_bytes(image_data)
                target.chmod(0o444)
            except OSError:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                preview['reason'] = '无法准备当前版本的预览副本，继续使用 CAD 文件。'
            else:
                preview.update(available=True, path=str(target.resolve()), mime='image/png',
                               sha256=digest(target), **image_info)
                checked_inputs.append({'path': preview['path'], 'sha256': preview['sha256'], 'format': 'png'})
        previous = self.store.all('SELECT id,name,path FROM artifacts WHERE session_id=? ORDER BY created_at', (task['session_id'],))
        context = {'task_id': task['id'], 'session_id': task['session_id'], 'drawing_id': drawing['id'],
                   'drawing_name': drawing['name'], 'revision_id': task['source_revision'],
                   'units': (drawing.get('metadata') or {}).get('units'), 'input_files': inputs,
                   'output_directory': str((root / 'output').resolve()),
                   'scratch_directory': str((root / 'scratch').resolve()),
                   'previous_artifacts': [{'name': item['name'], 'path': item['path']} for item in previous],
                   'request': text, 'preview': preview,
                   'review': {'manifest': str((root / 'scratch/cad-review.json').resolve()),
                              'render_settings': self.settings.cad_settings(),
                              'note': '修改后的 DXF 交付前使用 cad_review；场布选择 site_layout，未知事实不算通过。'}}
        # This inventory belongs to FreeCAD's Python, not the backend virtualenv.
        installed = ROOT / 'freecad/runtime/python-deps/installed.lock.json'
        if installed.is_file():
            lock = json.loads(installed.read_text())
            context['runtime'] = {'python': lock['environment']['python'],
                                  'packages': {item['name']: item['version'] for item in lock['packages']},
                                  'dependency_source': str(installed),
                                  'note': '专用依赖在服务启动时校验；历史缺依赖结论不能代表本轮环境。'}
        (root / 'context.json').write_text(encode(context), encoding='utf-8')
        binding = self.binding_path(task['session_id'])
        binding.parent.mkdir(parents=True, exist_ok=True)
        temporary = binding.with_suffix('.tmp')
        temporary.write_text(encode(context), encoding='utf-8')
        temporary.replace(binding)
        self.store.execute('UPDATE agent_tasks SET inputs=? WHERE id=?', (encode(checked_inputs), task['id']))
        return context

    async def start(self, session, drawing, text):
        if session.get('engine') != 'freecad':
            raise AgentError('旧会话只保留历史记录。请新建对话，使用 FreeCAD 自主执行能力。', 409)
        if self.active():
            raise JobConflict('当前 FreeCAD 正在处理另一项任务，请等待完成或停止该任务。')
        await self.ensure_ready()
        task_id = uuid.uuid4().hex
        with self.store.transaction() as db:
            # Serialise claims even when requests passed the asynchronous health checks together.
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT id FROM agent_tasks WHERE status IN ('preparing','running','collecting','stopping')").fetchone():
                raise JobConflict('FreeCAD 已被另一项任务使用，请稍后重试。')
            current = db.execute('SELECT s.drawing_id,d.current_revision FROM sessions s JOIN drawings d ON d.id=s.drawing_id WHERE s.id=?', (session['id'],)).fetchone()
            if current['drawing_id'] != drawing['id'] or current['current_revision'] != drawing['current_revision']:
                raise JobConflict('图纸在提交期间发生变化，请重新提交。')
            db.execute('INSERT INTO agent_tasks (id,session_id,drawing_id,source_revision,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)',
                       (task_id, session['id'], drawing['id'], drawing['current_revision'], 'preparing', now(), now()))
            db.execute('UPDATE sessions SET busy=1,busy_since=? WHERE id=?', (time.time(), session['id']))
        task = self.store.one('SELECT * FROM agent_tasks WHERE id=?', (task_id,))
        try:
            context = await asyncio.to_thread(self.prepare, task, drawing, text)
            previous = {message['info']['id'] for message in await self.agent.messages(session['id'])}
            if self.store.one('SELECT status FROM agent_tasks WHERE id=?', (task_id,))['status'] == 'stopping':
                self.set_status(task_id, 'cancelled')
                return task_id
            await self.agent.prompt_async(
                session['id'], text, system='本轮可信文件任务上下文（以此覆盖历史文件目标）：' + encode(context),
                preview=Path(context['preview']['path']) if context['preview']['available'] else None,
            )
            if self.store.one('SELECT status FROM agent_tasks WHERE id=?', (task_id,))['status'] != 'stopping':
                self.set_status(task_id, 'running')
            self.runners[task_id] = asyncio.create_task(self.monitor(task_id, previous))
        except Exception:
            try:
                await self.agent.abort(session['id'])
            except Exception:
                pass
            self.set_status(task_id, 'failed', '任务未能提交，请检查服务后重试。')
            raise
        return task_id

    def collect(self, task, checked_files=None, contract_sha256=None):
        root = self.root(task['id'])
        for item in task['inputs']:
            if digest(item['path']) != item['sha256']:
                raise ValueError('智能体修改了输入副本，本轮成果未发布；项目原图仍保留。')
        if checked_files is not None:
            contract = root / 'scratch/cad-review.json'
            current_contract = digest(contract) if contract.is_file() else None
            if current_contract != contract_sha256:
                raise ValueError('检查契约在验收后发生变化，本轮成果未发布。')
        directory = (root / 'output').resolve()
        if directory.parent != root.resolve() or (root / 'output').is_symlink():
            raise ValueError('输出目录被替换，本轮成果未发布。')
        # Any final file format is supported; preview capability is handled separately.
        for source in sorted(directory.rglob('*')):
            if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(directory):
                continue
            relative = source.relative_to(directory)
            if any(part.startswith('.') for part in relative.parts) or source.suffix.lower() in {'.tmp', '.part', '.incomplete'}:
                continue
            if source.stat().st_size > self.settings.cad_max_upload_mb * 1024 * 1024:
                raise ValueError('成果文件超过配置的文件大小限制，本轮未发布。')
            artifact_id = uuid.uuid4().hex
            target = self.settings.data_dir / 'artifacts' / artifact_id / source.name
            target.parent.mkdir(parents=True)
            before = digest(source)
            if (checked_files is not None and source.suffix.lower() == '.dxf'
                    and checked_files.get(relative.as_posix()) != before):
                raise ValueError('DXF 在验收后新增或变化，本轮成果未发布。')
            shutil.copy2(source, target)
            if digest(target) != before or digest(source) != before:
                raise ValueError('输出文件仍在变化，本轮成果未发布。')
            target.chmod(0o444)
            self.store.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?)',
                               (artifact_id, task['id'], task['session_id'], relative.as_posix(), str(target.resolve()), before,
                                target.stat().st_size, None, None, now()))

    def import_artifacts(self, task):
        # Preview/import is a generic file service, never an alternative editing tool.
        for artifact in self.store.all('SELECT * FROM artifacts WHERE task_id=?', (task['id'],)):
            source = Path(artifact['path'])
            if source.suffix.lower() not in {'.dxf', '.dwg'} or artifact['drawing_id']:
                continue
            drawing_id = uuid.uuid4().hex
            destination = drawing_root(self.settings, drawing_id) / 'source' / ('original' + source.suffix.lower())
            destination.parent.mkdir(parents=True)
            shutil.copy2(source, destination)
            destination.chmod(0o444)
            (destination.parent / 'sha256.txt').write_text(artifact['sha256'])
            parent = self.store.one('SELECT project_id FROM drawings WHERE id=?', (task['drawing_id'],))
            self.store.execute('INSERT INTO drawings VALUES (?,?,?,?,?,?,?)',
                               (drawing_id, parent['project_id'], artifact['name'], source.suffix.lower(), None, 'importing', now()))
            job = self.jobs.submit(drawing_id, 'import', {})
            self.store.execute('UPDATE artifacts SET drawing_id=?,job_id=? WHERE id=?', (drawing_id, job['id'], artifact['id']))

    async def monitor(self, task_id, previous):
        started = time.monotonic()
        task = self.store.one('SELECT * FROM agent_tasks WHERE id=?', (task_id,))
        session_id = task['session_id']
        recoveries = []
        review_retries = []
        idle_since = None
        try:
            while True:
                state = self.store.one('SELECT status FROM agent_tasks WHERE id=?', (task_id,))['status']
                if state == 'stopping':
                    stopped = await self.agent.abort(session_id)
                    if stopped and await freecad.idle():
                        self.set_status(task_id, 'cancelled')
                        return
                else:
                    messages = await self.agent.messages(session_id)
                    terminal = terminal_assistant(messages, previous)
                    idle = (await self.agent.session_status()).get(session_id, {}).get('type') in {None, 'idle'}
                    if idle and await freecad.idle():
                        # Cancellation can arrive during any readiness await;
                        # never overwrite stopping with collecting or a recovery.
                        if self.store.one('SELECT status FROM agent_tasks WHERE id=?', (task_id,))['status'] == 'stopping':
                            continue
                        current = [message for message in messages if message['info']['id'] not in previous]
                        if any(is_internal_message(message) and message['info'].get('error') for message in current):
                            self.set_status(task_id, 'failed', '智能体内部整理或续跑失败，未生成正式答复；本轮输出未发布。')
                            return
                        if terminal and terminal['info'].get('error'):
                            self.set_status(task_id, 'failed', '模型任务失败；本轮输出未自动发布。')
                            return
                        # Automatic compaction can briefly appear idle between its summary
                        # and continuation. Require sustained engine AND FreeCAD idleness.
                        if terminal:
                            idle_since = None
                        elif idle_since is None:
                            idle_since = time.monotonic()
                        missing_reply = idle_since is not None and time.monotonic() - idle_since >= self.idle_reply_grace
                        if (terminal and needs_continuation(terminal)) or missing_reply:
                            if len(recoveries) >= self.max_recoveries:
                                self.set_status(task_id, 'failed', '模型连续截断或未给出正式答复，自动续跑仍未完成；本轮输出未发布。')
                                return
                            # Resume the same file task, preserving its input/output and history.
                            # Only count abnormal model termination, never ordinary tool iterations.
                            context = json.loads((self.root(task_id) / 'context.json').read_text())
                            previous = {message['info']['id'] for message in messages}
                            reason = terminal['info'].get('finish') if terminal else 'idle-without-answer'
                            recovery_message = terminal or (current[-1] if current else None)
                            recoveries.append({'message_id': recovery_message['info']['id'] if recovery_message else None,
                                               'finish': reason, 'at': now()})
                            (self.root(task_id) / 'recovery.json').write_text(encode(recoveries), encoding='utf-8')
                            logger.warning('Resuming task %s after incomplete model reply (%s), attempt %d',
                                           task_id, reason, len(recoveries))
                            idle_since = None
                            await self.agent.prompt_async(
                                session_id,
                                '上一段执行已停止，但因截断、空答复或内部整理后未继续，用户尚未收到完整结果。'
                                '请在同一任务中继续：先利用已有证据完成用户原始要求并给出简洁中文答复；'
                                '仅在确有关键缺口时继续调用工具，不重复已完成操作，不扩大任务。'
                                '内部摘要不是用户答复；若仍有阻碍，明确已完成部分和具体阻碍。',
                                system='本轮可信文件任务上下文（以此覆盖历史文件目标）：' + encode(context),
                                internal=True,
                                preview=Path(context['preview']['path']) if context.get('preview', {}).get('available') else None,
                            )
                            continue
                        if terminal:
                            if terminal['info'].get('finish') != 'stop':
                                self.set_status(task_id, 'failed', '模型未正常结束本轮答复；本轮输出未自动发布。')
                                return
                            # A model's final answer cannot substitute for a check of saved geometry.
                            from cad_domain.delivery import feedback_preview, feedback_summary, mark_feedback, review_delivery
                            context = json.loads((self.root(task_id) / 'context.json').read_text())
                            review = await asyncio.to_thread(review_delivery, context)
                            if self.store.one('SELECT status FROM agent_tasks WHERE id=?', (task_id,))['status'] == 'stopping':
                                continue
                            if review['status'] != 'passed':
                                if len(review_retries) >= self.max_review_retries:
                                    details = '；'.join(issue.get('message', issue.get('code', '未知问题'))
                                                       for issue in review.get('issues', [])[:3])
                                    self.set_status(task_id, 'failed', '成果检查未通过，未发布本轮文件：' + details)
                                    return
                                preview = await asyncio.to_thread(feedback_preview, context, review, self.settings.cad_settings())
                                if self.store.one('SELECT status FROM agent_tasks WHERE id=?', (task_id,))['status'] == 'stopping':
                                    continue
                                previous = {message['info']['id'] for message in messages}
                                review_retries.append({'at': now(), 'status': review['status'],
                                                       'files': [{key: item.get(key) for key in ('name', 'file_sha256', 'status')}
                                                                 for item in review.get('files', [])],
                                                       'issues': review.get('issues', [])})
                                (self.root(task_id) / 'scratch/review-attempts.json').write_text(encode(review_retries), encoding='utf-8')
                                # This budget only bounds post-answer recovery; ordinary tool use is unrestricted.
                                image_sha = await self.agent.prompt_async(
                                    session_id,
                                    '刚才的答复尚未通过实际成果检查，文件尚未发布。继续同一任务，先读取下面的具体问题，'
                                    '修正当前 DXF 或补齐真实依据，再调用 cad_review；不要重复完成的分析，不得放宽要求冒充通过。'
                                    '如依据不足或无法布置，明确未完成的内容，并从 output_directory 移走未通过的候选图到 scratch_directory；'
                                    '可以仅交付说明缺口的报告，不得宣称施工验收通过。附件来自当前实际输出文件。\n'
                                    + encode(feedback_summary(review)),
                                    system='本轮可信文件任务上下文（以此覆盖历史文件目标）：' + encode(context),
                                    internal=True, preview=preview,
                                )
                                if image_sha and preview:
                                    item = next(item for item in review['files'] if item.get('preview', {}).get('path') == str(preview))
                                    try:
                                        await asyncio.to_thread(mark_feedback, context, Path(item['path']), item, image_sha256=image_sha)
                                    except ValueError:
                                        # A fast continuation may already have changed the file.
                                        # Its next check must supply fresh feedback; do not abort the repair.
                                        logger.info('Review image became stale during task continuation: %s', task_id)
                                idle_since = None
                                continue
                            self.set_status(task_id, 'collecting')
                            checked = {item['name']: item['file_sha256'] for item in review['files']}
                            await asyncio.to_thread(self.collect, task, checked, review.get('contract_sha256'))
                            # Abort may have arrived while copying files.
                            if self.store.one('SELECT status FROM agent_tasks WHERE id=?', (task_id,))['status'] == 'stopping':
                                self.store.execute('DELETE FROM artifacts WHERE task_id=?', (task_id,))
                                self.set_status(task_id, 'cancelled')
                                return
                            self.import_artifacts(task)
                            self.set_status(task_id, 'completed')
                            return
                    else:
                        idle_since = None
                if time.monotonic() - started > self.timeout and state != 'stopping':
                    self.set_status(task_id, 'stopping', '本轮超过一小时，正在停止模型并等待 FreeCAD 结束操作。')
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(self.agent.abort(session_id), 5)
            except Exception:
                pass
            self.set_status(task_id, 'interrupted', '服务停止，本轮未完成成果不会发布。')
            raise
        except Exception:
            logger.exception('Agent file task failed: %s', task_id)
            try:
                await self.agent.abort(session_id)
            except Exception:
                pass
            self.store.execute('DELETE FROM artifacts WHERE task_id=?', (task_id,))
            self.set_status(task_id, 'failed', '文件任务处理失败，请查看后端日志；原图已保留。')
        finally:
            self.runners.pop(task_id, None)

    async def abort(self, session_id):
        task = self.active(session_id)
        if task:
            self.set_status(task['id'], 'stopping')
        stopped = await self.agent.abort(session_id)
        if task and task['id'] in self.runners and stopped and await freecad.idle():
            # Let monitor perform the terminal transition and exclude candidate outputs.
            return False
        return stopped and task is None

    async def close(self):
        runners = list(self.runners.values())
        for runner in runners:
            runner.cancel()
        await asyncio.gather(*runners, return_exceptions=True)
