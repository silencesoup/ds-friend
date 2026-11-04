"""Isolated image UI demo: generated fixtures, simulated replies, no API calls."""
from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw
from whale_friend.app import Desktop
from whale_friend.config import LOCAL, Settings
from whale_friend.service import Companion


class DemoModel:
    sticker = 0

    def discover(self):
        return "图片验证 · 不调用 API"

    def stream(self, messages, cancel, **kwargs):
        if not cancel.is_set():
            yield f"Deep！Deep！本鱼把这张表情收藏好啦～这是隔离测试。[[sticker:{self.sticker}]]"

    def complete(self, messages, **kwargs):
        if "即时记忆执行器" in messages[0]["content"]:
            return '{"action":"none","reason":"not_requested"}'
        return '{"title":"图片验证","summary":"隔离的图片附件验证。","facts":[]}'


def fixtures():
    frames = []
    for color in ("#D6EAFF", "#FFE7AD"):
        image = Image.new("RGB", (320, 180), color)
        draw = ImageDraw.Draw(image)
        draw.ellipse((90, 40, 230, 140), fill="#367AC8")
        draw.ellipse((180, 65, 190, 75), fill="white")
        draw.text((110, 150), "Deep! Deep!", fill="#233B56")
        frames.append(image)
    png, gif = io.BytesIO(), io.BytesIO()
    frames[0].save(png, "PNG")
    frames[0].save(gif, "GIF", save_all=True, append_images=frames[1:], duration=500, loop=0)
    return png.getvalue(), gif.getvalue()


if __name__ == "__main__":
    settings = Settings(data_dir=str(LOCAL / "image-ui-check" / "data"), proactive_enabled=False, adaptive_enabled=False)
    model = DemoModel()
    core = Companion(settings, model=model)
    for data, filename in zip(fixtures(), ("点头测试.png", "点头动图.gif")):
        row = core.assets.import_bytes(data, original_name=filename)
        core.assets.update(row["id"], Path(filename).stem, "蓝色小鱼点头，用于开心答应；隔离的开发测试图。", "点头，开心", True)
        model.sticker = row["id"]
    app = Desktop(settings, core=core)
    app.base_title = "图片验证 · 小肥鱼的海边小屋"
    app.title(app.base_title)
    app.mainloop()
