from __future__ import annotations

import json
import re
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from whale_friend.backup import Backups
from whale_friend.config import Settings
from whale_friend.memory import MemoryEngine
from whale_friend.model import Cancelled
from whale_friend.presence import PREFERENCES, Presence
from whale_friend.service import Companion
from whale_friend.store import Store
from tests.test_core import FakeModel


class SocialModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.preferences = []
        self.initiative = "high"
        self.busy_minutes = 0
        self.proactive_calls = 0
        self.proactive_answer = "上次聊到的小车摄像头，本鱼想到可以先列一下你想要的功能～"
        self.entered = None
        self.release = None

    def complete(self, messages, **kwargs):
        system = messages[0]["content"]
        if "互动观察器" in system:
            user_id = int(re.search(r"本次用户消息编号：(\d+)", messages[1]["content"]).group(1))
            data = {"mood": "好奇", "intensity": 0.65, "reason": "一起讨论小车摄像头", "initiative": self.initiative, "busy_minutes": self.busy_minutes,
                    "preferences": [{"slot": slot, "value": value, "source_ids": [user_id]} for slot, value in self.preferences]}
            return json.dumps(data, ensure_ascii=False)
        if "现在由应用触发一次主动来信" in system:
            self.proactive_calls += 1
            if self.entered is not None:
                self.entered.set()
                self.release.wait(timeout=3)
                if kwargs["cancel"].is_set():
                    raise Cancelled("new user input")
            if self.fail:
                raise RuntimeError("模拟模型离线")
            return self.proactive_answer
        return super().complete(messages, **kwargs)


class PresenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = Settings(data_dir=str(self.root / "data"), backup_dir=str(self.root / "backups"), proactive_min_minutes=15, proactive_max_minutes=120)
        self.store = Store(Path(self.settings.data_dir))
        self.session = self.store.new_session()
        self.model = SocialModel()
        self.presence = Presence(self.store, self.model, self.settings)
        self.at = datetime.fromisoformat("2026-10-28T14:00:00+08:00")
        self.core = None

    def tearDown(self):
        if self.core:
            self.core.stop.set()
            self.core.worker.join(timeout=3)
            self.assertFalse(self.core.worker.is_alive())
        self.store.close()
        self.temporary.cleanup()

    def exchange(self, text="我们还没选好小车摄像头，之后再一起看看。", at=None):
        stamp = (at or self.at - timedelta(hours=3)).isoformat()
        identity = self.store.add_message(self.session, "user", text, timestamp=stamp)
        self.store.add_message(self.session, "assistant", "本鱼先陪你列一下需求。", timestamp=stamp)
        return identity

    def companion(self):
        self.core = Companion(self.settings, self.store, self.model)
        self.core.started_at = self.at - timedelta(hours=4)
        return self.core

    def test_no_conversation_no_proactive(self):
        self.assertIsNone(self.presence.permits(self.at))

    def test_quiet_hours_wrap_midnight_and_can_be_disabled(self):
        self.assertTrue(self.presence.quiet(self.at.replace(hour=23)))
        self.assertTrue(self.presence.quiet(self.at.replace(hour=7)))
        self.assertFalse(self.presence.quiet(self.at.replace(hour=8)))
        self.settings.quiet_start_hour = self.settings.quiet_end_hour = 8
        self.assertFalse(self.presence.quiet(self.at.replace(hour=23)))

    def test_plan_is_variable_and_persists_instead_of_redrawing_each_tick(self):
        self.exchange(at=self.at)
        user = self.store.latest_user()
        due = self.presence.plan(user)
        minutes = (due - self.at).total_seconds() / 60
        self.assertGreaterEqual(minutes, 15)
        self.assertLessEqual(minutes, 120)
        restored_presence = Presence(self.store, self.model, self.settings)
        self.assertEqual(restored_presence.plan(user), due)
        self.assertIsNone(self.presence.permits(self.at + timedelta(minutes=10)))
        self.assertEqual(self.presence.permits(self.at + timedelta(hours=3))["id"], user["id"])

    def test_high_interest_can_choose_an_earlier_window_than_closed_topic(self):
        user_id = self.exchange(at=self.at)
        class Midpoint:
            @staticmethod
            def uniform(start, end):
                return (start + end) / 2
        self.presence.rng = Midpoint()
        state = {"mood": "好奇", "intensity": 0.6, "reason": "待继续的话题", "source_ids": [user_id], "updated_at": self.at.isoformat(), "initiative": "high"}
        self.store.set_meta("companion_state", json.dumps(state))
        high = self.presence.plan(self.store.latest_user())
        self.store.set_meta("initiative_plan", "{}")
        state["initiative"] = "low"
        self.store.set_meta("companion_state", json.dumps(state))
        low = self.presence.plan(self.store.latest_user())
        self.assertLess(high, low)

    def test_busy_until_is_respected_even_when_mood_is_calm(self):
        user_id = self.exchange(at=self.at)
        until = self.at + timedelta(hours=4)
        self.store.set_meta("companion_state", json.dumps({"mood": "平静", "intensity": 0, "reason": "用户去开会", "source_ids": [user_id], "updated_at": self.at.isoformat(), "busy_until": until.isoformat()}))
        self.assertEqual(self.presence.mood(self.at)["mood"], "平静")
        due = self.presence.plan(self.store.latest_user())
        self.assertGreater(due, until)
        self.assertIsNone(self.presence.permits(self.at + timedelta(hours=3)))

    def test_does_not_chase_unanswered_message_even_next_day(self):
        self.exchange()
        user = self.store.latest_user()
        self.store.add_proactive(user, "来看看上次的话题", self.at.isoformat())
        self.assertIsNone(self.presence.permits(self.at + timedelta(days=1)))
        with self.assertRaises(ValueError):
            self.store.add_proactive(user, "再问一遍", (self.at + timedelta(days=1)).isoformat())

    def test_daily_limit_and_cooldown_apply_to_new_turns(self):
        self.settings.proactive_daily_limit = 1
        self.exchange()
        self.store.add_proactive(self.store.latest_user(), "主动来信", self.at.isoformat())
        self.exchange(at=self.at + timedelta(minutes=1))
        self.assertIsNone(self.presence.permits(self.at + timedelta(hours=3)))
        self.settings.proactive_daily_limit = 3
        self.assertIsNone(self.presence.permits(self.at + timedelta(minutes=90)))
        self.assertIsNotNone(self.presence.permits(self.at + timedelta(hours=3)))

    def test_failed_or_unanswered_user_turn_does_not_trigger_new_message(self):
        self.exchange()
        latest = self.store.messages(self.session)[-1]
        self.store.finish_message(latest["id"], "部分回复", "failed")
        self.assertIsNone(self.presence.permits(self.at))

    def test_explicit_preferences_are_updated_with_sources(self):
        user_id = self.exchange("以后少在每条回复结尾问问题，简短一点就好。")
        self.model.preferences = [("follow_up", "少问"), ("response_length", "简短")]
        self.assertTrue(self.presence.reflect(user_id, threading.Event()))
        memories = self.store.memories()
        self.assertEqual(len(memories), 2)
        self.assertTrue(all(item["kind"] == "相处偏好" and json.loads(item["source_ids"]) == [user_id] for item in memories))
        user_id = self.exchange("以后解释得详细一些。", at=self.at)
        self.model.preferences = [("response_length", "详细")]
        self.presence.reflect(user_id, threading.Event())
        self.assertEqual(len(self.store.memories()), 2)
        self.assertIn(PREFERENCES["response_length"]["详细"], [item["content"] for item in self.store.memories()])

    def test_no_explicit_preference_does_not_create_longterm_guess(self):
        user_id = self.exchange()
        self.presence.reflect(user_id, threading.Event())
        self.assertFalse(self.store.memories())

    def test_preference_with_assistant_or_missing_source_is_rejected(self):
        user_id = self.exchange()
        assistant_id = self.store.messages(self.session)[-1]["id"]
        self.model.complete = lambda *args, **kwargs: json.dumps({"mood": "平静", "intensity": 0, "reason": "普通交流", "preferences": [
            {"slot": "follow_up", "value": "少问", "source_ids": [assistant_id]},
            {"slot": "response_length", "value": "详细", "source_ids": [99999]},
        ]})
        self.presence.reflect(user_id, threading.Event())
        self.assertFalse(self.store.memories())

    def test_quiet_preference_can_be_deleted_or_edited(self):
        user_id = self.exchange("以后别主动打扰我，等我先开口。")
        self.store.remember_preference("proactivity", "安静", PREFERENCES["proactivity"]["安静"], user_id)
        self.assertIsNone(self.presence.permits(self.at))
        memory = self.store.memories()[0]
        self.store.update_memory(memory["id"], "相处偏好", "用户现在喜欢本鱼适度主动")
        self.assertIsNotNone(self.presence.permits(self.at))

    def test_mood_decays_and_deleted_source_stops_affecting_state(self):
        user_id = self.exchange()
        self.presence.reflect(user_id, threading.Event())
        state = json.loads(self.store.meta("companion_state"))
        updated = datetime.fromisoformat(state["updated_at"])
        self.assertEqual(self.presence.mood(updated)["mood"], "好奇")
        self.assertEqual(self.presence.mood(updated + timedelta(hours=8))["mood"], "平静")
        self.store.execute("UPDATE messages SET content='[已删除]',status='erased' WHERE id=?", (user_id,))
        self.assertEqual(self.presence.mood(updated)["mood"], "平静")

    def test_companion_proactive_is_assistant_only_and_backed_up(self):
        self.exchange()
        core = self.companion()
        before = len(self.store.messages(self.session))
        self.assertTrue(core.maybe_proactive(at=self.at))
        core.tasks.join()
        messages = self.store.messages(self.session)
        self.assertEqual(len(messages), before + 1)
        self.assertEqual(messages[-1]["role"], "assistant")
        self.assertTrue(messages[-1]["proactive"])
        self.assertEqual(sum(item["role"] == "user" for item in messages), 1)
        archive = Backups(self.store, self.settings).create()
        recovered_path = self.root / "restored"
        Backups.restore(archive, recovered_path)
        recovered = Store(recovered_path)
        try:
            self.assertTrue(recovered.messages(self.session)[-1]["proactive"])
            self.assertIsNone(Presence(recovered, self.model, self.settings).permits(self.at + timedelta(days=1)))
        finally:
            recovered.close()

    def test_draft_and_startup_grace_prevent_initiative(self):
        self.exchange()
        core = self.companion()
        self.assertFalse(core.maybe_proactive(composing=True, at=self.at))
        core.started_at = self.at
        self.assertFalse(core.maybe_proactive(at=self.at + timedelta(minutes=1)))
        self.assertEqual(self.model.proactive_calls, 0)

    def test_model_can_choose_silence_without_retrying_every_minute(self):
        self.exchange()
        self.model.proactive_answer = "SKIP"
        core = self.companion()
        self.assertTrue(core.maybe_proactive(at=self.at))
        core.tasks.join()
        self.assertFalse(core.maybe_proactive(at=self.at + timedelta(hours=3)))
        self.assertEqual(self.model.proactive_calls, 1)
        self.assertEqual(len(self.store.messages(self.session)), 2)

    def test_network_failure_is_not_saved_as_a_message_and_is_backed_off(self):
        self.exchange()
        self.model.fail = True
        core = self.companion()
        self.assertTrue(core.maybe_proactive(at=self.at))
        core.tasks.join()
        self.assertEqual(len(self.store.messages(self.session)), 2)
        self.assertFalse(core.maybe_proactive(at=self.at + timedelta(minutes=1)))

    def test_new_user_input_cancels_stale_initiative(self):
        self.exchange()
        self.model.entered, self.model.release = threading.Event(), threading.Event()
        core = self.companion()
        self.assertTrue(core.maybe_proactive(at=self.at))
        self.assertTrue(self.model.entered.wait(timeout=2))
        core.send(self.session, "我回来了，先聊另一件事。")
        self.model.release.set()
        core.tasks.join()
        messages = self.store.messages(self.session)
        self.assertFalse(any(item["proactive"] for item in messages))
        self.assertEqual(sum(item["role"] == "user" for item in messages), 2)
        self.assertEqual(messages[-1]["status"], "complete")

    def test_state_and_preferences_are_available_in_next_context(self):
        user_id = self.exchange("我希望你少问一些问题。")
        self.model.preferences = [("follow_up", "少问")]
        self.presence.reflect(user_id, threading.Event())
        memory = MemoryEngine(self.store, self.model, self.settings)
        memory.runtime_context = self.presence.context
        self.store.add_message(self.session, "user", "继续聊小车吧")
        context = memory.build_context(self.session, "小车", threading.Event())
        self.assertIn("角色语气状态", context[0]["content"])
        self.assertIn("减少结尾追问", context[0]["content"])

    def test_new_settings_roundtrip_and_invalid_window_rejected(self):
        path = self.root / "settings.json"
        self.settings.proactive_enabled = False
        self.settings.save(path)
        restored = Settings.load(path)
        self.assertFalse(restored.proactive_enabled)
        self.assertEqual(restored.proactive_max_minutes, 120)
        self.settings.proactive_min_minutes = 121
        with self.assertRaises(ValueError):
            self.settings.validate()

    def test_older_backup_without_initiative_table_still_restores(self):
        self.exchange()
        self.store.execute("DROP TABLE proactive_messages")
        archive = Backups(self.store, self.settings).create()
        destination = self.root / "old-restored"
        Backups.restore(archive, destination)
        recovered = Store(destination)
        try:
            self.assertEqual(len(recovered.messages(self.session)), 2)
            self.assertFalse(recovered.messages(self.session)[-1]["proactive"])
            self.assertIsNotNone(Presence(recovered, self.model, self.settings).permits(self.at))
        finally:
            recovered.close()


if __name__ == "__main__":
    unittest.main()
