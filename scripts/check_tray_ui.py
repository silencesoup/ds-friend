"""Interactive tray QA: isolated data and a simulated model; no API requests.

Close the window within 20 seconds. A test reply then arrives in the tray.
Use the tray or launch the same application again to inspect the unread reply.
Exit via the 小屋 menu or the tray menu when finished.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale_friend.app import Desktop
from whale_friend.config import LOCAL, Settings
from whale_friend.service import Companion


class DemoModel:
    def discover(self):
        return "托盘验证 · 不调用 API"

    def stream(self, messages, cancel, **kwargs):
        if not cancel.is_set():
            yield "本鱼的新来信到啦～这是隔离的托盘验证消息，打开小屋查看后，闪烁就会停止。"

    def complete(self, messages, **kwargs):
        return '{"title":"托盘验证","summary":"独立的通知测试，不属于用户日常对话。","facts":[]}'


settings = Settings(data_dir=str(LOCAL / "tray-check" / "data"), proactive_enabled=False, adaptive_enabled=False)
core = Companion(settings, model=DemoModel())
app = Desktop(settings, core=core)
app.base_title = "托盘验证 · 小肥鱼的海边小屋"
app.title(app.base_title)
app.status.configure(text="请在 20 秒内点关闭隐藏窗口，随后会收到一条测试回复。")
app.after(20_000, lambda: core.send(app.session, "[隔离开发验证] 发一条托盘测试回复。"))
app.mainloop()
