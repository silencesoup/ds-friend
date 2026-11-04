"""Optional pet capabilities. Neither one writes chat or memory records."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
import urllib.request
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from .config import LOCAL

VOICES = [
    {'id':'zh-CN-XiaoyiNeural', 'name':'晓伊', 'tag':'女 · 活泼', 'default':True},
    {'id':'zh-CN-XiaoxiaoNeural', 'name':'晓晓', 'tag':'女 · 通用', 'default':False},
    {'id':'zh-CN-YunxiNeural', 'name':'云希', 'tag':'男 · 年轻', 'default':False},
    {'id':'zh-CN-YunjianNeural', 'name':'云健', 'tag':'男 · 沉稳', 'default':False},
]


def query_balance(model):
    """Use only the configured model origin; never reuse its key at another host."""
    try:
        from .pet_updates import NoRedirect
        model._validate_connection()
        request = urllib.request.Request(model.base+'/user/balance',headers={'Accept':'application/json', **({'Authorization':'Bearer '+model.key} if model.key else {})})
        handlers = [NoRedirect()]
        if urlsplit(model.base).hostname in ('localhost','127.0.0.1','::1'):
            handlers.append(urllib.request.ProxyHandler({}))
        with urllib.request.build_opener(*handlers).open(request,timeout=10) as response:
            raw = json.loads(response.read(128 * 1024))
        infos = raw.get('balance_infos')
        if not isinstance(infos, list) or not infos:
            raise ValueError('missing wallets')
        wallets = []
        for info in infos:
            wallet = {'currency': info['currency']}
            if wallet['currency'] not in ('CNY', 'USD'):
                raise ValueError('unknown currency')
            for source, target in [('total_balance','total'), ('granted_balance','granted'), ('topped_up_balance','toppedUp')]:
                value = str(info[source])
                if len(value) > 64 or not Decimal(value).is_finite():
                    raise ValueError('invalid amount')
                wallet[target] = value
            wallets.append(wallet)
        return {'ok':True, 'provider':'当前模型服务', 'shape':'money', 'data':{'wallets':wallets}}
    except Exception:
        # Gateways and local inference servers often do not implement this API.
        # Do not expose upstream errors, account identifiers or credentials.
        return {'ok':False, 'reason':'fetch-error', 'message':'当前模型服务无法查询余额（本地服务可能不支持 /user/balance）。请检查模型地址与密钥。'}


class PetVoice:
    def __init__(self, settings, cache=None):
        self.settings = settings
        self.cache = Path(cache) if cache else LOCAL / 'voice-cache'
        self.lock = threading.Lock()

    def speak(self, data):
        if not self.settings.pet_voice_enabled:
            return {'ok':False, 'reason':'unsupported', 'message':'请先在设置中开启桌宠回复朗读'}
        text = data.get('text')
        voice = data.get('voice') or self.settings.pet_voice_name
        rate = data.get('rate', '+0%')
        if (not isinstance(text,str) or not 1 <= len(text.strip()) <= 500 or
            voice not in {item['id'] for item in VOICES} or not isinstance(rate,str) or
            not re.fullmatch(r'[+-]\d{1,3}%',rate) or not -50 <= int(rate[:-1]) <= 100):
            return {'ok':False,'reason':'bad-request','message':'朗读内容、音色或语速不正确（最多 500 字）'}
        if not self.lock.acquire(timeout=.2):
            return {'ok':False,'reason':'voice-busy','message':'本鱼还在朗读上一句，请稍后再试'}
        try:
            self.cache.mkdir(parents=True,exist_ok=True)
            identity = hashlib.sha256(json.dumps([text,voice,rate],ensure_ascii=False).encode()).hexdigest()
            target = self.cache / (identity + '.mp3')
            if not target.is_file() or not target.stat().st_size:
                staging = target.with_suffix('.part')
                try:
                    asyncio.run(self._synthesize(text,voice,rate,staging))
                    if not staging.stat().st_size:
                        raise ValueError('empty audio')
                    staging.replace(target)
                finally:
                    staging.unlink(missing_ok=True)
            target.touch()
            self._prune(target)
            return {'file':str(target), 'contentType':'audio/mpeg'}
        except Exception:
            return {'ok':False,'reason':'generate-error','message':'语音合成暂不可用，请检查网络与语音依赖（requirements.txt）后重试'}
        finally:
            self.lock.release()

    async def _synthesize(self,text,voice,rate,target):
        import edge_tts  # optional at runtime: failure must not prevent text chat
        await asyncio.wait_for(edge_tts.Communicate(text,voice,rate=rate).save(str(target)),timeout=25)

    def _prune(self,current):
        files = sorted(self.cache.glob('*.mp3'),key=lambda p:p.stat().st_mtime,reverse=True)
        total = 0
        for index,path in enumerate(files):
            total += path.stat().st_size
            if path != current and (index >= 100 or total > 100 * 1024 * 1024 or time.time()-path.stat().st_mtime > 7*86400):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass  # A player may still hold an old file on Windows.
