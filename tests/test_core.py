from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from whale_friend.backup import Backups
from whale_friend.config import Settings, read_env
from whale_friend.memory import MemoryEngine, fit_text, tokens
from whale_friend.model import Cancelled, ModelClient, ModelError, ThinkFilter
from whale_friend.service import Companion
from whale_friend.store import Store


class FakeModel:
    def __init__(self):
        self.calls = []
        self.malformed = False
        self.fail = False

    def complete(self, messages, **kwargs):
        self.calls.append(messages)
        if self.fail:
            raise ModelError("模拟离线")
        if "事实归档器" in messages[0]["content"]:
            if self.malformed:
                return "not valid JSON"
            import re
            sources = [int(identity) for identity in re.findall(r"消息#(\d+)[^\n]+用户", messages[1]["content"])]
            return json.dumps({"title": "小车和咖啡", "summary": "讨论小车摄像头；用户表示喜欢拿铁，尚待决定预算。", "facts": [{"kind": "偏好", "content": "用户喜欢拿铁", "source_ids": sources[:1]}] if sources else []}, ensure_ascii=False)
        return "用户正在讨论小车摄像头。待办：确认预算。"

    def discover(self):
        return "fake-local-model"

    def stream(self, messages, cancel, **kwargs):
        self.calls.append(messages)
        for chunk in ("本鱼", "记得你喜欢拿铁。"):
            if cancel.is_set():
                raise Cancelled("已停止")
            yield chunk


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = Settings(data_dir=str(self.root / "working"), backup_dir=str(self.root / "cloud"), backup_keep=2)
        self.store = Store(Path(self.settings.data_dir))
        self.model = FakeModel()
        self.memory = MemoryEngine(self.store, self.model, self.settings)
        self.session = self.store.new_session()

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def exchange(self, text="我喜欢拿铁，我们来讨论小车摄像头。", day=None):
        stamp = day + "T12:00:00+08:00" if day else None
        user = self.store.add_message(self.session, "user", text, timestamp=stamp)
        assistant = self.store.add_message(self.session, "assistant", "本鱼来陪你研究预算。", timestamp=stamp)
        return user, assistant

    def test_persistence_and_interrupted_reply(self):
        user, _ = self.exchange()
        pending = self.store.add_message(self.session, "assistant", "部分回复", "pending")
        self.store.close()
        self.store = Store(Path(self.settings.data_dir))
        messages = self.store.messages(self.session)
        self.assertEqual(messages[0]["id"], user)
        self.assertEqual(messages[-1]["id"], pending)
        self.assertEqual(messages[-1]["status"], "interrupted")

    def test_journal_is_idempotent_and_has_provenance(self):
        ids = self.exchange()
        self.assertEqual(self.memory.journal_due(force=True), 1)
        self.assertEqual(self.memory.journal_due(force=True), 0)
        journal = self.store.journals()[0]
        self.assertEqual(json.loads(journal["source_ids"]), list(ids))
        self.assertEqual(json.loads(self.store.memories()[0]["source_ids"]), [ids[0]])
        self.assertEqual(self.store.unjournaled(), [])
        self.assertTrue((self.store.directory / "journals" / "INDEX.md").exists())
        self.assertTrue(list((self.store.directory / "journals" / "index").glob("*.md")))

    def test_previous_day_gets_catchup_journal(self):
        yesterday = (datetime.now().astimezone() - timedelta(days=1)).date().isoformat()
        self.exchange(day=yesterday)
        self.assertEqual(self.memory.journal_due(), 1)
        self.assertEqual(self.store.journals()[0]["day"], yesterday)

    def test_today_waits_until_idle_or_explicit_archive(self):
        self.exchange()
        self.assertEqual(self.memory.journal_due(), 0)
        self.assertEqual(len(self.store.unjournaled()), 2)

    def test_journal_yields_between_batches_for_new_chat(self):
        self.exchange(day="2026-10-19")
        self.exchange(day="2026-10-20")
        checks = []
        def should_yield():
            checks.append(True)
            return len(checks) > 1
        self.assertEqual(self.memory.journal_due(force=True, should_yield=should_yield), 1)
        self.assertEqual(len(self.store.unjournaled()), 2)
        self.assertEqual(self.memory.journal_due(force=True), 1)

    def test_malformed_summary_never_advances_journal_watermark(self):
        self.exchange()
        self.model.malformed = True
        with self.assertRaises(ModelError):
            self.memory.journal_due(force=True)
        self.assertEqual(len(self.store.unjournaled()), 2)
        self.assertFalse(self.store.journals())
        self.model.malformed = False
        self.assertEqual(self.memory.journal_due(force=True), 1)

    def test_only_user_message_sources_can_become_facts(self):
        _, assistant = self.exchange()
        original = self.model.complete
        self.model.complete = lambda *a, **k: json.dumps({"title": "测试", "summary": "真实讨论", "facts": [{"kind": "事实", "content": "错误来源", "source_ids": [assistant]}, {"kind": "事实", "content": "不存在的来源", "source_ids": [99999]}]})
        self.memory.journal_due(force=True)
        self.assertFalse(self.store.memories())
        self.model.complete = original

    def test_edit_suppresses_old_fact_reextraction_and_clears_summary(self):
        ids = self.exchange()
        self.memory.journal_due(force=True)
        fact = self.store.memories()[0]
        self.store.set_summary(self.session, "喜欢拿铁", ids[-1])
        self.store.update_memory(fact["id"], "偏好", "用户现在少喝咖啡")
        self.assertEqual(self.store.summary(self.session)["content"], "")
        self.assertIsNone(self.store.add_memory("偏好", "用户喜欢拿铁", [ids[0]]))
        self.assertNotIn("用户表示喜欢拿铁", self.memory.retrieval("小车"))

    def test_forget_erases_exchange_and_derived_data_but_preserves_next_turn(self):
        ids = self.exchange()
        self.memory.journal_due(force=True)
        next_ids = self.exchange("下一轮讨论天气")
        fact = self.store.memories()[0]
        self.store.forget_memory(fact["id"], erase_sources=True)
        self.memory.export_journals()
        self.assertFalse(self.store.memories())
        self.assertFalse(self.store.journals())
        erased = self.store.message_sources(list(ids))
        self.assertTrue(all(item["status"] == "erased" for item in erased))
        self.assertTrue(all(item["status"] == "complete" for item in self.store.message_sources(list(next_ids))))
        self.assertEqual(len(self.store.unjournaled()), 2)
        self.assertFalse(list((self.store.directory / "journals").glob("????-??-??.md")))

    def test_chinese_theme_and_date_retrieval(self):
        self.exchange(day="2026-10-20")
        self.memory.journal_due(force=True)
        self.assertTrue(self.store.search("以前聊的小车摄像头"))
        self.assertTrue(self.store.search("2026-10-20"))
        self.assertIn("2026-10-20", self.memory.retrieval("2026年10月20日聊过什么"))
        self.assertFalse(self.store.search("quantumxyz"))

    def test_context_compacts_without_deleting_originals(self):
        self.settings.context_tokens = 4096
        for index in range(18):
            self.exchange(f"第{index}件事情：" + "我们正在讨论小车摄像头和路线规划。" * 8)
        self.store.add_message(self.session, "user", "最后的问题：预算是多少？")
        original_count = len(self.store.messages(self.session))
        context = self.memory.build_context(self.session, "预算是多少", threading.Event())
        self.assertEqual(context[-1]["content"], "最后的问题：预算是多少？")
        self.assertGreater(self.store.summary(self.session)["through_id"], 0)
        self.assertIn("待办", context[0]["content"])
        self.assertEqual(len(self.store.messages(self.session)), original_count)
        self.assertLessEqual(sum(tokens(item["content"]) + 16 for item in context), self.settings.context_tokens - self.settings.reply_tokens - 512)

    def test_oversized_current_message_does_not_silently_truncate(self):
        self.store.add_message(self.session, "user", "鲸" * 20000)
        with self.assertRaisesRegex(ValueError, "超过"):
            self.memory.build_context(self.session, "鲸", threading.Event())

    def test_fitting_preserves_unicode_and_budget(self):
        text = fit_text("鲸鱼 hello " * 1000, 100)
        self.assertLessEqual(tokens(text), 100)
        self.assertIn("截短", text)

    def test_backup_roundtrip_includes_all_memory_layers_without_credentials(self):
        self.exchange()
        self.memory.journal_due(force=True)
        self.store.set_summary(self.session, "未完成事项：预算", 1)
        archive = Backups(self.store, self.settings).create()
        manifest, _ = Backups.inspect(archive)
        self.assertEqual(manifest["counts"]["messages"], 2)
        with zipfile.ZipFile(archive) as zip_:
            self.assertIn("exports/messages.jsonl", zip_.namelist())
            self.assertIn("exports/summaries.json", zip_.namelist())
            self.assertNotIn(".env", zip_.namelist())
            self.assertNotIn("api_key", zip_.read("settings.json").decode())
        destination = self.root / "recovered"
        Backups.restore(archive, destination)
        recovered = Store(destination)
        try:
            self.assertEqual(len(recovered.messages(self.session)), 2)
            self.assertEqual(len(recovered.memories()), 1)
            self.assertEqual(recovered.summary(self.session)["content"], "未完成事项：预算")
            self.assertEqual(recovered.unjournaled(), [])
        finally:
            recovered.close()

    def test_restore_does_not_overwrite_existing_data(self):
        archive = Backups(self.store, self.settings).create()
        with self.assertRaisesRegex(ValueError, "空"):
            Backups.restore(archive, self.store.directory)

    def test_corrupt_or_incomplete_backup_is_rejected(self):
        archive = Backups(self.store, self.settings).create()
        changed = self.root / "tampered.zip"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(changed, "w") as target:
            for name in source.namelist():
                target.writestr(name, b"corrupt" if name == "companion.sqlite3" else source.read(name))
        with self.assertRaisesRegex(ValueError, "校验"):
            Backups.inspect(changed)

    def test_invalid_database_with_matching_hash_is_rejected(self):
        from whale_friend.backup import digest
        archive = Backups(self.store, self.settings).create()
        changed = self.root / "invalid-database.zip"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(changed, "w") as target:
            manifest = json.loads(source.read("manifest.json"))
            bad_database = b"this is not a sqlite database"
            manifest["files"]["companion.sqlite3"] = digest(bad_database)
            for name in source.namelist():
                if name == "manifest.json":
                    target.writestr(name, json.dumps(manifest))
                else:
                    target.writestr(name, bad_database if name == "companion.sqlite3" else source.read(name))
        with self.assertRaisesRegex(ValueError, "损坏"):
            Backups.inspect(changed)

    def test_backup_retention_and_latest_pointer(self):
        backups = Backups(self.store, self.settings)
        for _ in range(4):
            latest = backups.create()
        self.assertEqual(len(list(Path(self.settings.backup_dir).glob("whale-friend-*.zip"))), 2)
        pointer = json.loads((Path(self.settings.backup_dir) / "LATEST.json").read_text())
        self.assertEqual(pointer["file"], latest.name)

    def test_invalid_storage_overlap_rejected(self):
        self.settings.backup_dir = str(self.store.directory / "backups")
        with self.assertRaises(ValueError):
            self.settings.validate()

    def test_env_quotes_and_no_secrets_in_settings(self):
        path = self.root / ".env"
        path.write_text('# test\nexport ds_base_url="http://localhost:9999/v1"\napi_key=secret\n', encoding="utf-8")
        env = read_env(path)
        self.assertEqual(env["ds_base_url"], "http://localhost:9999/v1")
        config = self.root / "settings.json"
        self.settings.save(config)
        self.assertNotIn("secret", config.read_text())

    def test_service_streams_and_retry_does_not_duplicate_user(self):
        core = Companion(self.settings, store=self.store, model=self.model)
        core.send(self.session, "我喜欢拿铁")
        core.tasks.join()
        messages = self.store.messages(self.session)
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertEqual(messages[-1]["status"], "complete")
        self.store.finish_message(messages[-1]["id"], "部分回复", "failed")
        core.retry(self.session)
        core.tasks.join()
        self.assertEqual(sum(item["role"] == "user" for item in self.store.messages(self.session)), 1)
        core.stop.set()
        core.worker.join(timeout=2)

    def test_offline_journal_still_creates_backup(self):
        self.exchange(day="2026-10-20")
        self.model.fail = True
        core = Companion(self.settings, store=self.store, model=self.model)
        core.maintain(force=True)
        core.tasks.join()
        self.assertTrue(self.store.meta("journal_error"))
        self.assertTrue(list(Path(self.settings.backup_dir).glob("*.zip")))
        self.assertFalse(self.store.meta("backup_error"))
        self.assertTrue(self.store.meta("last_backup_at"))
        self.assertEqual(len(self.store.unjournaled()), 2)
        core.stop.set()
        core.worker.join(timeout=2)


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path != "/v1/models":
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[{"id":"test-local"}]}')

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                content = data["messages"][-1]["content"]
                self.send_response(200)
                self.send_header("Content-Type", "application/json" if content == "nonstream" else "text/event-stream")
                self.end_headers()
                if content == "nonstream":
                    self.wfile.write(json.dumps({"choices": [{"message": {"content": "本鱼在呢"}, "finish_reason": "stop"}]}).encode())
                    return
                for chunk in ("<thi", "nk>秘密思考", "</think>", "本鱼", "在呢"):
                    delta = {"choices": [{"index": 0, "delta": {"content": chunk}}]}
                    self.wfile.write(("data: " + json.dumps(delta) + "\n\n").encode())
                if content != "early-eof":
                    finish = {"choices": [{"delta": {}, "finish_reason": "length" if content == "length" else "stop"}]}
                    self.wfile.write(("data: " + json.dumps(finish) + "\n\ndata: [DONE]\n\n").encode())

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def client(self):
        return ModelClient(Settings(), env={"ds_base_url": f"http://127.0.0.1:{self.server.server_port}", "api_key": "test-key"})

    def test_models_discovery_and_split_thinking_filter(self):
        client = self.client()
        self.assertEqual(client.discover(), "test-local")
        answer = client.complete([{"role": "user", "content": "hello"}])
        self.assertEqual(answer, "本鱼在呢")

    def test_nonstream_compatible_response(self):
        self.assertEqual(self.client().complete([{"role": "user", "content": "nonstream"}]), "本鱼在呢")

    def test_partial_disconnect_is_not_marked_success(self):
        with self.assertRaisesRegex(ModelError, "断开"):
            self.client().complete([{"role": "user", "content": "early-eof"}])

    def test_length_limit_is_not_marked_success(self):
        with self.assertRaisesRegex(ModelError, "上限"):
            self.client().complete([{"role": "user", "content": "length"}])

    def test_cancelled_request_does_not_send(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            list(self.client().stream([{"role": "user", "content": "hello"}], cancel))

    def test_think_filter_with_plain_comparison(self):
        filter_ = ThinkFilter()
        answer = filter_.feed("1 < 2") + filter_.feed("", final=True)
        self.assertEqual(answer, "1 < 2")


if __name__ == "__main__":
    unittest.main()
