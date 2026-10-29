from __future__ import annotations

import ctypes
import sys
import time
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from whale_friend.app import Desktop
from whale_friend.config import Settings
from whale_friend.service import Companion
from whale_friend.tray import NIM_DELETE, NIN_KEYSELECT, NIN_SELECT, WM_TRAY, UnreadMessages, WindowsTray, show_existing
from tests.test_core import FakeModel


class UnreadTests(unittest.TestCase):
    def test_reading_current_conversation_does_not_blink(self):
        unread = UnreadMessages()
        unread.receive("current", reading=True)
        self.assertEqual(unread.count, 0)

    def test_hidden_or_other_conversations_remain_unread(self):
        unread = UnreadMessages()
        unread.receive("one")
        unread.receive("two")
        unread.receive("one")
        self.assertEqual(unread.count, 3)
        self.assertEqual(unread.latest, "one")
        unread.read("one")
        self.assertEqual(unread.count, 1)
        self.assertEqual(unread.latest, "two")

    def test_acknowledging_last_message_clears_notification(self):
        unread = UnreadMessages()
        unread.receive("one")
        unread.receive("one", reading=True)
        self.assertEqual(unread.count, 0)
        self.assertIsNone(unread.latest)


class WindowLifecycleTests(unittest.TestCase):
    def test_closing_to_tray_keeps_generation_running_and_saved(self):
        class WaitingModel(FakeModel):
            def stream(self, messages, cancel, **kwargs):
                started.set()
                release.wait(timeout=3)
                if not cancel.is_set():
                    yield "后台完成的回复"
        started, release = threading.Event(), threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=str(Path(directory) / "data"), adaptive_enabled=False, proactive_enabled=False)
            core = Companion(settings, model=WaitingModel())
            try:
                session = core.store.new_session()
                core.send(session, "请在后台完成回复")
                self.assertTrue(started.wait(timeout=2))
                state = []
                window = SimpleNamespace(core=core, tray=SimpleNamespace(available=True), hidden_to_tray=False, withdraw=lambda: state.append("hidden"))
                Desktop._close(window)
                self.assertEqual(state, ["hidden"])
                self.assertTrue(window.hidden_to_tray)
                self.assertFalse(core.stop.is_set())
                self.assertFalse(core.cancel.is_set())
                release.set()
                core.tasks.join()
                self.assertEqual(core.store.messages(session)[-1]["content"], "后台完成的回复")
                self.assertEqual(core.store.messages(session)[-1]["status"], "complete")
            finally:
                release.set()
                core.shutdown()

    def test_unavailable_tray_minimizes_instead_of_losing_the_window(self):
        actions = []
        window = SimpleNamespace(tray=None, iconify=lambda: actions.append("minimized"))
        Desktop._close(window)
        self.assertEqual(actions, ["minimized"])


@unittest.skipUnless(sys.platform == "win32", "Windows notification-area integration")
class NativeTrayTests(unittest.TestCase):
    def setUp(self):
        self.window_name = "WhaleFriend.Tray.Test." + uuid.uuid4().hex
        self.tray = WindowsTray("开发验证", instance_window=self.window_name)
        self.assertTrue(self.tray.start(), self.tray.error)

    def tearDown(self):
        self.tray.close()
        self.assertFalse(self.tray.thread.is_alive())

    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Native tray event did not complete")

    def test_unread_blinks_and_read_returns_to_normal_icon(self):
        self.assertTrue(self.tray.available)
        normal = self.tray.nid.hIcon
        self.tray.set_unread(2)
        self.wait_for(lambda: self.tray.unread == 2 and self.tray.blink_count > 0)
        self.assertIn("2 条未读", self.tray.nid.szTip)
        self.assertNotEqual(self.tray.nid.hIcon, normal)
        self.tray.set_unread(0)
        self.wait_for(lambda: self.tray.unread == 0)
        self.assertEqual(self.tray.nid.hIcon, normal)

    def test_click_and_keyboard_selection_queue_show_action(self):
        for event in (NIN_SELECT, NIN_KEYSELECT):
            self.tray.api.user.PostMessageW(self.tray.hwnd, WM_TRAY, 0, event)
            self.assertEqual(self.tray.actions.get(timeout=2), "show")

    def test_repeat_launch_can_request_existing_window(self):
        self.assertTrue(show_existing(self.window_name))
        self.assertEqual(self.tray.actions.get(timeout=2), "show")

    def test_icon_is_recreated_after_taskbar_restarts(self):
        self.tray.api.shell.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.tray.nid))
        self.tray.added = False
        self.tray.api.user.PostMessageW(self.tray.hwnd, self.tray.taskbar_created, 0, 0)
        self.wait_for(lambda: self.tray.added)
        self.assertTrue(self.tray.available)


if __name__ == "__main__":
    unittest.main()
