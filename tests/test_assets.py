from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import zipfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from whale_friend.assets import AssetError, AssetLibrary, MAX_IMAGE_BYTES, StickerFilter, asset_path, copy_media, image_info, message_text
from whale_friend.backup import Backups, digest
from whale_friend.config import Settings
from whale_friend.memory import MemoryEngine, tokens
from whale_friend.service import Companion
from whale_friend.store import Store
from tests.test_core import FakeModel


def picture(format_="PNG", animated=False):
    buffer = io.BytesIO()
    frame = Image.new("RGB", (24, 18), "royalblue")
    if animated:
        frame.save(buffer, format="GIF", save_all=True, append_images=[Image.new("RGB", (24, 18), "white")], duration=80, loop=0)
    else:
        frame.save(buffer, format=format_)
    return buffer.getvalue()


class ImageModel(FakeModel):
    sticker_id = None

    def stream(self, messages, cancel, **kwargs):
        self.calls.append(messages)
        for part in ("Deep！Deep！", "[[st", "icker:", str(self.sticker_id), "]]这张送给你～"):
            if not cancel.is_set():
                yield part

    def complete(self, messages, **kwargs):
        if "现在由应用触发一次主动来信" in messages[0]["content"]:
            return f"本鱼来打个招呼～[[sticker:{self.sticker_id}]]"
        return super().complete(messages, **kwargs)


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.settings = Settings(data_dir=str(self.root / "data"), backup_dir=str(self.root / "backups"), adaptive_enabled=False)
        self.store = Store(Path(self.settings.data_dir))
        self.library = AssetLibrary(self.store)
        self.session = self.store.new_session()
        self.core = None

    def tearDown(self):
        if self.core:
            self.core.stop.set()
            self.core.worker.join(timeout=3)
            self.assertFalse(self.core.worker.is_alive())
        self.store.close()
        self.temporary.cleanup()

    def asset(self, reusable=False):
        row = self.library.import_bytes(picture(), original_name="点头表情.png")
        if reusable:
            self.library.update(row["id"], "点头", "蓝鲸点头，表示肯定、开心", "开心，点头", True)
        return self.library.get(row["id"])

    def core_with(self, model):
        self.core = Companion(self.settings, self.store, model)
        return self.core

    def test_supported_formats_and_original_animation_are_preserved(self):
        for format_ in ("PNG", "JPEG", "GIF", "WEBP"):
            with self.subTest(format=format_):
                data = picture(format_, animated=format_ == "GIF")
                row = self.library.import_bytes(data, original_name="图片." + format_.lower())
                self.assertEqual(self.library.path(row["id"]).read_bytes(), data)
                self.assertEqual((row["width"], row["height"]), (24, 18))
                if format_ == "GIF":
                    self.assertEqual(row["frames"], 2)
                thumbnail = self.library.thumbnail(row["id"], (12, 12))
                self.assertLessEqual(max(thumbnail.size), 12)

    def test_content_deduplication_and_disguised_extension(self):
        first = self.library.import_bytes(picture(), original_name="wrong.jpg")
        second = self.library.import_bytes(picture(), original_name="another.png")
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(first["relative_path"].endswith(".png"))
        self.assertEqual(len(self.library.list()), 1)

    def test_managed_copy_survives_source_removal(self):
        source = self.root / "temporary-user-picture.png"
        source.write_bytes(picture())
        row = self.library.import_file(source)
        source.unlink()
        self.assertEqual(self.library.path(row["id"]).read_bytes(), picture())
        self.assertNotIn(str(self.root), row["original_name"])

    def test_unsupported_corrupt_and_oversized_images_rejected(self):
        for data in (b"", b"<svg></svg>", b"not a picture", picture()[:30], b"x" * (MAX_IMAGE_BYTES + 1)):
            with self.subTest(length=len(data)):
                with self.assertRaises(AssetError):
                    self.library.import_bytes(data)
        self.assertFalse(self.library.list())

    def test_pixel_limit_is_checked_before_decode(self):
        with patch("whale_friend.assets.MAX_IMAGE_PIXELS", 10):
            with self.assertRaises(AssetError):
                image_info(picture())

    def test_user_pictures_are_not_automatically_reused(self):
        row = self.asset()
        self.assertFalse(row["allow_reuse"])
        self.assertFalse(self.library.catalog("开心"))
        with self.assertRaisesRegex(AssetError, "描述"):
            self.library.update(row["id"], "点头", "", "", True)

    def test_catalog_uses_descriptions_tags_and_permission(self):
        row = self.asset(True)
        found = self.library.catalog("能开心一点吗")
        self.assertEqual(found[0]["id"], row["id"])
        self.library.archive(row["id"])
        self.assertFalse(self.library.catalog("开心"))
        self.assertFalse(self.library.list())
        self.assertTrue(self.library.list(include_archived=True))
        self.library.archive(row["id"], False)
        self.assertFalse(self.library.get(row["id"])["allow_reuse"])

    def test_attachment_snapshot_keeps_original_caption(self):
        row = self.asset(True)
        identity = self.store.add_message(self.session, "user", "送你一张图", attachments=[row["id"]])
        self.library.update(row["id"], "修改名字", "这是后来改过的说明", "新标签", False)
        source = self.store.message_sources([identity])[0]
        self.assertIn("蓝鲸点头", source["attachments"][0]["description"])
        self.assertNotIn("后来改过", message_text(source))
        self.assertIn("未进行视觉解析", message_text(source))

    def test_attachment_failure_rolls_back_entire_message(self):
        row = self.asset()
        with self.assertRaises(ValueError):
            self.store.add_message(self.session, "user", "不应保存半条消息", attachments=[row["id"], 99999])
        self.assertFalse(self.store.messages(self.session))
        self.assertFalse(self.store.rows("SELECT * FROM message_assets"))

    def test_archiving_keeps_historical_picture(self):
        row = self.asset(True)
        self.store.add_message(self.session, "user", "历史图片", attachments=[row["id"]])
        self.library.archive(row["id"])
        self.assertEqual(self.store.messages(self.session)[0]["attachments"][0]["id"], row["id"])
        self.assertTrue(self.library.path(row["id"]).exists())

    def test_text_only_model_receives_metadata_and_no_binary_or_fake_vision(self):
        row = self.asset(True)
        self.store.add_message(self.session, "user", "这张表情包送你", attachments=[row["id"]])
        memory = MemoryEngine(self.store, FakeModel(), self.settings)
        context = memory.build_context(self.session, "表情包", threading.Event())
        self.assertIsInstance(context[-1]["content"], str)
        self.assertIn("蓝鲸点头", context[-1]["content"])
        self.assertIn("图片未进行视觉解析", context[-1]["content"])
        self.assertNotIn("base64", str(context))
        self.assertNotIn("image_url", str(context))

    def test_picture_only_user_message_is_saved_and_can_be_retried(self):
        row = self.asset()
        core = self.core_with(FakeModel())
        core.send(self.session, "", attachments=[row["id"]])
        core.tasks.join()
        messages = self.store.messages(self.session)
        self.assertEqual(messages[0]["content"], "[图片]")
        self.assertEqual(len(messages[0]["attachments"]), 1)
        self.assertEqual(messages[-1]["status"], "complete")
        self.store.finish_message(messages[-1]["id"], "部分回复", "failed")
        core.retry(self.session)
        core.tasks.join()
        self.assertEqual(sum(message["role"] == "user" for message in self.store.messages(self.session)), 1)

    def test_sticker_catalog_leaves_room_for_a_turn_with_full_memory(self):
        row = self.asset(True)
        self.library.update(row["id"], "点头" * 40, "开心点头" * 125, ["点头" * 12] * 12, True)
        for index in range(30):
            self.store.add_memory("事实", f"历史记录{index}：" + "用于检索的长期事实。" * 25)
        self.store.add_message(self.session, "user", "请用点头表情答应一下")
        for capacity in (4096, 8192):
            with self.subTest(capacity=capacity):
                self.settings.context_tokens = capacity
                memory = MemoryEngine(self.store, FakeModel(), self.settings)
                memory.runtime_context = lambda: "近期状态：" + "平静好奇，等待继续话题。" * 20
                memory.retrieval = lambda query: "相关历史：" + "带来源的历史事实。" * 200
                catalog = self.library.catalog("点头", limit=1)
                context = memory.build_context(self.session, "点头", threading.Event(), image_catalog=catalog)
                self.assertEqual(context[-1]["content"], "请用点头表情答应一下")
                self.assertIn("[[sticker:编号]]", context[0]["content"])
                self.assertLessEqual(sum(tokens(message["content"]) + 16 for message in context), capacity - self.settings.reply_tokens - 512)

    def test_assistant_sticker_marker_is_hidden_and_linked_to_reply(self):
        row = self.asset(True)
        model = ImageModel()
        model.sticker_id = row["id"]
        core = self.core_with(model)
        core.send(self.session, "给我一张表示开心的点头表情")
        core.tasks.join()
        reply = self.store.messages(self.session)[-1]
        self.assertEqual(reply["attachments"][0]["id"], row["id"])
        self.assertNotIn("[[sticker:", reply["content"])
        deltas = []
        while not core.events.empty():
            event = core.events.get_nowait()
            if event["kind"] == "delta":
                deltas.append(event["text"])
        self.assertNotIn("[[sticker:", "".join(deltas))

    def test_model_cannot_select_a_private_or_unlisted_picture(self):
        row = self.asset(False)
        model = ImageModel()
        model.sticker_id = row["id"]
        core = self.core_with(model)
        core.send(self.session, "聊点开心的")
        core.tasks.join()
        reply = self.store.messages(self.session)[-1]
        self.assertFalse(reply["attachments"])
        self.assertNotIn("[[sticker:", reply["content"])

    def test_proactive_reply_can_use_authorized_sticker(self):
        self.settings.quiet_start_hour = self.settings.quiet_end_hour = 0
        row = self.asset(True)
        at = datetime.now().astimezone()
        earlier = (at - timedelta(hours=3)).isoformat()
        self.store.add_message(self.session, "user", "点头表情，开心", timestamp=earlier)
        self.store.add_message(self.session, "assistant", "好呀", timestamp=earlier)
        model = ImageModel()
        model.sticker_id = row["id"]
        core = self.core_with(model)
        core.started_at = at - timedelta(hours=4)
        self.assertTrue(core.maybe_proactive(at=at))
        core.tasks.join()
        reply = self.store.messages(self.session)[-1]
        self.assertTrue(reply["proactive"])
        self.assertEqual(reply["attachments"][0]["id"], row["id"])

    def test_forgetting_source_unlinks_image_and_stops_automatic_reuse(self):
        row = self.asset(True)
        identity = self.store.add_message(self.session, "user", "与这件事有关的图", attachments=[row["id"]])
        self.store.add_message(self.session, "assistant", "收到")
        fact = self.store.add_memory("事实", "需要忘记的事", [identity])
        self.store.forget_memory(fact, erase_sources=True)
        self.assertFalse(self.store.messages(self.session)[0]["attachments"])
        self.assertFalse(self.library.get(row["id"])["allow_reuse"])
        self.assertTrue(self.library.path(row["id"]).exists())

    def test_source_url_is_recorded_without_fetching_and_rejects_credentials(self):
        row = self.library.import_bytes(picture(), source_kind="web", source_url="https://example.com/meme/123")
        self.assertEqual(row["source_url"], "https://example.com/meme/123")
        for url in ("file:///private", "javascript:evil()", "https://secret:password@example.com/a"):
            with self.assertRaises(AssetError):
                self.library.import_bytes(picture(), source_kind="web", source_url=url)

    def test_backup_restore_preserves_binary_metadata_and_links(self):
        row = self.asset(True)
        self.store.add_message(self.session, "user", "有附件的消息", attachments=[row["id"]])
        archive = Backups(self.store, self.settings).create()
        manifest, _ = Backups.inspect(archive)
        self.assertEqual(manifest["version"], 2)
        with zipfile.ZipFile(archive) as zip_:
            self.assertIn(row["relative_path"], zip_.namelist())
            self.assertIn("exports/message_assets.json", zip_.namelist())
        destination = self.root / "restored"
        Backups.restore(archive, destination)
        recovered = Store(destination)
        try:
            library = AssetLibrary(recovered)
            self.assertEqual(library.path(row["id"]).read_bytes(), picture())
            self.assertEqual(recovered.messages(self.session)[0]["attachments"][0]["id"], row["id"])
            self.assertTrue(library.get(row["id"])["allow_reuse"])
        finally:
            recovered.close()

    def test_missing_original_prevents_incomplete_backup(self):
        row = self.asset()
        self.library.path(row["id"]).unlink()
        with self.assertRaisesRegex(AssetError, "缺失"):
            Backups(self.store, self.settings).create()

    def test_restore_rejects_package_missing_a_referenced_picture(self):
        row = self.asset()
        archive = Backups(self.store, self.settings).create()
        broken = self.root / "incomplete.zip"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(broken, "w") as target:
            manifest = json.loads(source.read("manifest.json"))
            manifest["files"].pop(row["relative_path"])
            for name in source.namelist():
                if name != row["relative_path"]:
                    target.writestr(name, json.dumps(manifest) if name == "manifest.json" else source.read(name))
        with self.assertRaisesRegex(AssetError, "缺少图片"):
            Backups.inspect(broken)

    def test_managed_paths_reject_traversal_and_wrong_digest(self):
        for relative in ("../private.png", "images/aa/../../private.png", "C:/secret.png", "images/aa/" + "b" * 64 + ".png"):
            with self.assertRaises(AssetError):
                asset_path(self.root, relative, "a" * 64)

    def test_migration_copies_originals_for_snapshot(self):
        row = self.asset()
        destination = self.root / "migrated"
        destination.mkdir()
        snapshot = destination / "companion.sqlite3"
        self.store.snapshot(snapshot)
        copy_media(snapshot, self.store.directory, destination)
        recovered = Store(destination)
        try:
            self.assertEqual(AssetLibrary(recovered).path(row["id"]).read_bytes(), picture())
        finally:
            recovered.close()

    def test_version_one_backups_without_images_still_restore(self):
        self.store.execute("DROP TABLE message_assets")
        self.store.execute("DROP TABLE media_assets")
        self.store.add_memory("事实", "旧版本记忆")
        archive = Backups(self.store, self.settings).create()
        legacy = self.root / "legacy.zip"
        with zipfile.ZipFile(archive) as source, zipfile.ZipFile(legacy, "w") as target:
            manifest = json.loads(source.read("manifest.json"))
            manifest["version"] = 1
            for name in source.namelist():
                target.writestr(name, json.dumps(manifest) if name == "manifest.json" else source.read(name))
        destination = self.root / "legacy-restored"
        Backups.restore(legacy, destination)
        recovered = Store(destination)
        try:
            self.assertEqual(recovered.memories()[0]["content"], "旧版本记忆")
            self.assertEqual(AssetLibrary(recovered).list(), [])
        finally:
            recovered.close()


class StickerFilterTests(unittest.TestCase):
    def test_split_marker_whitelist_and_single_image_limit(self):
        parser = StickerFilter([12, 13])
        result = "".join(parser.feed(chunk) for chunk in ("你好[", "[sti", "cker:1", "2]]", "[[sticker:13]]", "[[sticker:999]]", "～"))
        result += parser.feed("", final=True)
        self.assertEqual(result, "你好～")
        self.assertEqual(parser.selected, [12])

    def test_incomplete_protocol_is_hidden_but_ordinary_bracket_is_kept(self):
        parser = StickerFilter([])
        result = parser.feed("正文[[sticker:12") + parser.feed("", final=True)
        self.assertEqual(result, "正文")
        parser = StickerFilter([])
        self.assertEqual(parser.feed("列表[") + parser.feed("", final=True), "列表[")


if __name__ == "__main__":
    unittest.main()
