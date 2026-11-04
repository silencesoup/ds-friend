"""Connection UI QA with a loopback mock server and a separate test .env."""
from __future__ import annotations

import json
import base64
import io
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whale_friend.app import Desktop
from whale_friend.config import ConnectionConfig, LOCAL, Settings, read_env


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self, body, status=200):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.respond({"data": [{"id": "demo-model"}]})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer demo-key":
            self.respond({"error": "invalid demo key"}, 401)
            return
        answer = "连接成功"
        content = payload['messages'][-1]['content']
        if isinstance(content, list):
            image = next((part for part in content if part['type']=='image_url'), None)
            if image:
                palette = {(255,0,0):'red',(0,0,255):'blue',(0,180,0):'green',(255,255,0):'yellow',(160,0,200):'purple'}
                with Image.open(io.BytesIO(base64.b64decode(image['image_url']['url'].split(',',1)[1]))) as picture:
                    answer = json.dumps({'left':palette.get(picture.getpixel((10,10)), 'red'), 'right':palette.get(picture.getpixel((150,10)), 'blue')})
        self.respond({"choices": [{"message": {"content": answer}, "finish_reason": "stop"}]})


def main():
    root = LOCAL / "connection-ui-check"
    root.mkdir(parents=True, exist_ok=True)
    env_path, settings_path = root / ".env", root / "settings.json"
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    save_connection, save_settings = ConnectionConfig.save, Settings.save
    try:
        # Exclude inherited connection variables to guarantee that no real service is used.
        with patch.dict(os.environ, {}, clear=True):
            save_connection(ConnectionConfig(f"http://127.0.0.1:{server.server_port}/v1", "demo-key", "demo-before"), env_path)
            Settings(data_dir=str(root / "data"), proactive_enabled=False, adaptive_enabled=False).save(settings_path)
            with patch("whale_friend.config.read_env", side_effect=lambda: read_env(env_path)), \
                 patch.object(ConnectionConfig, "save", lambda self: save_connection(self, env_path)), \
                 patch.object(Settings, "save", lambda self: save_settings(self, settings_path)):
                while True:
                    app = Desktop(Settings.load(settings_path))
                    app.base_title = "连接设置验证 · 隔离配置"
                    app.title(app.base_title)
                    app._switch("settings")
                    app.mainloop()
                    if not app.restart:
                        break
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
