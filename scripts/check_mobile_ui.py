"""Phone + native desktop QA: isolated data, mock model, local-only test gateway."""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale_friend.app import Desktop
from whale_friend.config import LOCAL, Settings, atomic_write
from whale_friend.mobile import MobileServer, Pairing
from whale_friend.service import Companion
from tests.test_core import FakeModel
from check_images_ui import fixtures

ROOT = LOCAL / "mobile-ui-check"


class PhoneDemo(Desktop):
    def _start_mobile(self):
        self.mobile_server = MobileServer(self.core, port=0, host="127.0.0.1", pairing=Pairing(ROOT / "pairing.json")).start()
        self.mobile_code.set(self.mobile_server.pairing.code)
        self.mobile_detail.configure(text="隔离手机验证：" + self.mobile_server.urls()[-1])
        atomic_write(ROOT / "connection.json", json.dumps({"url": f"http://127.0.0.1:{self.mobile_server.port}", "code": self.mobile_server.pairing.code}))


if __name__ == "__main__":
    settings = Settings(data_dir=str(ROOT / "data"), mobile_enabled=True, proactive_enabled=False, adaptive_enabled=False)
    core = Companion(settings, model=FakeModel())
    if not core.store.sessions():
        session = core.store.new_session("电脑上的拿铁话题")
        core.store.add_message(session, "user", "本鱼，我喜欢海盐拿铁。手机里也要记得呀。")
        core.store.add_message(session, "assistant", "Deep！Deep！本鱼记住啦，换个小窗也能接着聊。")
        core.store.add_memory("偏好", "用户喜欢海盐拿铁")
    asset = core.assets.import_bytes(fixtures()[1], original_name="测试点头.gif")
    if not core.store.rows("SELECT 1 FROM memories WHERE content='测试用户加班时喜欢安静陪伴，少追问。'"):
        core.store.add_memory("相处偏好", "测试用户加班时喜欢安静陪伴，少追问。")
    project = core.store.add_memory("事实", "测试小车：先做两轮底盘，摄像头预算300元。")
    if project is None:
        project = core.store.rows("SELECT id FROM memories WHERE content='测试小车：先做两轮底盘，摄像头预算300元。'")[0]["id"]
    core.memory.hub.annotate(project, pinned=True)
    core.memory.hub.consolidate(force=True)
    session = core.store.sessions()[0]["id"]
    core.memory.build_context(session, "今天又要熬夜赶进度", threading.Event())
    core.assets.update(asset["id"], "点头小鱼", "蓝色小鱼点头，表示开心答应。", "点头，开心", True)
    app = PhoneDemo(settings, core=core)
    app.base_title = "分层记忆互通验证 · 隔离数据"
    app.title(app.base_title)
    app.mainloop()
