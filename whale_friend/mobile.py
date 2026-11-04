"""Authenticated phone gateway sharing the desktop's single Companion instance."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
import uvicorn

from .assets import AssetError, MAX_IMAGE_BYTES
from .config import LOCAL, ROOT, atomic_write
from .memory_actions import KINDS

WEB = Path(__file__).parent / "web"
COOKIE = "whale_phone"


class Pairing:
    def __init__(self, path=LOCAL / "mobile-access.json"):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.failures = {}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.code, self.tokens = data["code"], data["tokens"]
        else:
            self.rotate()

    def _save(self):
        atomic_write(self.path, json.dumps({"code": self.code, "tokens": self.tokens}))

    def rotate(self):
        with self.lock:
            digits = f"{secrets.randbelow(100_000_000):08d}"
            self.code = digits[:4] + "-" + digits[4:]
            self.tokens = {}
            self._save()

    def pair(self, code, peer):
        with self.lock:
            now = time.time()
            self.failures = {key: value for key, value in self.failures.items() if value[1] > now}
            count, until = self.failures.get(peer, (0, now + 600))
            if count >= 8 or len(self.failures) >= 512:
                raise HTTPException(429, "配对尝试太多，请十分钟后重试")
            normalized = str(code).replace("-", "").replace(" ", "")
            if not normalized.isascii() or not hmac.compare_digest(normalized, self.code.replace("-", "")):
                self.failures[peer] = (count + 1, until)
                raise HTTPException(401, "配对码不正确，请在电脑设置中核对")
            self.tokens = {key: expiry for key, expiry in self.tokens.items() if expiry > now}
            if len(self.tokens) >= 64:
                self.tokens.pop(next(iter(self.tokens)))
            token = secrets.token_urlsafe(32)
            self.tokens[hashlib.sha256(token.encode()).hexdigest()] = now + 30 * 86400
            self._save()
            self.failures.pop(peer, None)
            return token

    def valid(self, token):
        if not token or len(token) > 128:
            return False
        with self.lock:
            return self.tokens.get(hashlib.sha256(token.encode()).hexdigest(), 0) > time.time()

    def revoke(self, token):
        with self.lock:
            self.tokens.pop(hashlib.sha256(token.encode()).hexdigest(), None)
            self._save()


async def read_body(request, limit=65536):
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(413, "文件或请求过大")
    return bytes(data)


async def read_json(request):
    try:
        result = json.loads(await read_body(request))
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError):
        raise HTTPException(400, "请求格式不正确") from None


def public_asset(row):
    return {key: row[key] for key in ("id", "title", "description", "tags", "allow_reuse", "archived", "width", "height", "frames", "byte_size")}


def create_app(core, pairing: Pairing):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def local_origin(request, call_next):
        # Only direct local-network/loopback IP URLs; no CORS or proxy-header trust.
        host = request.headers.get("host", "")
        try:
            hostname = urlsplit("http://" + host).hostname
            allowed = hostname == "localhost" or ipaddress.ip_address(hostname).is_private
        except ValueError:
            allowed = False
        origin = request.headers.get("origin")
        if not allowed or (origin and origin != f"{request.url.scheme}://{host}"):
            return JSONResponse({"detail": "只接受本机或局域网的同源访问"}, status_code=403)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    def auth(request: Request):
        if not pairing.valid(request.cookies.get(COOKIE)):
            raise HTTPException(401, "请先与电脑小屋配对")

    def session_exists(identity):
        if not isinstance(identity, str) or not core.store.rows("SELECT id FROM sessions WHERE id=?", (identity,)):
            raise HTTPException(404, "会话不存在，请重新选择")

    @contextmanager
    def editing():
        with core.state_lock:
            if core.operation or core.chat_pending or core.maintenance_pending or core.reflection_pending or core.proactive_pending or not core.tasks.empty():
                raise HTTPException(409, "本鱼正在回复或整理记忆，请稍后再改")
            yield
        core.emit("data_changed")
        core.maintain(backup_now=True)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"detail": str(error) if isinstance(error, AssetError) else "操作未完成，请检查输入或等待当前回复结束"}, status_code=400)

    @app.exception_handler(OSError)
    async def unavailable(request, error):
        return JSONResponse({"detail": "文件暂不可用，请在电脑检查数据目录或备份"}, status_code=503)

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html", media_type="text/html")

    @app.get("/static/{name}")
    def static(name: str):
        if name not in ("app.js", "style.css", "manifest.webmanifest"):
            raise HTTPException(404)
        return FileResponse(WEB / name)

    @app.get("/character.png")
    def character():
        return FileResponse(ROOT / "assets" / "character-reference.png")

    @app.post("/api/pair")
    async def pair(request: Request):
        data = await read_json(request)
        token = pairing.pair(data.get("code", ""), request.client.host)
        response = JSONResponse({"ok": True})
        response.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, samesite="strict", secure=request.url.scheme == "https")
        return response

    @app.post("/api/logout", dependencies=[Depends(auth)])
    def logout(request: Request):
        pairing.revoke(request.cookies[COOKIE])
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/api/state", dependencies=[Depends(auth)])
    def state(session: str = "", before: int = 0):
        sessions = core.store.sessions()
        identity = session or (sessions[0]["id"] if sessions else "")
        messages, more = [], False
        if identity:
            session_exists(identity)
            rows = core.store.rows("SELECT m.*,p.message_id IS NOT NULL AS proactive FROM messages m LEFT JOIN proactive_messages p ON p.message_id=m.id WHERE m.session_id=?" + (" AND m.id<?" if before else "") + " ORDER BY m.id DESC LIMIT 81", (identity, before) if before else (identity,))
            more = len(rows) > 80
            messages = core.store._with_attachments(list(reversed(rows[:80])))
            for message in messages:
                message["attachments"] = [public_asset(row) for row in message["attachments"]]
        return {"session": identity, "sessions": sessions, "messages": messages, "has_older": more,
                "character": core.settings.character_name, "user": core.settings.user_name, "mood": core.presence.mood()["mood"],
                "busy": core.chat_pending, "active_session": core.active_session, "operation": core.operation,
                "last_error": core.last_chat_error, "backup_at": core.store.meta("last_backup_at"),
                "vision_enabled":core.memory.vision_enabled, "vision_mode":core.settings.vision_mode,
                "vision_status":getattr(core.model, "vision_status", ""),
                "animation":core.animations.snapshot(identity),"animation_idle":core.animations.snapshot(identity,include_reply=False)}

    @app.get('/api/animations', dependencies=[Depends(auth)])
    def animation_catalog():
        return {'clips':core.animations.catalog()}

    @app.get('/api/animations/{identity}/video', dependencies=[Depends(auth)])
    def animation_video(identity:str):
        try:
            path = core.animations.path(identity)
        except ValueError:
            raise HTTPException(404,'动画不存在') from None
        return FileResponse(path,media_type='video/webm',headers={'Cache-Control':'private, max-age=86400'})

    @app.post("/api/sessions", dependencies=[Depends(auth)])
    def new_session():
        identity = core.store.new_session()
        core.emit("data_changed")
        return {"id": identity}

    @app.post("/api/send", dependencies=[Depends(auth)])
    async def send(request: Request):
        data = await read_json(request)
        session_exists(data.get("session"))
        try:
            request_id = str(uuid.UUID(data["request_id"]))
            text, images = data.get("text", ""), data.get("images", [])
            if not isinstance(text, str) or len(text) > 20000 or not isinstance(images, list) or any(type(i) is not int for i in images):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(400, "消息格式不正确，请刷新后重试") from None
        try:
            identity = core.send(data["session"], text, attachments=images, request_id=request_id)
        except ValueError:
            raise HTTPException(409, "当前正在回复，或消息编号已用于其他内容；请稍后重试") from None
        return {"message_id": identity}

    @app.post("/api/reply/{action}", dependencies=[Depends(auth)])
    async def reply(action: str, request: Request):
        data = await read_json(request)
        session_exists(data.get("session"))
        if action == "retry":
            core.retry(data["session"])
        elif action == "stop":
            if core.chat_pending and core.active_session == data["session"]:
                core.cancel_reply()
        else:
            raise HTTPException(404)
        return {"ok": True}

    @app.post("/api/activity", dependencies=[Depends(auth)])
    async def activity(request: Request):
        data = await read_json(request)
        if data.get("composing") is True:
            core.remote_composing_until = time.monotonic() + 45
        return {"ok": True}

    @app.get("/api/memories", dependencies=[Depends(auth)])
    def memories():
        return core.store.memories()

    @app.get("/api/memory/overview", dependencies=[Depends(auth)])
    def memory_overview(session: str = ""):
        if session:
            session_exists(session)
        return core.memory.hub.overview(session)

    @app.patch("/api/memory/annotations/{identity}", dependencies=[Depends(auth)])
    async def annotate_memory(identity: int, request: Request):
        data = await read_json(request)
        level=data.get('importance')
        if level is not None and (type(level) is not int or not 0<=level<=3):
            raise HTTPException(400,'重要程度应为0到3或自动')
        with editing():
            if any(key in data for key in ('pinned','active','topics_override')):
                core.memory.hub.annotate(identity, data.get("pinned", False), data.get("active", True), data.get("topics_override"))
            if 'importance' in data:
                core.memory.hub.signals.set_importance(identity,level)
        return {"ok": True}

    @app.post('/api/memory/changes/{identity}',dependencies=[Depends(auth)])
    async def resolve_memory_change(identity:int,request:Request):
        data=await read_json(request)
        with editing():
            core.memory.hub.signals.resolve(identity,data.get('decision'))
            core.memory.hub.index()
        return {'ok':True}

    @app.post("/api/memory/topics", dependencies=[Depends(auth)])
    @app.patch("/api/memory/topics/{identity}", dependencies=[Depends(auth)])
    async def save_topic(request: Request, identity: str | None = None):
        data = await read_json(request)
        if not isinstance(data.get("title"), str):
            raise HTTPException(400, "请填写主题名字")
        with editing():
            result = core.memory.hub.save_topic(data["title"], data.get("aliases", ""), identity)
        return {"id": result}

    @app.get("/api/memory/versions/{identity}", dependencies=[Depends(auth)])
    def memory_versions(identity: int):
        if not core.store.rows("SELECT id FROM memories WHERE id=?", (identity,)):
            raise HTTPException(404, "记忆不存在")
        return core.store.rows("SELECT * FROM memory_versions WHERE memory_id=? ORDER BY id DESC", (identity,))

    @app.post("/api/memory/organize", dependencies=[Depends(auth)])
    def organize_memory():
        if not core.maintain(force=True, backup_now=True):
            raise HTTPException(409, "本鱼正在忙，请稍后再整理")
        return {"ok": True}

    @app.api_route("/api/memories/{identity}", methods=["PATCH"], dependencies=[Depends(auth)])
    @app.post("/api/memories", dependencies=[Depends(auth)])
    async def save_memory(request: Request, identity: int | None = None):
        data = await read_json(request)
        content, kind = data.get("content", ""), data.get("kind", "事实")
        level=data.get('importance')
        if level is not None and (type(level) is not int or not 0<=level<=3):
            raise HTTPException(400,'重要程度应为0到3或自动')
        if kind not in KINDS or not isinstance(content, str) or not 1 <= len(content.strip()) <= 1000:
            raise HTTPException(400, "记忆内容请填写 1–1000 字，并选择有效类型")
        with editing():
            has_annotation = any(key in data for key in ("pinned", "active", "topics_override"))
            if has_annotation:
                core.memory.hub.validate_annotation(identity, data.get("pinned", False), data.get("active", True), data.get("topics_override"))
            if identity is None:
                identity = core.store.add_memory(kind, content.strip())
                if identity is None:
                    identity = core.store.rows("SELECT id FROM memories WHERE content=?", (content.strip(),))[0]["id"]
            elif core.store.rows("SELECT id FROM memories WHERE id=?", (identity,)):
                old = core.store.rows("SELECT kind,content FROM memories WHERE id=?", (identity,))[0]
                if old["kind"] != kind or old["content"] != content.strip():
                    core.store.update_memory(identity, kind, content.strip())
            else:
                raise HTTPException(404, "记忆不存在")
            if has_annotation:
                core.memory.hub.annotate(identity, data.get("pinned", False), data.get("active", True), data.get("topics_override"))
            if 'importance' in data:
                core.memory.hub.signals.set_importance(identity,level)
            core.memory.export_journals()
        return {"ok": True, "id": identity}

    @app.get("/api/journals", dependencies=[Depends(auth)])
    def journals(query: str = ""):
        return core.store.search(query[:100], 200) if query else core.store.journals()[:200]

    @app.get("/api/images", dependencies=[Depends(auth)])
    def images(query: str = ""):
        return [public_asset(row) for row in core.assets.list(query[:100])[:300]]

    @app.post("/api/images", dependencies=[Depends(auth)])
    async def upload(request: Request):
        content = await read_body(request, MAX_IMAGE_BYTES)
        name = unquote(request.headers.get("x-file-name", "手机图片.png"))[:200]
        row = await asyncio.to_thread(core.assets.import_bytes, content, original_name=name)
        core.maintain(backup_now=True)
        return public_asset(row)

    @app.get("/api/images/{identity}", dependencies=[Depends(auth)])
    def image(identity: int):
        row = core.assets.get(identity)
        return FileResponse(core.assets.path(identity), media_type=row["mime"], headers={"Content-Disposition": "inline"})

    @app.patch("/api/images/{identity}", dependencies=[Depends(auth)])
    async def describe_image(identity: int, request: Request):
        data = await read_json(request)
        if any(not isinstance(data.get(key, ""), str) for key in ("title", "description", "tags")) or type(data.get("allow_reuse", False)) is not bool:
            raise HTTPException(400, "图片说明格式不正确")
        with editing():
            core.assets.update(identity, data.get("title", ""), data.get("description", ""), data.get("tags", ""), data.get("allow_reuse", False))
        return {"ok": True}

    return app


class MobileServer:
    def __init__(self, core, port=None, pairing=None, host="0.0.0.0"):
        self.core, self.host = core, host
        self.port = port if port is not None else core.settings.mobile_port
        self.pairing = pairing or Pairing()
        self.app = create_app(core, self.pairing)
        self.thread = self.server = self.socket = None
        self.failure = None

    def start(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.socket.bind((self.host, self.port))
            self.port = self.socket.getsockname()[1]
            self.socket.listen(128)
            self.socket.setblocking(False)
            config = uvicorn.Config(self.app, host=self.host, port=self.port, log_config=None, access_log=False, proxy_headers=False, lifespan="off", timeout_graceful_shutdown=1, limit_concurrency=32, server_header=False)
            self.server = uvicorn.Server(config)

            def run():
                try:
                    asyncio.run(self.server.serve(sockets=[self.socket]))
                except Exception:
                    self.failure = "手机连接服务启动失败，请检查端口是否被占用"
            self.thread = threading.Thread(target=run, daemon=True, name="phone-gateway")
            self.thread.start()
            deadline = time.monotonic() + 3
            while not self.server.started and self.thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.02)
            if not self.server.started:
                raise RuntimeError(self.failure or "手机连接服务未能启动")
        except Exception:
            self.close()
            raise
        return self

    def urls(self):
        addresses = {"127.0.0.1"}
        try:
            addresses.update(row[4][0] for row in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET) if ipaddress.ip_address(row[4][0]).is_private)
        except OSError:
            pass
        return [f"http://{address}:{self.port}" for address in sorted(addresses, key=lambda value: (value == "127.0.0.1", value))]

    def close(self):
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=3)
        if self.socket:
            self.socket.close()
