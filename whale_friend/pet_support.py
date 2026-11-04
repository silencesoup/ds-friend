"""Locate the installed dsh-pet and prepare an isolated, attributed renderer."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from .config import LOCAL, ROOT, atomic_write


def jsonc(text):
    output, i, quoted = [], 0, False
    while i < len(text):
        char = text[i]
        if quoted:
            output.append(char)
            if char == '\\' and i + 1 < len(text):
                i += 1
                output.append(text[i])
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
            output.append(char)
        elif text[i:i+2] == '//':
            output.append(' ')
            while i < len(text) and text[i] not in '\r\n':
                i += 1
            continue
        elif text[i:i+2] == '/*':
            end = text.find('*/', i+2)
            if end < 0:
                raise ValueError('桌宠配置的注释没有结束')
            output.append(' ')
            i = end+2
            continue
        else:
            output.append(char)
        i += 1
    return json.loads(''.join(output))


def plugin_candidates():
    from .pet_updates import PetUpdates, version_key
    active = PetUpdates().active()
    if active:
        yield active
    yield ROOT / 'vendor/dsh-pet/upstream'
    home = Path.home()
    bases = [home / '.dsh', Path(os.environ.get('APPDATA', home / 'AppData/Roaming')) / 'dsh-desktop/harness',
             home / 'Library/Application Support/dsh-desktop/harness', home / '.config/dsh-desktop/harness']
    if os.environ.get('DSH_HOME'):
        bases.insert(0, Path(os.environ['DSH_HOME']))
    for base in bases:
        yield base / 'profiles/node_modules/dsh-pet'
        generations = base / 'profiles/.generations/live'
        if generations.is_dir():
            for item in sorted(generations.glob('dsh-pet+*/node_modules/dsh-pet'), key=lambda p:version_key(p.parent.parent.name.split('+')[1]), reverse=True):
                yield item


def find_plugin(configured=''):
    choices = [Path(configured).expanduser()] if configured.strip() else plugin_candidates()
    for root in choices:
        if (root / 'package.json').is_file():
            try:
                info = json.loads((root / 'package.json').read_text(encoding='utf-8'))
                if info.get('name') == 'dsh-pet' and (root / 'runtime/electron-helper/main.js').is_file():
                    return root.resolve()
            except (OSError, ValueError):
                continue
    raise ValueError('没有找到 dsh-pet。请选择已安装插件中包含 package.json、assets 和 runtime 的目录。')


def find_electron(root, configured=''):
    if configured.strip():
        candidates = [Path(configured).expanduser()]
    else:
        suffix = 'electron.exe' if sys.platform == 'win32' else 'Electron.app/Contents/MacOS/Electron' if sys.platform == 'darwin' else 'electron'
        candidates = []
        for parent in [root] + list(root.parents)[:8]:
            candidates.extend([parent / 'electron' / suffix, parent / 'node_modules/electron/dist' / suffix])
        # Managed renderer packages do not contain Electron. Continue to reuse
        # the user's installed DSH runtime after switching to a managed version.
        for installed in plugin_candidates():
            for parent in list(installed.parents)[:8]:
                candidates.extend([parent / 'electron' / suffix, parent / 'node_modules/electron/dist' / suffix])
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError('没有找到 Electron 运行程序。请在桌宠设置中选择本机已有的 Electron 可执行文件。')


def configuration(root, settings):
    info = json.loads((root / 'package.json').read_text(encoding='utf-8'))
    if not str(info.get('version', '')).startswith('0.4.'):
        raise ValueError('当前适配 dsh-pet 0.4.x；所选版本需要先检查接口兼容性。')
    config = jsonc((root / 'assets/config.jsonc').read_text(encoding='utf-8-sig'))
    if not isinstance(config.get('animations'), dict) or not config.get('pets'):
        raise ValueError('桌宠缺少动画或角色配置，请检查插件安装是否完整。')
    pet = dict(config['pets'][0])
    pet.update(id='main', name=settings.character_name, size=settings.pet_size, display='desktop',
               balanceEnabled=False, whisperEnabled=False, workStatusEnabled=True, fixedEnabled=settings.pet_fixed)
    config.update(pets=[pet], trayEnabled=False, voiceEnabled=settings.pet_voice_enabled,
                  voiceName=settings.pet_voice_name, voiceVolume=1, notificationsEnabled=False,
                  chatImageEnabled=False, whisperImageEnabled=False, memes={})
    return {'main': config}


def prepare_helper(root):
    source = root / 'runtime/electron-helper'
    files = sorted(path for path in source.rglob('*') if path.is_file())
    adapter = Path(__file__).read_bytes() + (Path(__file__).parent / 'pet_bootstrap.cjs').read_bytes()
    identity = hashlib.sha256(adapter + str(root).encode() + '\n'.join(f'{p.relative_to(source)}:{p.stat().st_size}:{p.stat().st_mtime_ns}' for p in files).encode()).hexdigest()[:16]
    target = LOCAL / 'dsh-pet-runtime' / identity
    if (target / 'ready').is_file():
        return target
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target / 'helper', dirs_exist_ok=True)
    shutil.copyfile(root / 'LICENSE', target / 'LICENSE.dsh-pet')
    main = target / 'helper/main.js'
    code = main.read_text(encoding='utf-8')
    if "app.setName('dsh-pet-electron-helper');" not in code:
        raise ValueError('当前 dsh-pet 的启动协议有变化，请使用已验证的 0.4.1 版本或更新适配器。')
    code = code.replace("app.setName('dsh-pet-electron-helper');", "app.setName('whale-friend-dsh-pet');\nif (process.env.DS_FRIEND_PET_PROFILE) app.setPath('userData', path.join(process.env.DS_FRIEND_PET_PROFILE, process.env.DSH_PET_DPI_PROBE === '1' ? 'dpi-probe' : 'renderer'));" )
    code = code.replace('com.dsh.pet.helper', 'com.whale.friend.pet')
    atomic_write(main, code)
    page = target / 'helper/index.html'
    html = page.read_text(encoding='utf-8').replace('dsh-pet 桌宠（桌面模式）', '小肥鱼 · 小屋桌宠')
    html = html.replace('</head>', '<style>button[aria-label="按住说话"]{display:none!important}</style></head>')
    atomic_write(page, html)
    # Keep the original animations and interactions; remove host-specific management.
    menu_file = target / 'helper/sprite/menu.js'
    menu = menu_file.read_text(encoding='utf-8')
    menu = menu.replace("{ label: '待办事项', action: 'todo', hint: S.TODO_HOTKEY_HINT },", '')
    menu = menu.replace('S.displayMenuBranch(),', "{ label: '隐藏桌宠', action: 'ds-friend-hide' },")
    menu = menu.replace("desktop: '打开软件'", "desktop: '打开小屋'")
    menu = menu.replace("if (leaf.action === 'open-site') {", "if (leaf.action === 'ds-friend-hide') { S.postAction(BASE + '/hide', {}); return; }\n    if (leaf.action === 'open-site') {")
    atomic_write(menu_file, menu)
    bridge = target / 'helper/main/bridge.js'
    atomic_write(bridge, bridge.read_text(encoding='utf-8').replace('}, 45000)', "}, Number(process.env.DS_FRIEND_PET_TIMEOUT_MS || 45000))"))
    atomic_write(target / 'bootstrap.cjs', (Path(__file__).parent / 'pet_bootstrap.cjs').read_text(encoding='utf-8'))
    atomic_write(target / 'NOTICE.txt', 'dsh-pet 0.4.x desktop renderer, copyright (c) 2026 PC2005-cloud. Code: MIT, see LICENSE.dsh-pet.\nSource: https://github.com/PC2005-cloud/dsh-pet\nAnimation assets are read from the installed plugin, not copied here. Upstream assets are non-commercial and require attribution for public derivatives.\nAdaptations: isolated profile, host controls and shared whale-friend memory.\n')
    atomic_write(target / 'ready', identity)
    return target
