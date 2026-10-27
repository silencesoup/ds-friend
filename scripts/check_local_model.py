"""Explicit opt-in integration check. Uses .env, isolates all test memories."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale_friend.backup import Backups
from whale_friend.config import LOCAL, Settings, atomic_write
from whale_friend.memory import MemoryEngine
from whale_friend.model import ModelClient
from whale_friend.store import Store


def main():
    LOCAL.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="integration-", dir=LOCAL) as directory:
        root = Path(directory)
        settings = Settings(data_dir=str(root / "working"), backup_dir=str(root / "backup"))
        model = ModelClient(settings)
        store = Store(Path(settings.data_dir))
        recovered = None
        try:
            memory = MemoryEngine(store, model, settings)
            session = store.new_session("隔离的开发验证")
            user = "这是一个隔离的开发测试：我希望你记住，我喜欢海盐拿铁，最近的长期目标是给小车安装摄像头。小肥鱼，你平时怎样称呼自己？"
            store.add_message(session, "user", user)
            context = memory.build_context(session, user, threading.Event())
            answer = model.complete(context, max_tokens=400)
            store.add_message(session, "assistant", answer)
            count = memory.journal_due(force=True)
            if count != 1 or not store.memories():
                raise RuntimeError("真实模型日志或长期记忆提取未通过")
            next_session = store.new_session("跨会话回忆验证")
            question = "之前的开发测试里，我提到喜欢什么饮品，以及给小车做什么？"
            store.add_message(next_session, "user", question)
            recalled = model.complete(memory.build_context(next_session, question, threading.Event()), max_tokens=350)
            if "拿铁" not in recalled or "摄像头" not in recalled:
                raise RuntimeError("真实模型跨会话回忆验证未通过")
            archive = Backups(store, settings).create()
            Backups.restore(archive, root / "recovered")
            recovered = Store(root / "recovered")
            if len(recovered.memories()) != len(store.memories()) or len(recovered.journals()) != 1:
                raise RuntimeError("真实模型数据备份恢复未通过")
            report = {"model": model.model, "character_reply": answer, "recall_reply": recalled, "journals": count, "memories": len(store.memories()), "backup_restore": "passed", "test_data": "isolated; not written to personal conversations"}
            atomic_write(LOCAL / "model-check.json", json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            if recovered:
                recovered.close()
            store.close()


if __name__ == "__main__":
    main()
