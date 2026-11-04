from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from whale_friend.backup import Backups
from whale_friend.config import ConnectionConfig, Settings, read_env
from whale_friend.model import ModelClient, ModelError
from whale_friend.service import Companion
from whale_friend.store import Store
from tests.test_core import FakeModel


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env_path = self.root / ".env"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_save_updates_all_aliases_and_preserves_unrelated_configuration(self):
        self.env_path.write_text('# keep this comment\nbase_url=http://old.local\nDS_BASE_URL=http://stale.local\nexport api_key=old-key\nDS_API_KEY=stale-key\nDS_MODEL=old-model\nOTHER="keep-me"\n', encoding="utf-8")
        draft = ConnectionConfig("http://localhost:9000/v1/", 'test-key-with-"quotes"-and-\\slash', "new-model")
        draft.save(self.env_path)
        env = read_env(self.env_path)
        self.assertEqual(ConnectionConfig.load(env), draft)
        self.assertEqual(env["DS_BASE_URL"], env["base_url"])
        self.assertEqual(env["DS_API_KEY"], env["api_key"])
        text = self.env_path.read_text(encoding="utf-8")
        self.assertIn("# keep this comment", text)
        self.assertIn('OTHER="keep-me"', text)

    def test_save_can_create_env_and_clear_key_and_model(self):
        ConnectionConfig("http://localhost:9000/v1", "key", "old").save(self.env_path)
        ConnectionConfig("http://localhost:8000/v1", "", "").save(self.env_path)
        env = read_env(self.env_path)
        self.assertEqual(env["api_key"], "")
        self.assertEqual(env["ds_model"], "")
        self.assertEqual(ConnectionConfig.load(env).model, "")

    def test_environment_conflict_does_not_overwrite_file(self):
        self.env_path.write_text("# original\n", encoding="utf-8")
        with patch.dict(os.environ, {"DS_API_KEY": "process-secret"}):
            with self.assertRaisesRegex(ValueError, "DS_API_KEY") as caught:
                ConnectionConfig("http://localhost:9000", "draft-secret", "").save(self.env_path)
            self.assertNotIn("process-secret", str(caught.exception))
            self.assertNotIn("draft-secret", str(caught.exception))
        self.assertEqual(self.env_path.read_text(), "# original\n")

    def test_invalid_values_are_rejected_without_exposing_them(self):
        for url in ("", "ftp://host", "http://secret:password@host", "http://host?key=secret", "http://host/#fragment", "http://[broken", "http://host:70000", "http://bad host"):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as caught:
                    ConnectionConfig(url, "private-key").save(self.env_path)
                self.assertNotIn("private-key", str(caught.exception))
                self.assertNotIn("password", str(caught.exception))
        with self.assertRaises(ValueError):
            ConnectionConfig("http://localhost", "key\ninjected=value").validate()
        self.assertFalse(self.env_path.exists())

    def test_missing_or_invalid_url_does_not_block_desktop_construction(self):
        for url in ("", "http://[broken"):
            client = ModelClient(Settings(), env={"base_url": url, "ds_model": "manual"})
            with self.assertRaises(ModelError):
                client.discover()

    def test_legacy_model_is_respected_until_migration(self):
        env = {"base_url": "http://localhost/v1", "ds_model": "updated-env-model"}
        self.assertEqual(ModelClient(Settings(model="legacy"), env=env).model, "legacy")
        self.assertEqual(ModelClient(Settings(model=""), env=env).model, "updated-env-model")

    def test_draft_probe_uses_draft_model_without_applying_or_persisting(self):
        settings = Settings(data_dir=str(self.root / "data"), model="legacy-model", adaptive_enabled=False)
        core = Companion(settings, model=FakeModel())
        try:
            with patch("whale_friend.service.ModelClient") as constructor:
                constructor.return_value.model = "draft-model"
                core.test_connection(ConnectionConfig("http://localhost:9000/v1", "draft-secret", "draft-model"))
                core.tasks.join()
                args, kwargs = constructor.call_args
                self.assertEqual(args[0].model, "")
                self.assertEqual(kwargs["env"]["ds_model"], "draft-model")
                constructor.return_value.check_connection.assert_called_once()
            events = []
            while not core.events.empty():
                events.append(core.events.get_nowait())
            self.assertTrue(any(event.get("success") for event in events))
            self.assertNotIn("draft-secret", repr(events))
            self.assertEqual(core.settings.model, "legacy-model")
            self.assertFalse(core.store.sessions())
            self.assertFalse(self.env_path.exists())
        finally:
            core.shutdown()

    def test_unexpected_probe_error_does_not_expose_credentials(self):
        core = Companion(Settings(data_dir=str(self.root / "data")), model=FakeModel())
        try:
            with patch("whale_friend.service.ModelClient", side_effect=RuntimeError("secret-header")):
                core.test_connection(ConnectionConfig("http://localhost:9000/v1", "secret-header"))
                core.tasks.join()
            events = []
            while not core.events.empty():
                events.append(core.events.get_nowait())
            self.assertTrue(any(event.get("success") is False for event in events))
            self.assertNotIn("secret-header", repr(events))
        finally:
            core.shutdown()

    def test_probe_accepts_reasoning_only_or_length_limited_response(self):
        client = ModelClient(Settings(), env={"base_url": "http://localhost/v1", "ds_model": "reasoner"})
        data = {"choices": [{"message": {"reasoning_content": "partial thought"}, "finish_reason": "length"}]}
        with patch.object(client, "_open", return_value=io.BytesIO(json.dumps(data).encode())) as request:
            self.assertEqual(client.check_connection(), "reasoner")
            self.assertFalse(request.call_args.args[1]["stream"])
            self.assertEqual(request.call_args.args[1]["max_tokens"], 1)

    def test_probe_rejects_incompatible_response_without_exposing_body(self):
        client = ModelClient(Settings(), env={"base_url": "http://localhost/v1", "ds_model": "manual"})
        with patch.object(client, "_open", return_value=io.BytesIO(b'{"error":"sensitive server response"}')):
            with self.assertRaises(ModelError) as caught:
                client.check_connection()
        self.assertNotIn("sensitive", str(caught.exception))

    def test_connection_secrets_are_not_in_settings_or_backups(self):
        draft = ConnectionConfig("http://localhost:9000", "uniquely-private-test-key", "test")
        draft.save(self.env_path)
        self.assertNotIn(draft.api_key, repr(draft))
        settings = Settings(data_dir=str(self.root / "data"))
        settings.save(self.root / "settings.json")
        self.assertNotIn(draft.api_key, (self.root / "settings.json").read_text())
        store = Store(Path(settings.data_dir))
        try:
            archive = Backups(store, settings).create(self.root / "backups")
            import zipfile
            with zipfile.ZipFile(archive) as package:
                self.assertNotIn(".env", package.namelist())
                self.assertNotIn(draft.api_key.encode(), b"".join(package.read(name) for name in package.namelist()))
        finally:
            store.close()
