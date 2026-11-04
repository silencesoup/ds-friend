"""Verify the configured endpoint sees real pixels, without reading personal memory."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale_friend.config import LOCAL, Settings, atomic_write
from whale_friend.service import Companion


def main():
    LOCAL.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vision-check-", dir=LOCAL) as directory:
        settings = Settings(data_dir=str(Path(directory) / "data"), proactive_enabled=False, adaptive_enabled=False, context_tokens=16384, reply_tokens=2048, timeout_seconds=90)
        core = Companion(settings)
        core.maintain = lambda **kwargs: False
        payloads = []
        original_open = core.model._open
        def inspect_request(endpoint, payload=None, **kwargs):
            if payload:
                payloads.append({"image_parts":sum(part.get('type') == 'image_url' for msg in payload.get('messages', []) if isinstance(msg.get('content'), list) for part in msg['content']), "roles":[msg['role'] for msg in payload.get('messages', [])]})
            return original_open(endpoint, payload, **kwargs)
        core.model._open = inspect_request
        try:
            picture = Image.new("RGB", (480, 240), (0,180,0))
            ImageDraw.Draw(picture).rectangle((240,0,480,240), fill=(160,0,200))
            buffer = io.BytesIO()
            picture.save(buffer, format="PNG")
            picture.close()
            asset = core.assets.import_bytes(buffer.getvalue(), original_name="test.png")
            session = core.store.new_session("隔离的识图验证")
            core.send(session, "请直接看图片，左半边与右半边分别是什么颜色？简短回答，不用猜图片说明。", attachments=[asset['id']])
            deadline = time.monotonic() + 240
            while core.chat_pending and time.monotonic() < deadline:
                time.sleep(.1)
            reply = core.store.messages(session)[-1]
            if core.chat_pending or reply['status'] != 'complete':
                raise RuntimeError("识图聊天未完成：" + core.last_chat_error)
            answer = reply['content']
            if not core.model.vision_available or not ('绿' in answer and '紫' in answer):
                raise RuntimeError("未通过实际画面识别验证；当前视觉状态：" + core.model.vision_status)
            report = {"model":core.model.model, "vision_available":True, "vision_status":core.model.vision_status, "reply":answer, "requests":payloads, "test_data":"generated colored panels; isolated SQLite; no personal images or memories accessed"}
            atomic_write(LOCAL / "vision-check.json", json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        finally:
            core.stop.set()
            core.cancel_reply()
            core.worker.join(timeout=5)
            if not core.worker.is_alive():
                core.store.close()


if __name__ == '__main__':
    main()
