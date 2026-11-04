"""Immediate-memory UI demo; fake model and isolated character agreement."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale_friend.app import Desktop
from whale_friend.config import LOCAL, Settings
from whale_friend.service import Companion
from tests.test_memory_actions import ActionModel


class DemoModel(ActionModel):
    def discover(self):
        return "即时记忆验证 · 不调用 API"

    def stream(self, messages, cancel, **kwargs):
        yield "Deep！Deep！本鱼这回真的把补充写进记忆收藏夹啦～白米饭后面也有本鱼会陪你聊天、帮忙出主意的那一半！"


if __name__ == "__main__":
    settings = Settings(data_dir=str(LOCAL / "memory-ui-check" / "data"), adaptive_enabled=False, proactive_enabled=False)
    model = DemoModel()
    core = Companion(settings, model=model)
    model.store = core.store
    original = core.store.add_memory("偏好", "小肥鱼爱吃白米饭")
    original = original or next(row["id"] for row in core.store.memories() if "白米饭" in row["content"])
    model.proposal = {"action": "update", "target_id": original, "kind": "角色约定", "content": "角色约定：本鱼爱吃白米饭，也会在聊天里陪用户上班、听用户说话、帮忙出主意。"}
    session = core.store.new_session("白米饭 · 即时记忆验证")
    core.store.add_message(session, "user", "我把你爱吃白饭这件事写进记忆里了！")
    core.store.add_message(session, "assistant", "本鱼认了，不过后面也要补一句：也会在聊天里陪你上班、听你说话、帮忙出主意。")
    app = Desktop(settings, core=core)
    app.base_title = "即时记忆验证 · 隔离数据"
    app.title(app.base_title)
    app.after(1000, lambda: core.send(session, "哼，那你自己记一下吧"))
    app.mainloop()
