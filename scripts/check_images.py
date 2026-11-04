"""Image/sticker compatibility check in explicit metadata-only mode."""
from __future__ import annotations

import hashlib
import json
import queue
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from check_images_ui import fixtures
from whale_friend.backup import Backups
from whale_friend.config import LOCAL, Settings, atomic_write
from whale_friend.service import Companion
from whale_friend.store import Store


def main():
    LOCAL.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="images-check-", dir=LOCAL) as directory:
        root = Path(directory)
        settings = Settings(data_dir=str(root / "data"), proactive_enabled=False, adaptive_enabled=False, vision_mode="off")
        core = Companion(settings)
        recovered = None
        try:
            original = fixtures()[1]
            asset = core.assets.import_bytes(original, original_name="开心点头.gif")
            core.assets.update(asset["id"], "开心点头", "蓝色小鱼点头，表示开心、答应。", "开心，点头", True)
            session = core.store.new_session("隔离的图片验证")
            core.send(session, "这张图的说明是蓝色小鱼点头，表示开心答应。请使用图库中获准的‘开心点头’表情回复我，简短地说你收藏好了。", attachments=[asset["id"]])
            deadline, deltas, error = time.monotonic() + settings.timeout_seconds + 30, [], None
            while time.monotonic() < deadline:
                try:
                    event = core.events.get(timeout=1)
                except queue.Empty:
                    continue
                if event["kind"] == "delta":
                    deltas.append(event["text"])
                elif event["kind"] == "reply_error":
                    error = event["message"]
                elif event["kind"] == "chat_finished":
                    break
            else:
                raise RuntimeError("图片聊天验证超时")
            if error:
                raise RuntimeError(error)
            messages = core.store.messages(session)
            reply = messages[-1]
            if reply["status"] != "complete" or not reply["attachments"]:
                raise RuntimeError("模型本次未选择获准的表情；请查看模型指令遵循能力")
            if "[[sticker:" in "".join(deltas) or "[[sticker:" in reply["content"]:
                raise RuntimeError("内部图片选择标记泄漏")
            archive = core.backups.create(root / "backup")
            Backups.restore(archive, root / "restored")
            recovered = Store(root / "restored")
            from whale_friend.assets import AssetLibrary
            if hashlib.sha256(AssetLibrary(recovered).path(asset["id"]).read_bytes()).digest() != hashlib.sha256(original).digest():
                raise RuntimeError("恢复的图片原件不一致")
            if not recovered.messages(session)[-1]["attachments"]:
                raise RuntimeError("恢复的聊天缺少图片关联")
            report = {"model": core.model.model, "reply": reply["content"], "selected_images": [row["id"] for row in reply["attachments"]], "backup_restore": "passed", "visual_input": False, "test_data": "isolated; generated fixture only"}
            atomic_write(LOCAL / "image-check.json", json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            core.stop.set()
            core.cancel_reply()
            core.worker.join(timeout=5)
            if recovered:
                recovered.close()
            core.store.close()


if __name__ == "__main__":
    main()
