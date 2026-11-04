"""Versioned dsh-pet renderer updates, isolated from DSH profiles and user data.

Only the renderer/assets are used: npm lifecycle scripts and Cordis plugins are
never executed. A validated installation is activated with an atomic pointer.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shutil
import tarfile
import tempfile
import threading
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .config import LOCAL, Settings, atomic_write

REGISTRY = 'https://registry.npmjs.org'
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024
UPDATE_LOCK = threading.Lock()


def version_key(value):
    match = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', str(value))
    return tuple(map(int,match.groups())) if match else (-1,-1,-1)


def compatible(value):
    return version_key(value)[:2] == (0,4)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        raise ValueError('更新服务器重定向，已停止本次下载')


def download(url, limit):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'registry.npmjs.org' or parsed.username or parsed.password or parsed.port not in (None,443):
        raise ValueError('插件仅从 npm 官方仓库下载')
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(urllib.request.Request(url,headers={'Accept':'application/json','User-Agent':'whale-friend-pet-updater'}),timeout=25) as response:
            data = response.read(limit+1)
        if len(data) > limit:
            raise ValueError('插件下载超过大小上限')
        return data
    except ValueError:
        raise
    except Exception:
        raise ValueError('无法连接 npm 更新源，请检查网络后重试') from None


def unpack(data, destination):
    destination = Path(destination).resolve()
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as archive:
        members = archive.getmembers()
        if len(members) > 20000 or sum(max(0,m.size) for m in members) > MAX_EXPANDED:
            raise ValueError('插件解压超过大小上限')
        for member in members:
            name = PurePosixPath(member.name)
            if ('\\' in member.name or ':' in member.name or name.is_absolute() or '..' in name.parts or
                not name.parts or name.parts[0] != 'package' or not (member.isdir() or member.isfile())):
                raise ValueError('插件压缩包包含不安全路径或链接')
            target = destination.joinpath(*name.parts[1:])
            if not target.resolve().is_relative_to(destination):
                raise ValueError('插件路径越界')
            if member.isdir():
                target.mkdir(parents=True,exist_ok=True)
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                with archive.extractfile(member) as source, target.open('wb') as output:
                    shutil.copyfileobj(source,output)


class PetUpdates:
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory else LOCAL / 'plugins/dsh-pet'
        self.pointer = self.directory / 'active.json'

    def state(self):
        try:
            result = json.loads(self.pointer.read_text(encoding='utf-8'))
            return result if isinstance(result,dict) else {}
        except (OSError,ValueError):
            return {}

    def active(self):
        value = self.state().get('active','')
        # Active installations are managed here. An external source may only
        # occur as the rollback target and is selected via rollback explicitly.
        return Path(value) if value and Path(value).is_dir() else None

    def check(self,current=None):
        metadata = json.loads(download(REGISTRY+'/dsh-pet',8*1024*1024))
        versions = metadata.get('versions',{})
        candidates = [v for v in versions if compatible(v)]
        if not candidates:
            raise ValueError('没有与当前小屋适配器兼容的 dsh-pet 版本')
        selected = max(candidates,key=version_key)
        manifest = versions[selected]
        if manifest.get('name') != 'dsh-pet' or manifest.get('version') != selected:
            raise ValueError('更新源中的插件信息不正确')
        current_version = ''
        if current:
            current_version = json.loads((Path(current)/'package.json').read_text(encoding='utf-8')).get('version','')
        return {'version':selected, 'latest':metadata.get('dist-tags',{}).get('latest',''),
                'current':current_version, 'available':version_key(selected)>version_key(current_version), 'dist':manifest.get('dist',{})}

    def install(self,release,current,settings=None):
        if not UPDATE_LOCK.acquire(blocking=False):
            raise ValueError('另一个插件更新正在进行')
        try:
            return self._install(release,current,settings)
        finally:
            UPDATE_LOCK.release()

    def _install(self,release,current,settings):
        from .pet_support import configuration, prepare_helper
        version = release['version']
        if not compatible(version):
            raise ValueError('此版本需要先更新小屋适配器；当前支持 0.4.x')
        dist = release['dist']
        integrity = dist.get('integrity','')
        if not isinstance(integrity,str) or not integrity.startswith('sha512-'):
            raise ValueError('更新包缺少 SHA-512 完整性校验')
        data = download(dist.get('tarball',''),MAX_ARCHIVE)
        expected = base64.b64decode(integrity[7:],validate=True)
        if hashlib.sha512(data).digest() != expected:
            raise ValueError('插件下载校验失败，保留当前版本')
        self.directory.mkdir(parents=True,exist_ok=True)
        target = self.directory / ('v'+version+'-'+hashlib.sha256(data).hexdigest()[:12])
        with tempfile.TemporaryDirectory(prefix='staging-',dir=self.directory) as staging:
            root = Path(staging)/'package'
            root.mkdir()
            unpack(data,root)
            info = json.loads((root/'package.json').read_text(encoding='utf-8'))
            if info.get('name') != 'dsh-pet' or info.get('version') != version:
                raise ValueError('插件包名称或版本不符')
            if not (root/'assets/webm').is_dir() or not (root/'LICENSE').is_file():
                raise ValueError('插件缺少动画或许可文件')
            configuration(root,settings or Settings())
            # Validate the exact renderer adaptation before moving/activating.
            prepare_helper(root)
            if not target.exists():
                root.rename(target)
        prepare_helper(target)
        previous = str(Path(current).resolve()) if current else self.state().get('active','')
        state = {'active':str(target.resolve()),'previous':previous}
        if state['active'] == previous:
            state['previous'] = self.state().get('previous','')
        atomic_write(self.pointer,json.dumps(state,ensure_ascii=False,indent=2))
        return target

    def rollback(self,settings=None):
        from .pet_support import configuration, prepare_helper
        with UPDATE_LOCK:
            state = self.state()
            previous = state.get('previous','')
            if not previous or not Path(previous).is_dir():
                raise ValueError('暂无可回退的上一版本')
            configuration(Path(previous),settings or Settings())
            prepare_helper(Path(previous))
            atomic_write(self.pointer,json.dumps({'active':previous,'previous':state.get('active','')},ensure_ascii=False,indent=2))
            return Path(previous)
