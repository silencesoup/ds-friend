from __future__ import annotations

import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator

from .config import Settings, read_env


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
        env = read_env() if env is None else env
        self.base = (env.get("DS_BASE_URL") or env.get("ds_base_url") or env.get("base_url") or "").rstrip("/")
        for suffix in ("/chat/completions", "/models"):
            if self.base.endswith(suffix):
                self.base = self.base[:-len(suffix)]
        self.key = env.get("DS_API_KEY") or env.get("api_key") or ""
        self.model = settings.model or env.get("DS_MODEL") or env.get("ds_model") or ""
        self.discovery_lock = threading.Lock()
        self.response_lock = threading.Lock()
        self.active_response = None
        parsed = urllib.parse.urlparse(self.base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ModelError("请在 .env 中设置有效的 ds_base_url（http:// 或 https://，凭据放在 api_key 中）")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if parsed.hostname in ("localhost", "127.0.0.1", "::1") else urllib.request.build_opener()

    def _open(self, endpoint: str, payload=None, timeout=None):
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
