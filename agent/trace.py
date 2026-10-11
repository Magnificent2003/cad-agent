"""Local, per-task Markdown transcript of the upstream agent's observable loop.

No extra model calls. Retain tool payloads before engine history pruning, redact
credentials, and distinguish provider-returned reasoning from unavailable internals.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo


SECRET_KEY = re.compile(r"api[_-]?key|authorization|password|access[_-]?token|secret", re.I)
DATA_URL = re.compile(r"data:([\w.+/-]+);base64,([A-Za-z0-9+/=\r\n]+)")


class TaskTrace:
    def __init__(self, data_dir, task, request, secrets=()):
        self.task = dict(task)
        self.request = request
        self.directory = Path(data_dir) / 'agent-runs'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / (task['id'] + '.md')
        self.secrets = sorted({str(v) for v in secrets if v and len(str(v)) >= 6}, key=len, reverse=True)
        self.previous = set()
        self.messages = {}
        self.events = []
        self.dirty = True

    def clean(self, value):
        if isinstance(value, dict):
            return {str(k): '[REDACTED]' if SECRET_KEY.search(str(k)) else self.clean(v) for k, v in value.items()
                    if k not in {'system', 'encrypted_content'}}
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if not isinstance(value, str):
            return value
        for secret in self.secrets:
            value = value.replace(secret, '[REDACTED]')
        value = re.sub(r'(?i)(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+', r'\1 [REDACTED]', value)
        value = re.sub(r'(?i)(https?://)[^\s/@]+:[^\s/@]+@', r'\1[REDACTED]@', value)
        value = re.sub(r'''(?ix)((?:api[_-]?key|password|access[_-]?token|secret)["']?\s*[:=]\s*["']?)([^\s,"'<>]+)''', r'\1[REDACTED]', value)
        return DATA_URL.sub(self.attachment, value)

    def attachment(self, match):
        mime, encoded = match.groups()
        try:
            data = base64.b64decode(encoded, validate=False)
        except ValueError:
            return '[附件编码无效]'
        sha = hashlib.sha256(data).hexdigest()
        suffix = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}.get(mime)
        if suffix:
            folder = self.directory / (self.task['id'] + '_attachments')
            folder.mkdir(mode=0o700, exist_ok=True)
            target = folder / (sha + suffix)
            if not target.exists():
                target.write_bytes(data)
                target.chmod(0o600)
            return f'[图片附件: {mime}, {len(data)} bytes, sha256={sha}, file={target.relative_to(self.directory)}]'
        return f'[二进制附件: {mime}, {len(data)} bytes, sha256={sha}；正文不展开base64]'

    def note(self, kind, value):
        self.events.append({'time': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                            'kind': kind, 'value': self.clean(value)})
        self.dirty = True

    def message(self, info):
        identity = info.get('id')
        if not identity or identity in self.previous:
            return
        item = self.messages.setdefault(identity, {'info': {}, 'parts': {}})
        fields = ('id', 'role', 'agent', 'mode', 'modelID', 'providerID', 'time', 'finish', 'tokens', 'error', 'summary', 'synthetic')
        item['info'].update(self.clean({key: info[key] for key in fields if key in info}))
        self.dirty = True

    def part(self, part):
        parent, identity = part.get('messageID'), part.get('id')
        if not parent or not identity or parent in self.previous:
            return
        item = self.messages.setdefault(parent, {'info': {'id': parent}, 'parts': {}})
        old = item['parts'].get(identity)
        # Engine compaction can remove old tool outputs. Preserve the last full
        # observation already received, instead of replacing it with a placeholder.
        if old and old.get('type') == 'tool' and part.get('state', {}).get('time', {}).get('compacted'):
            return
        item['parts'][identity] = self.clean(deepcopy(part))
        self.dirty = True

    def observe(self, event):
        kind, props = event.get('type'), event.get('properties') or {}
        if kind == 'message.updated':
            self.message(props.get('info') or {})
        elif kind == 'message.part.updated':
            self.part(props.get('part') or {})
        elif kind == 'message.part.delta':
            item = self.messages.get(props.get('messageID'), {})
            part = item.get('parts', {}).get(props.get('partID'))
            if part and props.get('field') == 'text':
                part['text'] = part.get('text', '') + props.get('delta', '')
                self.dirty = True
        elif kind in {'session.error', 'session.compacted', 'message.removed', 'message.part.removed'}:
            self.note(kind, props)

    def reconcile(self, messages):
        for message in messages:
            info = message.get('info') or {}
            if info.get('id') in self.previous:
                continue
            self.message(info)
            for part in message.get('parts', []):
                self.part(dict(part, messageID=info.get('id')))

    @staticmethod
    def fence(value, language='json'):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        longest = max((len(match.group()) for match in re.finditer(r'`+', text)), default=0)
        fence = '`' * max(3, longest + 1)
        return f'\n{fence}{language}\n{text}\n{fence}\n'

    def flush(self, task=None):
        if task is not None:
            if any(self.task.get(k) != task.get(k) for k in ('status', 'error')):
                self.dirty = True
            self.task.update(task)
        if not self.dirty and self.path.exists():
            return
        lines = ['# CAD Agent 单轮运行记录\n',
                 f"任务：`{self.task['id']}` · 会话：`{self.task['session_id']}`\n",
                 f"状态：**{self.task.get('status', 'preparing')}** · 更新：{datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()}\n",
                 '记录来自实际运行，不增加模型请求。保留本轮模型消息、工具参数/返回、错误与自动续轮。'
                 '“模型返回的推理”仅指供应商实际返回的可见内容；未返回的内部推理不可观测。'
                 '本文件不展开系统提示或密钥；图片独立保存，其他二进制只记类型与哈希。'
                 '若引擎先行截断工具输出，其标记和完整文件位置保留在metadata，不能据摘要宣称捕获上游未返回的数据。\n',
                 '## 用户任务\n', self.fence(self.clean(self.request), 'text')]
        if self.task.get('error'):
            lines += ['## 终态错误\n', self.fence(self.clean(self.task['error']), 'text')]
        messages = sorted(self.messages.values(), key=lambda m: (m['info'].get('time', {}).get('created', 0), m['info'].get('id', '')))
        for index, message in enumerate(messages, 1):
            info = message['info']
            lines += [f"## 消息 {index} · {info.get('role', 'pending')} · {info.get('id', '')}\n", self.fence(info)]
            for part in message['parts'].values():
                kind = part.get('type')
                if kind in {'step-start', 'step-finish', 'snapshot'}:
                    continue
                if kind in {'text', 'reasoning'}:
                    label = '模型返回的推理/摘要' if kind == 'reasoning' else '文字'
                    if part.get('synthetic') or part.get('metadata', {}).get('cad_task_recovery'):
                        label += '（系统自动续轮）'
                    lines += [f'### {label}\n', self.fence(part.get('text', ''), 'text')]
                elif kind == 'tool':
                    state = part.get('state') or {}
                    lines += [f"### 工具 `{part.get('tool')}` · {state.get('status', 'pending')}\n",
                              '调用参数：\n', self.fence(state.get('input', {}))]
                    for field in ('output', 'error', 'metadata', 'attachments', 'time'):
                        if field in state:
                            lines += [f'{field}：\n', self.fence(state[field], 'text' if isinstance(state[field], str) else 'json')]
                else:
                    lines += [f'### {kind}\n', self.fence(part)]
        lines += ['## 运行事件\n', self.fence(self.events)]
        # Redact again after deltas; credentials can cross token boundaries.
        body = self.clean('\n'.join(lines))
        self.atomic(self.path, body)
        self.atomic(self.directory / 'latest.md',
                    f"# 最近一次 Agent 运行\n\n[{self.task['id']}]({self.path.name})\n\n状态：{self.task.get('status')}\n")
        self.dirty = False

    @staticmethod
    def atomic(path, text):
        temporary = path.with_suffix('.tmp')
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as stream:
            stream.write(text)
        os.replace(temporary, path)
