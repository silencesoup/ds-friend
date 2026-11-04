"""Lifecycle of the optional dsh-pet renderer; all model work stays in Companion."""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from .config import LOCAL
from .pet_bridge import PetBridge
from .pet_support import configuration, find_electron, find_plugin, prepare_helper


def callback_url(value):
    url = urlsplit(value)
    if url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or url.path != '/respond' or url.query or url.fragment or url.username or url.password:
        raise ValueError('桌宠回调地址不正确')
    return value


class PetRuntime:
    def __init__(self, core, settings, session):
        self.core, self.settings = core, settings
        self.root = find_plugin(settings.pet_plugin_dir)
        self.electron = find_electron(self.root, settings.pet_electron_path)
        self.helper = prepare_helper(self.root)
        self.bridge = PetBridge(core, self.root, configuration(self.root, settings), session, settings)
        self.process = None
        self.control_port = None
        self.control_token = secrets.token_hex(32)
        self.pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix='pet-bridge')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.closed = False
        self.stderr_lines = deque(maxlen=12)

    @property
    def session(self):
        return self.bridge.session

    @session.setter
    def session(self, value):
        self.bridge.session = value

    def start(self):
        profile = LOCAL / 'dsh-pet-profile'
        profile.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.pop('ELECTRON_RUN_AS_NODE', None)
        env.update(DSH_PET_BRIDGE='1', DSH_PET_APP_MODE='0', DSH_PET_SMOKE='0', DSH_PET_DPI_PROBE='0',
                   DSH_HOME=str(LOCAL / 'dsh-pet-home'),
                   DSH_PET_HOST_PID=str(os.getpid()), DSH_PET_OPEN_TARGET='desktop',
                   DSH_PET_CONFIG_URL='http://127.0.0.1/dsh-pet-7340/config', DSH_PET_GPU_SANDBOX='1',
                   DSH_PET_PETS=json.dumps([{'id':'main','size':self.settings.pet_size,'index':0}]),
                   DS_FRIEND_PET_PROFILE=str(profile), DS_FRIEND_PET_CONTROL=self.control_token,
                   DS_FRIEND_PET_TIMEOUT_MS=str((self.settings.timeout_seconds * 3 + 60) * 1000))
        options = {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        self.process = subprocess.Popen([str(self.electron), str(self.helper / 'bootstrap.cjs')], cwd=self.helper, env=env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
        threading.Thread(target=self._read, daemon=True, name='pet-renderer').start()
        threading.Thread(target=self._read_errors, daemon=True, name='pet-renderer-errors').start()
        threading.Thread(target=self._observe, daemon=True, name='pet-observer').start()
        return self

    def _read_errors(self):
        try:
            for line in self.process.stderr:
                self.stderr_lines.append(line.decode('utf-8',errors='replace').strip()[:1000])
        except OSError:
            pass

    def _observe(self):
        while not self.bridge.closed.wait(.2):
            try:
                self.bridge.refresh()
            except Exception:
                if not self.bridge.closed.is_set():
                    self.core.emit('pet_error', message='桌宠状态暂未同步，可关闭桌宠后重新启动。')
                return

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(30 * 1024 * 1024)
                if not line:
                    break
                text = line.decode('utf-8', errors='replace').strip()
                if text.startswith('dsh-pet-bridge:'):
                    self.pool.submit(self._respond, json.loads(text[len('dsh-pet-bridge:'):]))
                elif text.startswith('ds-friend-pet:'):
                    event = json.loads(text[len('ds-friend-pet:'):])
                    if event.get('kind') == 'control' and type(event.get('port')) is int and 0 < event['port'] <= 65535:
                        self.control_port = event['port']
                    elif event.get('kind') == 'open':
                        self.core.emit('pet_open', session=self.session)
        except (OSError, ValueError, RuntimeError):
            pass
        finally:
            if not self.closed:
                self.bridge.stop()
                self.core.emit('pet_error', message='桌宠渲染进程已退出。请检查 Electron 路径，或在设置中重新启动；小屋聊天仍可使用。')

    def _respond(self, request):
        if not isinstance(request, dict):
            return
        try:
            callback = callback_url(request.get('cb', ''))
            response = {'id':request['id'], 'status':200, 'contentType':'application/json'}
            try:
                value = self.bridge.handle(request.get('method'), request.get('url', ''), request.get('body'))
                if isinstance(value, dict) and 'file' in value:
                    response.update(value)
                else:
                    response['body'] = json.dumps(value, ensure_ascii=False)
            except (ValueError, TypeError, OSError):
                response.update(status=400, body=json.dumps({'ok':False,'reason':'bad-request','message':'桌宠请求未完成，请检查输入或在小屋重试'}, ensure_ascii=False))
            self._post(callback, response)
        except (ValueError, KeyError, TypeError, OSError):
            pass

    def _post(self, url, data, headers=None):
        request = urllib.request.Request(url, data=json.dumps(data, ensure_ascii=False).encode(), headers={'Content-Type':'application/json', **(headers or {})}, method='POST')
        with self.opener.open(request, timeout=3) as result:
            result.read(128)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.bridge.stop()
        if self.process:
            if self.control_port:
                try:
                    self._post(f'http://127.0.0.1:{self.control_port}/shutdown', {}, {'Authorization':'Bearer ' + self.control_token})
                except OSError:
                    pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.process.kill()
            if self.process.stdout:
                self.process.stdout.close()
            if self.process.stderr:
                self.process.stderr.close()
        self.pool.shutdown(wait=False, cancel_futures=True)
