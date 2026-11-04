"""Exercise the configured model with fictional facts and an isolated SQLite store."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale_friend.backup import Backups
from whale_friend.config import LOCAL, Settings, atomic_write
from whale_friend.service import Companion


def main():
    LOCAL.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="memory-hub-check-", dir=LOCAL) as directory:
        settings = Settings(data_dir=str(Path(directory) / "data"), proactive_enabled=False, adaptive_enabled=False, timeout_seconds=90)
        core = Companion(settings)
        # Keep this diagnostic deterministic: manually run its one consolidation pass.
        core.maintain = lambda **kwargs: False
        try:
            session = core.store.new_session("隔离的主题记忆验证")
            preference = core.store.add_memory("相处偏好", "测试用户赶工或加班时，喜欢先被安静陪伴，不希望被连续追问。")
            project = core.store.add_memory("事实", "测试用户的小车项目决定先选两轮底盘，摄像头预算300元，还没购买。")
            core.memory.hub.annotate(project, pinned=True)
            original = core.store.memories()
            organized = core.memory.hub.consolidate(force=True)
            if not organized:
                raise RuntimeError("实际模型没有生成有效主题卡")
            print("真实模型主题归纳：通过", flush=True)
            identity = core.send(session, "今天又要熬夜赶进度了，先陪我待一会儿吧。")
            deadline = time.monotonic() + 210
            while core.chat_pending and time.monotonic() < deadline:
                time.sleep(.1)
            messages = core.store.messages(session)
            if core.chat_pending or messages[-1]["status"] != "complete":
                raise RuntimeError("实际角色回复未完成")
            activation = core.memory.hub.activation(session)
            if not activation or not {preference, project}.issubset(activation["memory_ids"]):
                raise RuntimeError("相关记忆未实际进入模型上下文")
            if core.store.memories() != original:
                raise RuntimeError("普通闲聊意外改写了原始记忆")
            archive = core.backups.create(Path(directory) / "backups")
            restored_dir = Path(directory) / "restored"
            Backups.restore(archive, restored_dir)
            report = {"model":core.model.model, "test_data":"fictional facts; isolated SQLite; no personal memory accessed", "organized":organized, "activation":activation, "reply":messages[-1]["content"], "topics":[{"id":t["id"], "summary":t["summary"], "source_ids":t["source_ids"]} for t in core.memory.hub.topics() if t["summary"]], "original_records_unchanged":True, "backup_restore":True, "user_message_id":identity}
            atomic_write(LOCAL / "memory-hub-check.json", json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        finally:
            core.shutdown()


if __name__ == "__main__":
    main()
