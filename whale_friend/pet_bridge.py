"""dsh-pet 0.4 bridge protocol backed by the existing Companion and SQLite."""
from __future__ import annotations

import base64
import copy
import json
import mimetypes
import random
import threading
import time
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlsplit

from .assets import MAX_IMAGE_BYTES
from .pet_capabilities import PetVoice, VOICES, query_balance

PREFIX = '/dsh-pet-7340/'


def failure(message, reason='bad-request'):
    return {'ok': False, 'reason': reason, 'message': message}


class PetBridge:
    def __init__(self, core, root, config, session, settings=None):
        self.core, self.root, self.config, self.session = core, root, config, session
        self.closed = threading.Event()
        self.changed = threading.Condition(threading.RLock())
        leaf = lambda: {'counter': 0, 'data': None}
        self.state = {'counter': 0, 'sections': {key: leaf() for key in ('balance','workStatus','notify','config','menu','focusSession')},
                      'pets': {'main': {key: leaf() for key in ('say','anim','position','throw','lock','sleep')}}}
        self.latest_reply = core.store.rows("SELECT COALESCE(MAX(id),0) AS id FROM messages WHERE role='assistant' AND status='complete'")[0]['id']
        self.work = None
        self.pose = None
        self.voice = PetVoice(settings or core.settings)
        self.balance_lock = threading.Lock()
        self.balance_at = 0
        self.balance = None
        self.animation_event = None

    def write(self, group, key, data):
        with self.changed:
            self.state['counter'] += 1
            target = self.state['sections'] if group == 'sections' else self.state['pets']['main']
            target[key] = {'counter': self.state['counter'], 'data': data}
            self.changed.notify_all()

    def refresh(self):
        with self.changed:
            self._refresh_locked()

    def _refresh_locked(self):
        busy = bool(self.core.chat_pending)
        work = ('thinking', self.core.active_session) if busy else (None, self.session)
        if work != self.work:
            self.work = work
            self.write('sections', 'workStatus', {'state':work[0], 'source':'agent', 'sessionId':work[1],
                       'task':'本鱼正在想，等小尾巴晃一会儿…' if busy else None})
        replies = self.core.store.rows("SELECT id,session_id,content FROM messages WHERE role='assistant' AND status='complete' AND id>? ORDER BY id", (self.latest_reply,))
        for reply in replies:
            self.latest_reply = max(self.latest_reply, reply['id'])
            if reply['session_id'] != self.session:
                continue
            text = reply['content'].strip()
            if text:
                data = {'text': text[:600] + ('…（打开小屋查看完整回复）' if len(text) > 600 else ''), 'duration':25000, 'voiceTo':'desktop'}
                assets = self.core.store.rows('SELECT asset_id FROM message_assets WHERE message_id=? ORDER BY ordinal LIMIT 1', (reply['id'],))
                if assets:
                    data['url'] = 'dsh-pet-bridge://dsh-pet' + PREFIX + 'images/' + str(assets[0]['asset_id'])
                self.write('main', 'say', data)
        animation = self.core.animations.snapshot(self.session)
        event = animation.get('event')
        if animation.get('enabled') and event != self.animation_event:
            self.animation_event = event
            # Do not select paths/URLs from model output. The catalog is the
            # sole authority, and only clips present in this renderer are sent.
            file = self.core.animations.path(animation['id']).name
            if (self.root/'assets/webm'/file).is_file():
                self.write('main','anim',{'name':file[:-5]})

    def snapshot(self, query=None):
        query = query or {}
        try:
            since = int(query.get('since', [-1])[0])
            timeout = min(10, max(0, int(query.get('timeout', [0])[0]) / 1000))
        except (ValueError, TypeError):
            raise ValueError('状态轮询参数不正确') from None
        with self.changed:
            if self.state['counter'] == since and timeout:
                self.changed.wait_for(lambda: self.closed.is_set() or self.state['counter'] != since, timeout)
            return copy.deepcopy(self.state)

    def stop(self):
        self.closed.set()
        with self.changed:
            self.changed.notify_all()

    def _file(self, base, relative):
        base = base.resolve()
        path = (base / relative).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            raise ValueError('桌宠素材不存在或路径不正确')
        return {'file':str(path), 'contentType':mimetypes.guess_type(path.name)[0] or 'application/octet-stream'}

    def _chat(self, text, attachment=None):
        if self.closed.is_set():
            return failure('桌宠正在关闭', 'generate-error')
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 2000:
            return failure('请输入 1–2000 字的消息')
        images = []
        if attachment is not None:
            if not isinstance(attachment, dict) or not isinstance(attachment.get('data'), str) or len(attachment['data']) > MAX_IMAGE_BYTES * 4 // 3 + 8:
                return failure('图片格式不正确或超过 20 MB')
            try:
                data = base64.b64decode(attachment['data'], validate=True)
                images = [self.core.assets.import_bytes(data, original_name='桌宠附图.png')['id']]
            except (ValueError, OSError):
                return failure('图片无法读取，请重新选择')
        session = self.session
        # Let Companion perform admission, immediate memory tools and the one shared model call.
        try:
            user_id = self.core.send(session, text.strip(), attachments=images)
        except ValueError as error:
            return failure(str(error), 'generate-error')
        deadline = time.monotonic() + self.core.settings.timeout_seconds * 3 + 30
        while not self.closed.is_set() and time.monotonic() < deadline:
            rows = self.core.store.rows("SELECT id,content,status FROM messages WHERE session_id=? AND role='assistant' AND id>? ORDER BY id LIMIT 1", (session, user_id))
            if rows and rows[0]['status'] == 'complete':
                self.refresh()
                return {'ok':True, 'text':rows[0]['content']}
            if (rows and rows[0]['status'] in ('failed','interrupted')) or not self.core.chat_pending:
                return failure(self.core.last_chat_error or '回复未完成，可在小屋重试', 'generate-error')
            self.closed.wait(.08)
        return failure('等待回复已结束；已保存的内容可以在小屋查看', 'generate-error')

    def handle(self, method, raw_url, body=None):
        url = urlsplit(raw_url)
        if url.scheme or url.netloc or not url.path.startswith(PREFIX):
            raise ValueError('不支持的桌宠路由')
        route = unquote(url.path[len(PREFIX):])
        query = parse_qs(url.query)
        if query.get('pet', ['main'])[0] != 'main':
            return failure('未知桌宠')
        if route == 'state' and method == 'GET':
            return self.snapshot(query)
        if route == 'config' and method == 'GET':
            return copy.deepcopy(self.config)
        if route == 'voice/voices' and method == 'GET':
            return {'ok':True, 'voices':copy.deepcopy(VOICES)}
        if method == 'GET':
            if route.startswith('thumb/main/'):
                return self._file(self.root / 'assets/webm', route[len('thumb/main/'):])
            for name in ('font','pic','sound'):
                if route.startswith(name + '/'):
                    return self._file(self.root / 'assets' / ('fonts' if name == 'font' else name), route[len(name)+1:])
            if route == 'logo':
                return self._file(self.root / 'assets', 'logo.png')
            if route.startswith('images/'):
                path = self.core.assets.path(int(route.split('/')[1]))
                return {'file':str(path), 'contentType':mimetypes.guess_type(path.name)[0] or 'image/png'}
            if route == 'chat':
                records = self.core.store.messages(self.session, complete_only=True)[-80:]
                return {'ok':True, 'rounds':40, 'messages':[{'role':row['role'], 'content':row['content'], 'ts':int(datetime.fromisoformat(row['created_at']).timestamp()*1000)+row['id']%1000} for row in records]}
            if route == 'pose':
                return {'ok':True, 'pose':self.pose}
        if route == 'chat' and method == 'DELETE':
            return failure('请在小屋的记忆与日志页面管理原始记录', 'unsupported')
        if method != 'POST':
            return failure('此桌宠功能尚未接入小屋', 'unsupported')
        data = json.loads(body) if isinstance(body, str) and body else (body or {})
        if not isinstance(data, dict):
            raise ValueError('桌宠请求格式不正确')
        if route == 'balance':
            with self.balance_lock:
                # Repeated menu clicks share a short cache, never parallel calls.
                if self.balance is None or time.monotonic() - self.balance_at >= 30:
                    self.balance = query_balance(self.core.model)
                    self.balance_at = time.monotonic()
                self.write('sections','balance',{**self.balance,'manual':True})
            return {'ok':True}
        if route == 'voice/speak':
            return self.voice.speak(data)
        if route == 'chat':
            return self._chat(data.get('text', ''), data.get('attachment'))
        if route == 'whisper':
            return self._chat('本鱼，随口和我聊一句吧。')
        if route in ('focus-session','open'):
            session = data.get('sessionId', self.session)
            if not isinstance(session, str) or not self.core.store.rows('SELECT id FROM sessions WHERE id=?', (session,)):
                raise ValueError('会话不存在')
            self.core.emit('pet_open', session=session)
        elif route in ('hide','reload'):
            self.core.emit('pet_' + route)
        elif route == 'bubble/clear':
            self.write('main','say',{'clear':True})
        elif route == 'pose/report':
            self.pose = data
        elif route in ('sleep','wake'):
            sleeping = route == 'sleep'
            sets = self.config['main']['animations'].get('sleep', [])
            if sleeping and not sets:
                return failure('此角色未配置睡觉动画')
            names = {key:random.choice(sets[0][key]) for key in ('start','loop','end')} if sleeping else {}
            self.write('main','sleep',{'sleeping':sleeping, **names})
            return {'ok':True, 'sleeping':sleeping, **names}
        elif route in ('playback/report','input/report','hover/report'):
            pass
        else:
            return failure('此功能请在小屋中使用；当前接入动画、睡觉、桌面对话与共享记忆', 'unsupported')
        return {'ok':True}
