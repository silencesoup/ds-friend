"""Opt-in live-model verification of social state; never touches personal records."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale_friend.backup import Backups
from whale_friend.config import LOCAL, Settings, atomic_write
from whale_friend.memory import MemoryEngine
from whale_friend.model import ModelClient
from whale_friend.presence import Presence
from whale_friend.store import Store


def main():
    LOCAL.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="presence-check-", dir=LOCAL) as directory:
        root = Path(directory)
        settings = Settings(data_dir=str(root / "data"), backup_dir=str(root / "backups"))
        store = Store(Path(settings.data_dir))
        recovered = None
        try:
            model = ModelClient(settings)
            memory = MemoryEngine(store, model, settings)
            presence = Presence(store, model, settings)
            memory.runtime_context = presence.context
            session = store.new_session("隔离的互动测试")
            question = "这是隔离的开发测试：我最近想给小车安装摄像头，选型还没定。以后和我聊天请少在每条回复最后问问题，平时回复简短一点。小肥鱼，记住这个偏好哦。"
            earlier = (datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
            user_id = store.add_message(session, "user", question, timestamp=earlier)
            reply = model.complete(memory.build_context(session, question, threading.Event()), max_tokens=700)
            store.add_message(session, "assistant", reply, timestamp=earlier)
            if not presence.reflect(user_id, threading.Event()):
                raise RuntimeError("互动观察未保存")
            preferences = [row["content"] for row in store.memories() if row["kind"] == "相处偏好"]
            if not any("追问" in content for content in preferences):
                raise RuntimeError("真实模型未提取明确的相处偏好")
            candidate = presence.plan(store.latest_user())
            active_reply = presence.compose(memory, store.latest_user(), threading.Event())
            if active_reply:
                store.add_proactive(store.latest_user(), active_reply, candidate.isoformat(timespec="seconds"))
            archive = Backups(store, settings).create()
            recovered_path = root / "recovered"
            Backups.restore(archive, recovered_path)
            recovered = Store(recovered_path)
            if recovered.meta("companion_state") != store.meta("companion_state"):
                raise RuntimeError("互动状态恢复未通过")
            if recovered.meta("initiative_plan") != store.meta("initiative_plan"):
                raise RuntimeError("主动时机恢复未通过")
            minutes = (candidate - datetime.fromisoformat(store.latest_user()["created_at"])).total_seconds() / 60
            report = {"model": model.discover(), "reply": reply, "state": presence.mood(), "preferences": preferences,
                      "candidate_wait_minutes": round(minutes, 1), "proactive_reply": active_reply or "SKIP（选择安静）", "backup_restore": "passed", "test_data": "isolated"}
            atomic_write(LOCAL / "presence-check.json", json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            if recovered:
                recovered.close()
            store.close()


if __name__ == "__main__":
    main()
