"""Real installed dsh-pet renderer, isolated SQLite and a mock model."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.test_core import FakeModel
from whale_friend.app import Desktop
from whale_friend.config import LOCAL, Settings, atomic_write
from whale_friend.service import Companion


class DemoModel(FakeModel):
    def stream(self, messages, cancel, **kwargs):
        self.calls.append(messages)
        yield 'Deep！Deep！本鱼游到桌面上来啦，和小屋共用同一份记忆。'


def main():
    folder = LOCAL / 'pet-ui-check'
    settings = Settings(data_dir=str(folder/'data'), proactive_enabled=False, adaptive_enabled=False, pet_fixed=True, pet_voice_enabled=True)
    # Synthetic account fixture: UI testing never spends tokens or displays the
    # user's real account amounts. Real network checks have a separate script.
    import whale_friend.pet_bridge as bridge_module
    bridge_module.query_balance = lambda model:{'ok':True,'provider':'隔离测试账户','shape':'money','data':{'wallets':[{'currency':'CNY','total':'12.34','granted':'2.00','toppedUp':'10.34'}]}}
    core = Companion(settings, model=DemoModel())
    core.maintain = lambda **kwargs: False
    if not core.store.sessions():
        core.store.new_session('桌宠 · 隔离验证')
    app = Desktop(settings, core=core)
    app.base_title = '桌宠集成验证 · 隔离数据'
    app.title(app.base_title)
    def launch():
        app._start_pet()
        def record():
            runtime = app.pet_runtime
            atomic_write(folder/'runtime.json',json.dumps({'pid':runtime.process.pid if runtime and runtime.process else None, 'exit':runtime.process.poll() if runtime and runtime.process else None, 'control_ready':bool(runtime and runtime.control_port), 'errors':list(runtime.stderr_lines) if runtime else [], 'session':app.session},ensure_ascii=False,indent=2))
            if not app.exiting:
                app.after(3000, record)
        record()
    app.after(700,launch)
    app.after(300000,app._exit_app)
    app.mainloop()


if __name__=='__main__':
    main()
