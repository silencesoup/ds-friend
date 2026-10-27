from __future__ import annotations

import queue
import threading
import time
from datetime import datetime

from .backup import Backups
from .config import Settings
from .memory import MemoryEngine
from .model import Cancelled, ModelClient
from .store import Store, now


class Companion:
    """Single worker serializes model/memory operations; UI consumes an event queue."""

    def __init__(self, settings: Settings, store: Store | None = None, model: ModelClient | None = None):
        from pathlib import Path
        self.settings = settings
        self.model = model or ModelClient(settings)
        self.store = store or Store(Path(settings.data_dir))
        self.memory = MemoryEngine(self.store, self.model, settings)
        self.memory.export_journals()
        self.backups = Backups(self.store, settings)
        self.events = queue.Queue()
        self.tasks = queue.PriorityQueue()
        self.stop = threading.Event()
        self.cancel = threading.Event()
        self.chat_pending = False
        self.maintenance_pending = False
        self.operation = ""
        self.sequence = 0
        self.state_lock = threading.Lock()
        self.worker = threading.Thread(target=self._run, name="companion-worker", daemon=True)
        self.worker.start()

    def emit(self, kind: str, **data) -> None:
        self.events.put({"kind": kind, **data})

    def _enqueue(self, priority, function, operation):
        with self.state_lock:
            self.sequence += 1
            self.tasks.put((priority, self.sequence, operation, function))

    def _run(self):
        while not self.stop.is_set():
            try:
                _, _, operation, function = self.tasks.get(timeout=0.3)
            except queue.Empty:
                continue
            self.operation = operation
            try:
                function()
            except Exception as error:
                self.emit("error", message=str(error), operation=operation)
            finally:
                self.operation = ""
                self.tasks.task_done()
                self.emit("idle")

    def connect(self):
        def work():
            name = self.model.discover()
            self.emit("connected", model=name)
        self._enqueue(0, work, "连接模型")

    def send(self, session: str, text: str, retry=False):
        with self.state_lock:
            if self.chat_pending:
                raise ValueError("请等待当前回复结束，或先停止生成")
            self.chat_pending = True
            self.cancel.clear()
        if not retry:
            identity = self.store.add_message(session, "user", text.strip())
            if len(self.store.messages(session)) == 1:
                self.store.execute("UPDATE sessions SET title=? WHERE id=?", (text.strip().replace("\n", " ")[:22], session))
            self.emit("user_saved", session=session, identity=identity)

        def work():
            assistant = None
            answer = ""
            try:
                self.emit("status", text="正在回忆，听你说…")
                context = self.memory.build_context(session, text, self.cancel)
                if self.cancel.is_set():
                    raise Cancelled("已停止生成")
                assistant = self.store.add_message(session, "assistant", "", "pending")
                self.emit("reply_start", session=session, identity=assistant)
                last_save = time.monotonic()
                for chunk in self.model.stream(context, self.cancel):
                    answer += chunk
                    self.emit("delta", session=session, text=chunk)
                    if time.monotonic() - last_save >= 0.7:
                        self.store.finish_message(assistant, answer, "pending")
                        last_save = time.monotonic()
                self.store.finish_message(assistant, answer, "complete")
                self.emit("reply_done", session=session)
            except Exception as error:
                cancelled = self.cancel.is_set() or isinstance(error, Cancelled)
                if assistant is not None:
                    self.store.finish_message(assistant, answer, "interrupted" if cancelled else "failed")
                self.emit("reply_error", session=session, message="已停止生成，部分回复已保留。" if cancelled else str(error))
            finally:
                with self.state_lock:
                    self.chat_pending = False
                self.emit("chat_finished", session=session)
                self.maintain(backup_now=True)
        self._enqueue(0, work, "角色聊天")

    def retry(self, session: str):
        messages = self.store.messages(session)
        if not messages:
            raise ValueError("还没有可以重新生成的消息")
        if messages[-1]["role"] == "assistant" and messages[-1]["status"] == "complete":
            raise ValueError("这条回复已完成；可以继续追问，或开启新的对话")
        last_user = next((m for m in reversed(messages) if m["role"] == "user" and m["status"] == "complete"), None)
        if not last_user:
            raise ValueError("没有可以重试的用户消息")
        self.send(session, last_user["content"], retry=True)

    def maintain(self, force=False, backup_now=False):
        with self.state_lock:
            if self.maintenance_pending or self.chat_pending or self.stop.is_set():
                return False
            self.maintenance_pending = True

        def work():
            count = 0
            try:
                self.emit("status", text="正在整理日志与记忆…")
                try:
                    count = self.memory.journal_due(force=force, should_yield=lambda: self.chat_pending or self.stop.is_set())
                    self.store.set_meta("journal_error", "")
                except Exception as error:
                    self.store.set_meta("journal_error", str(error))
                    self.emit("maintenance_warning", message=str(error))
                if self.settings.backup_dir:
                    stamp = self.store.meta("last_backup_at")
                    due = not stamp or (datetime.now().astimezone() - datetime.fromisoformat(stamp)).total_seconds() >= self.settings.backup_minutes * 60
                    if force or backup_now or due:
                        try:
                            path = self.backups.create()
                            self.store.set_meta("backup_error", "")
                            self.emit("backup_done", path=str(path))
                        except Exception:
                            # Paths/errors may contain sensitive filesystem details; keep a useful generic UI message.
                            message = "备份未完成，请检查指定目录是否可写、磁盘空间及 OneDrive 文件夹是否可用。本地对话仍已保存。"
                            self.store.set_meta("backup_error", message)
                            self.emit("maintenance_warning", message=message)
                self.emit("maintenance_done", count=count)
            finally:
                with self.state_lock:
                    self.maintenance_pending = False
        self._enqueue(10, work, "日志与备份")
        return True

    def cancel_reply(self):
        self.cancel.set()
        if self.operation == "角色聊天" and hasattr(self.model, "interrupt"):
            threading.Thread(target=self.model.interrupt, daemon=True).start()

    def shutdown(self):
        self.cancel_reply()
        self.stop.set()
        self.worker.join(timeout=0.6)
        if not self.worker.is_alive():
            self.store.close()
