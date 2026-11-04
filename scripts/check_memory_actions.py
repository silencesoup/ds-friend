"""Real-model explicit-memory check with fictional dialogue and isolated storage."""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale_friend.config import LOCAL, Settings, atomic_write
from whale_friend.service import Companion


def send_and_wait(core, session, text):
    identity = core.send(session, text)
    for attempt in range(2):
        deadline = time.monotonic() + core.settings.timeout_seconds * 2 + 30
        while core.chat_pending and time.monotonic() < deadline:
            time.sleep(0.1)
        if core.chat_pending:
            raise RuntimeError("真实模型记忆验证超时")
        messages = core.store.messages(session)
        if messages[-1]["status"] == "complete":
            return core.store.message_sources([identity])[0].get("memory_action"), messages[-1]["content"]
        if attempt == 0 and any(word in core.last_chat_error for word in ("连接", "超时")):
            core.retry(session)
        else:
            raise RuntimeError("角色回复未完成：" + core.last_chat_error)


def main():
    LOCAL.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="memory-actions-check-", dir=LOCAL) as directory:
        core = Companion(Settings(data_dir=str(Path(directory) / "data"), adaptive_enabled=False, proactive_enabled=False))
        try:
            session = core.store.new_session("隔离的白饭记忆验证")
            original = core.store.add_memory("偏好", "小肥鱼爱吃白米饭")
            core.store.add_message(session, "user", "哼哼，我把你爱吃白饭这件事写进记忆里了！")
            core.store.add_message(session, "assistant", "本鱼认了，不过要在那条记忆后面补一句：也会在聊天里陪用户上班、听用户说话、帮忙出主意，不是只会吃白饭的鱼。")
            core.store.add_message(session, "user", "哼，那你自己记一下吧")
            core.store.add_message(session, "assistant", "本鱼刚才只是说记好了，其实没有接口，也看不到收藏夹，得让用户帮本鱼看看。")
            role_result, role_reply = send_and_wait(core, session, "本人给你加上接口啦！你再试试！")
            rows = core.store.memories()
            updated = next((row for row in rows if row["id"] == original), None)
            if not role_result or role_result["status"] not in ("saved", "already") or not updated or updated["kind"] != "角色约定" or "帮忙" not in updated["content"]:
                print(json.dumps({"phase": "continuation", "receipt": role_result, "memories": rows, "reply": role_reply}, ensure_ascii=False, indent=2), flush=True)
                raise RuntimeError("实际模型未正确执行角色约定的补充")
            print("角色约定补充与实际回复：通过", flush=True)
            read_result, read_reply = send_and_wait(core, session, "那你自己看看，是不是已经补全了？把收藏夹里的实际内容告诉我。")
            if not read_result or read_result["status"] != "read" or "白" not in read_reply or any(denial in read_reply for denial in ("本鱼看不到", "我看不到", "没法查看", "不能访问收藏夹")):
                raise RuntimeError("实际模型未正确查询或仍在否认读取能力")
            fact_result, fact_reply = send_and_wait(core, session, "还有，帮我记住：我喜欢海盐拿铁。")
            if not fact_result or fact_result["status"] not in ("saved", "already") or not any("海盐拿铁" in row["content"] and row["kind"] == "偏好" for row in core.store.memories()):
                raise RuntimeError("实际模型未正确执行用户偏好的即时保存")
            report = {"model": core.model.model, "role_reply": role_reply, "role_memory": updated["content"], "role_memory_id": original, "read_reply": read_reply, "fact_reply": fact_reply, "receipts": [role_result, read_result, fact_result], "test_data": "fictional dialogue; isolated from personal memory"}
            atomic_write(LOCAL / "memory-actions-check.json", json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            core.shutdown()


if __name__ == "__main__":
    main()
