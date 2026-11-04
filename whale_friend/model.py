from __future__ import annotations

import json
import base64
import io
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator

from .config import ConnectionConfig, Settings


class ModelError(RuntimeError):
    pass


class Cancelled(ModelError):
    pass


class ThinkFilter:
    """Hide reasoning even if <think> markers are split across SSE chunks."""

    def __init__(self):
        self.pending = ""
        self.inside = False

    def feed(self, value: str, final=False) -> str:
        self.pending += value
        output = ""
        while self.pending:
            marker = "</think>" if self.inside else "<think>"
            found = self.pending.find(marker)
            if found >= 0:
                if not self.inside:
                    output += self.pending[:found]
                self.pending = self.pending[found + len(marker):]
                self.inside = not self.inside
                continue
            retain = 0
            if not final:
                for length in range(1, min(len(marker), len(self.pending) + 1)):
                    if self.pending.endswith(marker[:length]):
                        retain = length
            safe = self.pending[:-retain] if retain else self.pending
            if not self.inside:
                output += safe
            self.pending = self.pending[-retain:] if retain else ""
            break
        return output


class ModelClient:
    def __init__(self, settings: Settings, env: dict | None = None):
        self.settings = settings
        connection = ConnectionConfig.load(env, legacy_model=settings.model)
        self.base = connection.base_url.strip().rstrip("/")
        for suffix in ("/chat/completions", "/models"):
            if self.base.endswith(suffix):
                self.base = self.base[:-len(suffix)]
        self.key = connection.api_key.strip()
        self.model = connection.model.strip()
        self.discovery_lock = threading.Lock()
        self.response_lock = threading.Lock()
        self.active_response = None
        self.vision_available = settings.vision_mode == "on"
        self.vision_checked = settings.vision_mode in ("on", "off")
        self.vision_status = "已手动开启识图" if self.vision_available else "尚未检测视觉输入" if settings.vision_mode == "auto" else "已关闭识图，仅发送图片说明"
        # Keep the desktop accessible even when configuration is missing or invalid.
        try:
            hostname = urllib.parse.urlparse(self.base).hostname
        except ValueError:
            hostname = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if hostname in ("localhost", "127.0.0.1", "::1") else urllib.request.build_opener()

    def _validate_connection(self):
        try:
            ConnectionConfig(self.base, self.key, self.model).validate()
        except ValueError as error:
            raise ModelError(str(error)) from None

    def _open(self, endpoint: str, payload=None, timeout=None):
        self._validate_connection()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key
        request = urllib.request.Request(self.base + endpoint, headers=headers, data=json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None)
        try:
            return self.opener.open(request, timeout=timeout or self.settings.timeout_seconds)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            meanings = {400: "请求参数或模型名称不被服务支持", 401: "API 密钥不正确", 403: "服务拒绝访问", 404: "接口路径或模型不存在", 429: "服务繁忙或限流，请稍后重试"}
            # Server error bodies and request URLs can contain secrets; do not log them.
            raise ModelError(f"模型服务 HTTP {code}：{meanings.get(code, '服务暂时不可用')}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ModelError("无法连接模型服务或请求超时。请确认本地模型已启动、地址正确，并检查设置中的超时时间。") from None

    def discover(self) -> str:
        with self.discovery_lock:
            self._validate_connection()
            if self.model:
                return self.model
            original = self.base
            candidates = [original] if original.endswith("/v1") else [original, original + "/v1"]
            for candidate in candidates:
                self.base = candidate
                try:
                    with self._open("/models", timeout=min(15, self.settings.timeout_seconds)) as response:
                        data = json.load(response)
                    items = data.get("data", [])
                    self.model = next((item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)), "")
                    if self.model:
                        return self.model
                except (ModelError, ValueError, AttributeError):
                    continue
            self.base = original
            raise ModelError("未能自动获取模型名称。请在设置中填写服务使用的模型名，或在 .env 中配置 ds_model。")

    def check_connection(self) -> str:
        """Check endpoint, credentials and model without requiring a finished answer."""
        payload = {"model": self.discover(), "messages": [{"role": "user", "content": "你好"}], "stream": False, "max_tokens": 1}
        try:
            with self._open("/chat/completions", payload) as response:
                data = json.load(response)
            choices = data.get("choices") if isinstance(data, dict) else None
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict) or not isinstance(choices[0].get("message"), dict):
                raise ValueError
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ModelError("服务可以访问，但返回格式不兼容聊天接口，请检查 API 地址和模型名称。") from None
        except (TimeoutError, OSError):
            raise ModelError("连接测试中断或超时，请检查服务状态与超时设置。") from None
        return self.model

    def ensure_vision(self, cancel=None, force=False) -> bool:
        """Verify pixels reach the configured model, even behind a text-only gateway."""
        if cancel and cancel.is_set():
            raise Cancelled("已停止生成")
        if not force and self.vision_checked:
            return self.vision_available
        from PIL import Image
        palette = {"red":(255,0,0), "blue":(0,0,255), "green":(0,180,0), "yellow":(255,255,0), "purple":(160,0,200)}
        left, right = secrets.SystemRandom().sample(list(palette), 2)
        picture = Image.new("RGB", (160, 80), palette[left])
        picture.paste(palette[right], (80,0,160,80))
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG")
        picture.close()
        url = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        prompt = '这是一张左右各一个纯色色块的测试图片。直接观察图片，返回JSON：{"left":"颜色英文名","right":"颜色英文名"}。颜色只从 red,blue,green,yellow,purple 选择；不要解释，不要猜没有看到的图。'
        try:
            answer = self.complete([{"role":"user", "content":[{"type":"text", "text":prompt}, {"type":"image_url", "image_url":{"url":url}}]}], max_tokens=512, cancel=cancel)
            try:
                data = json.loads(answer[answer.index("{"):answer.rindex("}") + 1])
                available = data.get("left", "").lower().strip() == left and data.get("right", "").lower().strip() == right
            except (ValueError, TypeError, AttributeError):
                available = False
        except Cancelled:
            raise
        except ModelError as error:
            if not any(code in str(error) for code in ("HTTP 400", "HTTP 404", "HTTP 422")):
                raise
            available = False
        if cancel and cancel.is_set():
            raise Cancelled("已停止生成")
        self.vision_available, self.vision_checked = available, True
        self.vision_status = "视觉测试通过，可以直接识图" if available else "视觉测试未通过：当前接口可能不支持或未正确转发图片；本轮仅发送说明，可在设置中重新测试或手动开启"
        return available

    def stream(self, messages: list[dict], cancel: threading.Event, max_tokens=None, temperature=0.75) -> Iterator[str]:
        if cancel.is_set():
            raise Cancelled("已停止生成")
        payload = {"model": self.discover(), "messages": messages, "stream": True, "temperature": temperature, "max_tokens": max_tokens or self.settings.reply_tokens}
        filter_ = ThinkFilter()
        finished = False
        received = False
        try:
            with self._open("/chat/completions", payload) as response:
                with self.response_lock:
                    self.active_response = response
                if "text/event-stream" not in response.headers.get("Content-Type", ""):
                    data = json.load(response)
                    choice = data["choices"][0]
                    if cancel.is_set():
                        raise Cancelled("已停止生成")
                    content = filter_.feed(choice.get("message", {}).get("content") or "", final=True)
                    if content:
                        yield content
                    if choice.get("finish_reason") == "length":
                        raise ModelError("回复达到长度上限，已保留部分内容。可提高回复上限后重新生成。")
                    if not content.strip():
                        raise ModelError("模型没有返回可显示的正文，请检查模型或提高回复上限。")
                    return
                for raw in response:
                    if cancel.is_set():
                        raise Cancelled("已停止生成")
                    line = raw.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    value = line[5:].strip()
                    if value == "[DONE]":
                        finished = True
                        break
                    if not value:
                        continue
                    data = json.loads(value)
                    if "error" in data:
                        raise ModelError("模型服务在生成途中报告错误，已保留收到的内容。")
                    for choice in data.get("choices", []):
                        if choice.get("index", 0) != 0:
                            continue
                        delta = choice.get("delta", {})
                        content = delta.get("content") or ""
                        if not isinstance(content, str):
                            raise ModelError("模型返回了不受支持的内容格式")
                        visible = filter_.feed(content)
                        if visible:
                            received = received or bool(visible.strip())
                            yield visible
                        if choice.get("finish_reason") == "length":
                            raise ModelError("回复达到长度上限，已保留部分内容。可提高回复上限后重新生成。")
                        if choice.get("finish_reason"):
                            finished = True
                tail = filter_.feed("", final=True)
                if tail:
                    received = received or bool(tail.strip())
                    yield tail
            if cancel.is_set():
                raise Cancelled("已停止生成")
            if not finished:
                raise ModelError("模型连接提前断开，已保留部分回复，可以重试。")
            if not received:
                raise ModelError("模型只返回了思考内容或空回复，请提高回复上限或切换模型。")
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ModelError("模型返回格式不兼容，已保留收到的内容。") from None
        except (TimeoutError, OSError):
            raise ModelError("模型连接中断或超时，已保留部分回复，可以重试。") from None
        finally:
            with self.response_lock:
                self.active_response = None

    def interrupt(self) -> None:
        """Called off the UI thread to unblock an SSE reader waiting for tokens."""
        with self.response_lock:
            response = self.active_response
        if response is not None:
            try:
                raw = getattr(getattr(response, "fp", None), "raw", None)
                connection = getattr(raw, "_sock", None)
                if connection is not None:
                    connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def complete(self, messages: list[dict], max_tokens=700, cancel=None) -> str:
        return "".join(self.stream(messages, cancel or threading.Event(), max_tokens=max_tokens, temperature=0.2)).strip()
