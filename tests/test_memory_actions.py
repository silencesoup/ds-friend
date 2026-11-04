from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from whale_friend.backup import Backups
from whale_friend.config import Settings
from whale_friend.memory_actions import receipt_label
from whale_friend.service import Companion
from whale_friend.store import Store
from tests.test_core import FakeModel


class ActionModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.proposal = {"action": "add", "kind": "偏好", "content": "用户喜欢海盐拿铁"}
        self.plans = []
        self.contexts = []
        self.visible_memories = []
        self.store = None
        self.cancel_plan = False

    def complete(self, messages, **kwargs):
        if "即时记忆执行器" in messages[0]["content"]:
            self.plans.append(messages)
            if self.cancel_plan:
                kwargs["cancel"].set()
            return self.proposal if isinstance(self.proposal, str) else json.dumps(self.proposal, ensure_ascii=False)
        return super().complete(messages, **kwargs)

    def stream(self, messages, cancel, **kwargs):
        self.contexts.append(messages)
        self.visible_memories.append(self.store.memories())
        yield "本鱼按实际执行结果来回答。"


class MemoryActionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = Settings(data_dir=str(self.root / "data"), proactive_enabled=False, adaptive_enabled=False)
        self.model = ActionModel()
        self.core = Companion(self.settings, model=self.model)
        self.model.store = self.core.store
        self.store = self.core.store
        self.session = self.store.new_session()

    def tearDown(self):
        self.core.shutdown()
        self.temporary.cleanup()

    def send(self, text):
        identity = self.core.send(self.session, text)
        self.core.tasks.join()
        return identity

    def test_explicit_request_is_saved_before_model_reply_and_has_receipt(self):
        user = self.send("帮我记住，我喜欢海盐拿铁")
        self.assertEqual(self.model.visible_memories[0][0]["content"], "用户喜欢海盐拿铁")
        memory = self.store.memories()[0]
        self.assertEqual(json.loads(memory["source_ids"]), [user])
        self.assertIn('"status": "saved"', self.model.contexts[0][0]["content"])
        receipt = self.store.message_sources([user])[0]["memory_action"]
        self.assertEqual(receipt["memory_id"], memory["id"])
        self.assertIn("已写入", receipt_label(receipt))
        self.assertTrue(any(e["kind"] == "memory_action_done" for e in list(self.core.events.queue)))
        exported = json.loads((self.root / "data" / "memories.json").read_text(encoding="utf-8"))
        self.assertEqual(exported[0]["id"], memory["id"])

    def test_reference_to_previous_character_proposal_updates_the_actual_record(self):
        original = self.store.add_memory("偏好", "小肥鱼爱吃白米饭")
        self.store.add_message(self.session, "user", "我把你爱吃白饭这件事写进记忆里了")
        self.store.add_message(self.session, "assistant", "后面补一句：本鱼也会在聊天里陪你上班、听你说话、帮忙出主意。")
        self.model.proposal = {"action": "update", "target_id": original, "kind": "角色约定", "content": "角色约定：本鱼爱吃白米饭，也会在聊天里陪用户上班、听用户说话、帮忙出主意。"}
        self.send("哼，那你自己记一下吧")
        memories = self.store.memories()
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["id"], original)
        self.assertEqual(memories[0]["kind"], "角色约定")
        self.assertIn("帮忙出主意", memories[0]["content"])
        self.assertIn("本次用户请求", self.model.plans[0][1]["content"])
        self.assertIn("小肥鱼爱吃白米饭", self.model.plans[0][1]["content"])

    def test_try_again_continues_the_previous_memory_task_without_keywords(self):
        original = self.store.add_memory("事实", "小肥鱼爱吃白米饭")
        self.store.add_message(self.session, "user", "那你自己把这条补进记忆吧")
        self.store.add_message(self.session, "assistant", "本鱼想把爱吃白饭、也会陪你聊天和帮忙出主意的补充记进去，但刚才没能写入。")
        self.model.proposal = {"action": "update", "kind": "角色约定", "target_id": original, "content": "角色约定：本鱼爱吃白米饭，也会陪用户聊天、帮忙出主意。"}
        user = self.send("本人给你加上接口啦！你再试试！")
        self.assertTrue(self.model.plans)
        self.assertEqual(self.core.memory_actions.existing(user)["status"], "saved")
        self.assertIn("帮忙出主意", self.store.memories()[0]["content"])

    def test_read_action_uses_actual_collection_and_does_not_write(self):
        identity = self.store.add_memory("角色约定", "角色约定：本鱼爱吃白米饭，也会帮用户出主意。")
        self.model.proposal = {"action": "read", "query": "白米饭"}
        user = self.send("你自己看看收藏夹里那条白饭记录到底是什么")
        self.assertEqual(self.core.memory_actions.existing(user)["status"], "read")
        self.assertEqual(len(self.store.memories()), 1)
        system = self.model.contexts[0][0]["content"]
        self.assertIn('"status": "read"', system)
        self.assertIn("白米饭", system)
        self.assertIn(str(identity), system)

    def test_read_all_empty_and_no_match_are_distinguished(self):
        self.model.proposal = {"action": "read", "query": ""}
        user = self.send("你都记了哪些事？")
        self.assertEqual(self.core.memory_actions.existing(user)["status"], "read")
        self.assertIn('"memory_count": 0', self.model.contexts[-1][0]["content"])
        self.store.add_memory("偏好", "用户喜欢拿铁")
        self.model.proposal = {"action": "read", "query": "quantumxyz"}
        self.send("找找 quantumxyz 相关的记录")
        context = self.model.contexts[-1][0]["content"]
        self.assertIn('"memory_count": 1', context)
        self.assertIn('"match_count": 0', context)

    def test_read_after_retry_queries_current_record_instead_of_stale_snapshot(self):
        identity = self.store.add_memory("角色约定", "角色约定：本鱼喜欢白米饭")
        self.model.proposal = {"action": "read", "target_id": identity}
        user = self.send("你自己看看第1条")
        self.store.update_memory(identity, "角色约定", "角色约定：本鱼也会陪用户聊天")
        result = self.core.memory_actions.run(user, threading.Event())
        self.assertEqual(result["records"][0]["content"], "角色约定：本鱼也会陪用户聊天")
        receipt = self.store.rows("SELECT * FROM memory_actions WHERE user_message_id=?", (user,))[0]
        self.assertNotIn("content", receipt)

    def test_failed_query_retry_does_not_reuse_previous_successful_read(self):
        self.store.add_memory("偏好", "用户喜欢拿铁")
        self.model.proposal = {"action": "read", "query": "拿铁"}
        user = self.send("看看拿铁那条")
        self.model.proposal = "unparseable"
        result = self.core.memory_actions.run(user, threading.Event())
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("records", result)

    def test_read_request_is_never_converted_to_write_by_the_executor(self):
        self.store.add_memory("事实", "测试记录")
        self.model.proposal = {"action": "read", "query": "测试"}
        self.send("不要保存新东西，只看一下收藏夹里现有的内容")
        self.assertEqual(len(self.store.memories()), 1)
        self.assertIn('"status": "read"', self.model.contexts[-1][0]["content"])

    def test_unrelated_retry_can_select_none_without_inventing_memory(self):
        self.store.add_message(self.session, "user", "帮我画一张图")
        self.store.add_message(self.session, "assistant", "图片功能暂不可用。")
        self.model.proposal = {"action": "none", "reason": "not_requested"}
        self.send("我给你加接口了，再试试")
        self.assertFalse(self.store.memories())
        self.assertIn("若上一任务是画图", self.model.plans[0][0]["content"])

    def test_relevant_old_record_is_not_hidden_by_many_newer_facts(self):
        identity = self.store.add_memory("角色约定", "角色约定：本鱼爱吃白米饭，也会帮用户出主意")
        for index in range(30):
            self.store.add_memory("事实", f"最近的无关资料{index}：" + "普通日常小事。" * 20)
        rows = self.core.memory.memory_matches("白米饭", limit=1)
        self.assertEqual(rows[0]["id"], identity)
        self.model.proposal = {"action": "read", "query": "白米饭"}
        self.send("你看看白米饭那条")
        self.assertIn("帮用户出主意", self.model.contexts[-1][0]["content"])

    def test_write_result_is_confirmed_with_readback_before_reply(self):
        self.send("帮我记住，我喜欢海盐拿铁")
        self.assertIn('"verified_read": true', self.model.contexts[0][0]["content"])

    def test_retry_does_not_plan_or_apply_a_saved_request_twice(self):
        user = self.send("记一下我喜欢海盐拿铁")
        answer = self.store.messages(self.session)[-1]
        self.store.finish_message(answer["id"], "中途断开", "failed")
        self.core.retry(self.session)
        self.core.tasks.join()
        self.assertEqual(len(self.model.plans), 1)
        self.assertEqual(len(self.store.memories()), 1)
        self.assertEqual(len(self.store.rows("SELECT * FROM memory_actions WHERE user_message_id=?", (user,))), 1)

    def test_existing_identical_memory_is_reported_without_duplication(self):
        identity = self.store.add_memory("偏好", "用户喜欢海盐拿铁")
        user = self.send("记住我喜欢海盐拿铁")
        result = self.store.message_sources([user])[0]["memory_action"]
        self.assertEqual(result["status"], "already")
        self.assertEqual(result["memory_id"], identity)
        self.assertEqual(len(self.store.memories()), 1)

    def test_ordinary_chat_selects_no_tool_without_writing(self):
        self.model.proposal = {"action": "none", "reason": "not_requested"}
        self.send("我今天喝了杯拿铁，真开心")
        self.assertEqual(len(self.model.plans), 1)
        self.assertFalse(self.store.memories())

    def test_disabled_tools_do_not_plan_but_keep_readonly_memory_context(self):
        self.settings.memory_tools_enabled = False
        self.store.add_memory("偏好", "用户喜欢海盐拿铁")
        self.send("记住我喜欢海盐拿铁")
        self.assertFalse(self.model.plans)
        self.assertEqual(len(self.store.memories()), 1)
        self.assertIn("工具已关闭", self.model.contexts[0][0]["content"])
        self.assertIn("用户喜欢海盐拿铁", self.model.contexts[0][0]["content"])

    def test_small_context_keeps_current_request_and_execution_result(self):
        self.settings.context_tokens = 4096
        for index in range(20):
            self.store.add_memory("事实", f"无关旧事{index}：" + "过去聊过的普通事情。" * 30)
        user = self.send("帮我记住，我喜欢海盐拿铁")
        self.assertEqual(self.core.memory_actions.existing(user)["status"], "saved")
        self.assertEqual(self.store.messages(self.session)[-1]["status"], "complete")
        self.assertIn("本次用户请求", self.model.plans[0][1]["content"])
        self.assertTrue(self.model.plans[0][1]["content"].endswith("帮我记住，我喜欢海盐拿铁"))
        self.assertIn('"status": "saved"', self.model.contexts[0][0]["content"])

    def test_ambiguous_request_requires_clarification_without_inventing_fact(self):
        self.model.proposal = {"action": "none", "reason": "unclear"}
        user = self.send("把那件事记一下吧")
        receipt = self.store.message_sources([user])[0]["memory_action"]
        self.assertEqual(receipt["status"], "unclear")
        self.assertFalse(self.store.memories())
        self.assertIn('"status": "unclear"', self.model.contexts[0][0]["content"])

    def test_quoted_or_already_saved_statement_can_be_classified_without_write(self):
        self.model.proposal = {"action": "none", "reason": "not_requested"}
        self.send("这是引用的日志，不是新要求：‘记住我喜欢拿铁’。")
        self.assertFalse(self.store.memories())

    def test_negative_request_and_credentials_are_not_saved(self):
        for text, status in (("不要记住这件事", "skipped"), ("记住我的 API key 是 private-test-value", "blocked")):
            user = self.send(text)
            self.assertEqual(self.store.message_sources([user])[0]["memory_action"]["status"], status)
        self.assertEqual(len(self.model.plans), 2)
        self.assertFalse(self.store.memories())

    def test_malformed_plan_is_failed_and_normal_chat_still_works(self):
        self.model.proposal = "本鱼记好啦！"
        user = self.send("请记住我喜欢海盐拿铁")
        self.assertFalse(self.store.memories())
        self.assertEqual(self.store.message_sources([user])[0]["memory_action"]["status"], "failed")
        self.assertEqual(self.store.messages(self.session)[-1]["status"], "complete")
        self.assertIn('"status": "failed"', self.model.contexts[0][0]["content"])

    def test_failed_plan_can_be_retried_but_invalid_targets_cannot_modify_data(self):
        original = self.store.add_memory("事实", "保留的记录")
        self.model.proposal = {"action": "update", "target_id": 99999, "kind": "事实", "content": "不应写入"}
        user = self.send("修改那条记忆")
        self.assertEqual(self.store.memories()[0]["id"], original)
        self.assertEqual(self.store.memories()[0]["content"], "保留的记录")
        self.assertEqual(self.core.memory_actions.existing(user)["status"], "failed")
        self.model.proposal = {"action": "add", "kind": "偏好", "content": "用户喜欢海盐拿铁"}
        result = self.core.memory_actions.run(user, threading.Event())
        self.assertEqual(result["status"], "saved")

    def test_cancelled_planner_does_not_save_or_confirm(self):
        self.model.cancel_plan = True
        self.send("记住我喜欢海盐拿铁")
        self.assertFalse(self.store.memories())
        self.assertFalse(self.model.contexts)
        self.assertFalse(self.store.rows("SELECT * FROM memory_actions"))

    def test_memory_and_receipt_are_one_transaction(self):
        user = self.store.add_message(self.session, "user", "记住我喜欢海盐拿铁")
        with patch.object(self.core.memory_actions, "_record", side_effect=RuntimeError("receipt write failed")):
            with self.assertRaises(RuntimeError):
                self.core.memory_actions.apply(user, self.model.proposal, set(), threading.Event())
        self.assertFalse(self.store.memories())
        self.assertFalse(self.store.rows("SELECT * FROM memory_actions"))

    def test_correcting_a_fact_blocks_old_extraction_and_clears_summary(self):
        old = self.store.add_message(self.session, "user", "我喜欢拿铁")
        original = self.store.add_memory("偏好", "用户喜欢拿铁", [old])
        self.store.set_summary(self.session, "用户喜欢拿铁", old)
        self.model.proposal = {"action": "update", "kind": "偏好", "content": "用户现在喜欢茶", "target_id": original}
        self.send("更正这条记忆，我现在喜欢茶")
        self.assertIsNone(self.store.add_memory("偏好", "用户喜欢拿铁", [old]))
        self.assertEqual(self.store.summary(self.session)["content"], "")

    def test_forgetting_removes_receipt_and_does_not_reintroduce_deleted_fact(self):
        user = self.send("记住我喜欢海盐拿铁")
        self.store.forget_memory(self.store.memories()[0]["id"], erase_sources=True)
        self.assertFalse(self.store.memories())
        self.assertNotIn("memory_action", self.store.message_sources([user])[0])
        self.assertIsNone(self.core.memory_actions.run(user, threading.Event()))

    def test_deleted_record_receipt_cannot_reapply_original_request(self):
        user = self.send("记住我喜欢海盐拿铁")
        self.store.forget_memory(self.store.memories()[0]["id"], erase_sources=False)
        self.assertEqual(self.core.memory_actions.run(user, threading.Event())["status"], "removed")
        self.assertFalse(self.store.memories())

    def test_backup_restore_keeps_action_receipt_and_source(self):
        user = self.send("记住我喜欢海盐拿铁")
        archive = self.core.backups.create(self.root / "backups")
        Backups.restore(archive, self.root / "restored")
        restored = Store(self.root / "restored")
        try:
            self.assertEqual(restored.messages(self.session)[0]["memory_action"]["status"], "saved")
            self.assertEqual(json.loads(restored.memories()[0]["source_ids"]), [user])
        finally:
            restored.close()
