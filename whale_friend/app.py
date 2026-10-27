from __future__ import annotations

import json
import os
import queue
import sys
import tkinter as tk
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .backup import Backups
from .config import LOCAL, ROOT, Settings
from .service import Companion

NAVY = "#132D4B"
BLUE = "#327CEC"
PALE = "#F1F6FD"
WHITE = "#FFFFFF"
INK = "#233B56"
MUTED = "#7B8DA3"
LINE = "#DEE8F5"
FONT = "PingFang SC" if sys.platform == "darwin" else "Microsoft YaHei UI"


def label(parent, text="", size=11, color=INK, bg=WHITE, bold=False, **kwargs):
    return tk.Label(parent, text=text, font=(FONT, size, "bold" if bold else "normal"), fg=color, bg=bg, **kwargs)


def button(parent, text, command, primary=False, **kwargs):
    return tk.Button(parent, text=text, command=command, font=(FONT, 10), bg=BLUE if primary else PALE,
                     fg=WHITE if primary else INK, activebackground="#2466CA" if primary else LINE,
                     activeforeground=WHITE if primary else INK, relief="flat", borderwidth=0,
                     padx=15, pady=9, cursor="hand2", **kwargs)


def readonly(widget: tk.Text, text: str):
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", text)
    widget.configure(state="disabled")


class Desktop(tk.Tk):
    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        try:
            self.core = Companion(settings)
        except Exception:
            self.destroy()
            raise
        self.restart = False
        self.page = "chat"
        self.session_ids = []
        self.memory_items = []
        self.journal_items = []
        self.current_memory = None
        self.streaming_session = None
        self.streaming_start = None
        self.model_label = "连接中"
        self.chat_error = ""
        self.title(f"{settings.character_name} · 鲸鱼娘的海边小屋")
        self.geometry("1260x850")
        self.minsize(1050, 740)
        self.configure(bg=PALE)
        self.option_add("*Font", (FONT, 10))
        self.protocol("WM_DELETE_WINDOW", self._close)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TScrollbar", background="#CFDCEE", troughcolor=PALE, borderwidth=0, arrowsize=12)
        self._build()
        sessions = self.core.store.sessions()
        self.session = sessions[0]["id"] if sessions else self.core.store.new_session()
        self._refresh_sessions()
        self._render_chat()
        self._switch("chat")
        self.after(80, self._poll)
        self.after(1500, self._tick)
        self.core.connect()
        self.core.maintain()

    def _build(self):
        self.sidebar = tk.Frame(self, bg=NAVY, width=234)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        brand = tk.Frame(self.sidebar, bg=NAVY)
        brand.pack(fill="x", padx=22, pady=(25, 20))
        label(brand, "W H A L E  F R I E N D", 9, "#8EB9EF", NAVY).pack(anchor="w")
        label(brand, self.settings.character_name + "的小屋", 21, WHITE, NAVY, True).pack(anchor="w", pady=(7, 5))
        label(brand, "把普通的日子，记成小小的浪花。", 9, "#A9BED6", NAVY).pack(anchor="w")
        self.nav = {}
        for key, text in (("chat", "◉   和本鱼聊天"), ("memories", "◇   记忆收藏夹"), ("journals", "▤   每日小日志"), ("settings", "⚙   设置与备份")):
            item = tk.Button(self.sidebar, text=text, anchor="w", command=lambda k=key: self._switch(k), bg=NAVY, fg="#BFD0E4", activebackground="#25466C", activeforeground=WHITE, relief="flat", bd=0, padx=20, pady=12, cursor="hand2")
            item.pack(fill="x", padx=12, pady=2)
            self.nav[key] = item
        tk.Frame(self.sidebar, height=1, bg="#294563").pack(fill="x", padx=22, pady=(18, 14))
        session_head = tk.Frame(self.sidebar, bg=NAVY)
        session_head.pack(fill="x", padx=22)
        label(session_head, "我们的来信", 9, "#93ABC6", NAVY).pack(side="left")
        tk.Button(session_head, text="＋ 新对话", command=self._new_session, bg=NAVY, fg="#B7D4FF", bd=0, cursor="hand2").pack(side="right")
        self.sessions_list = tk.Listbox(self.sidebar, bg=NAVY, fg="#C5D5E7", selectbackground="#2B507B", selectforeground=WHITE, bd=0, highlightthickness=0, activestyle="none", exportselection=False, height=7)
        self.sessions_list.pack(fill="both", expand=True, padx=20, pady=12)
        self.sessions_list.bind("<<ListboxSelect>>", self._select_session)
        portrait = tk.Frame(self.sidebar, bg="#EAF2FF")
        portrait.pack(fill="x", padx=16, pady=(0, 18))
        art_path = ROOT / "assets" / "character-reference.png"
        if art_path.exists():
            self.reference = tk.PhotoImage(file=str(art_path))
            # Clip the first view in a canvas; the supplied reference stays unmodified.
            self.portrait = tk.PhotoImage()
            self.portrait.tk.call(self.portrait, "copy", self.reference, "-from", 0, 0, 540, 621, "-subsample", 3, 3)
            canvas = tk.Canvas(portrait, width=202, height=220, bg=WHITE, bd=0, highlightthickness=0)
            canvas.pack(fill="x")
            canvas.create_image(11, 5, image=self.portrait, anchor="nw")
        label(portrait, "本鱼在这里，慢慢说就好。", 9, "#4D6E98", "#EAF2FF").pack(pady=10)
        self.content = tk.Frame(self, bg=PALE)
        self.content.pack(side="left", fill="both", expand=True)
        header = tk.Frame(self.content, bg=PALE)
        header.pack(fill="x", padx=32, pady=(25, 18))
        titles = tk.Frame(header, bg=PALE)
        titles.pack(side="left")
        self.title_label = label(titles, "今天，也想听你说说。", 22, INK, PALE, True)
        self.title_label.pack(anchor="w")
        self.subtitle = label(titles, "一封来信，一段陪伴。", 10, MUTED, PALE)
        self.subtitle.pack(anchor="w", pady=(6, 0))
        self.connection = label(header, "●  正在连接", 9, "#5B8BBE", PALE)
        self.connection.pack(side="right", anchor="n", pady=5)
        self.pages = {}
        for name in ("chat", "memories", "journals", "settings"):
            self.pages[name] = tk.Frame(self.content, bg=PALE)
        self._chat_page(self.pages["chat"])
        self._memory_page(self.pages["memories"])
        self._journal_page(self.pages["journals"])
        self._settings_page(self.pages["settings"])
        footer = tk.Frame(self.content, bg=PALE)
        footer.pack(side="bottom", fill="x", padx=32, pady=(6, 12))
        self.status = label(footer, "本鱼正在准备小屋…", 9, MUTED, PALE, anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.backup_status = label(footer, "", 8, MUTED, PALE)
        self.backup_status.pack(side="right")

    def _chat_page(self, parent):
        panel = tk.Frame(parent, bg=WHITE, highlightbackground=LINE, highlightthickness=1)
        panel.pack(fill="both", expand=True, padx=32)
        self.chat_text = tk.Text(panel, height=1, bg=WHITE, fg=INK, wrap="word", bd=0, padx=25, pady=22, font=(FONT, 11), spacing3=9, cursor="arrow", state="disabled")
        scroll = ttk.Scrollbar(panel, command=self.chat_text.yview)
        scroll.pack(side="right", fill="y")
        self.chat_text.configure(yscrollcommand=scroll.set)
        self.chat_text.pack(fill="both", expand=True)
        self.chat_text.tag_configure("name", foreground=BLUE, font=(FONT, 10, "bold"), spacing1=16, spacing3=7)
        self.chat_text.tag_configure("user_name", foreground="#74859E", font=(FONT, 10, "bold"), spacing1=16, spacing3=7)
        self.chat_text.tag_configure("user", background="#F0F5FF", lmargin1=12, lmargin2=12, rmargin=12, spacing1=8, spacing3=12)
        self.chat_text.tag_configure("assistant", lmargin1=0, lmargin2=0, rmargin=30, spacing3=14)
        self.chat_text.tag_configure("hint", foreground=MUTED, font=(FONT, 9), spacing3=15)
        self.chat_text.tag_configure("welcome", foreground=INK, font=(FONT, 16, "bold"), spacing1=40, spacing3=16)
        composer = tk.Frame(parent, bg=WHITE, highlightbackground=LINE, highlightthickness=1)
        composer.pack(side="bottom", fill="x", padx=32, pady=(14, 0), before=panel)
        self.input = tk.Text(composer, height=3, wrap="word", bg=WHITE, fg=INK, insertbackground=BLUE, bd=0, padx=16, pady=14, undo=True, font=(FONT, 11))
        self.input.pack(fill="x")
        self.input.bind("<Return>", self._enter)
        bar = tk.Frame(composer, bg=WHITE)
        bar.pack(fill="x", padx=16, pady=(0, 12))
        label(bar, "Enter 发送 · Shift+Enter 换行 · 当前支持文字", 9, MUTED).pack(side="left")
        self.send_button = button(bar, "发送来信  ↗", self._send, True)
        self.send_button.pack(side="right")
        self.stop_button = button(bar, "停止", self._stop_reply)
        self.stop_button.pack(side="right", padx=(0, 8))
        self.stop_button.configure(state="disabled")
        self.retry_button = button(bar, "重试", self._retry)
        self.retry_button.pack(side="right", padx=(0, 8))

    def _split_page(self, parent):
        panel = tk.Frame(parent, bg=WHITE, highlightbackground=LINE, highlightthickness=1)
        panel.pack(fill="both", expand=True, padx=32)
        left = tk.Frame(panel, bg=WHITE, width=240)
        left.pack(side="left", fill="y", padx=16, pady=18)
        left.pack_propagate(False)
        tk.Frame(panel, width=1, bg=LINE).pack(side="left", fill="y", pady=18)
        right = tk.Frame(panel, bg=WHITE)
        right.pack(side="left", fill="both", expand=True, padx=22, pady=18)
        return left, right

    def _memory_page(self, parent):
        left, right = self._split_page(parent)
        label(left, "被好好记住的小事", 12, bold=True).pack(anchor="w", pady=(0, 12))
        self.memory_list = tk.Listbox(left, bg=WHITE, fg=INK, selectbackground="#E5EFFF", selectforeground=BLUE, bd=0, highlightthickness=0, activestyle="none", exportselection=False)
        self.memory_list.pack(fill="both", expand=True)
        self.memory_list.bind("<<ListboxSelect>>", self._select_memory)
        button(left, "＋ 手动记一件事", self._new_memory).pack(fill="x", pady=(12, 0))
        label(right, "长期记忆", 16, bold=True).pack(anchor="w")
        label(right, "可修正、可删除；自动提取的内容请核对来源。", 9, MUTED, wraplength=430, justify="left").pack(anchor="w", pady=(5, 18))
        self.memory_kind = tk.StringVar(value="事实")
        kinds = ttk.Combobox(right, textvariable=self.memory_kind, values=("偏好", "称呼", "目标", "事实"), state="readonly", width=16)
        kinds.pack(anchor="w", pady=(0, 10))
        self.memory_text = tk.Text(right, height=5, bg=PALE, fg=INK, wrap="word", bd=0, padx=12, pady=12, undo=True)
        self.memory_text.pack(fill="x")
        actions = tk.Frame(right, bg=WHITE)
        actions.pack(fill="x", pady=12)
        button(actions, "保存记忆", self._save_memory, True).pack(side="left")
        button(actions, "删除…", self._forget_memory).pack(side="left", padx=10)
        label(right, "记忆从哪里来", 11, bold=True).pack(anchor="w", pady=(12, 8))
        self.source_text = tk.Text(right, bg=WHITE, fg=MUTED, wrap="word", height=10, bd=0, state="disabled")
        self.source_text.pack(fill="both", expand=True)
        label(right, "修正会阻止旧来源再次自动提取，旧原文仍可在历史对话中核对。", 9, MUTED, wraplength=440, justify="left").pack(anchor="w", pady=10)

    def _journal_page(self, parent):
        left, right = self._split_page(parent)
        label(left, "按日收藏的浪花", 12, bold=True).pack(anchor="w", pady=(0, 12))
        self.journal_query = tk.StringVar()
        entry = tk.Entry(left, textvariable=self.journal_query, bg=PALE, fg=INK, relief="flat", borderwidth=8)
        entry.pack(fill="x")
        entry.bind("<Return>", lambda event: self._refresh_journals())
        label(left, "输入日期或主题，例如 2026-10-27", 8, MUTED).pack(anchor="w", pady=6)
        button(left, "检索日志", self._refresh_journals).pack(fill="x", pady=(0, 10))
        self.journal_list = tk.Listbox(left, bg=WHITE, fg=INK, selectbackground="#E5EFFF", selectforeground=BLUE, bd=0, highlightthickness=0, activestyle="none", exportselection=False)
        self.journal_list.pack(fill="both", expand=True)
        self.journal_list.bind("<<ListboxSelect>>", self._select_journal)
        button(left, "整理尚未归档的对话", self._archive_now).pack(fill="x", pady=(12, 0))
        self.journal_detail = tk.Text(right, bg=WHITE, fg=INK, wrap="word", bd=0, state="disabled", spacing3=10)
        scroll = ttk.Scrollbar(right, command=self.journal_detail.yview)
        scroll.pack(side="right", fill="y")
        self.journal_detail.configure(yscrollcommand=scroll.set)
        self.journal_detail.pack(fill="both", expand=True)
        readonly(self.journal_detail, "这里会留下每一天的对话足迹。\n\n对话较多时自动分段；空闲后自动整理；重新打开应用会补写遗漏的日志。\n\n也可以点击左下方，立即整理今天的来信。")

    def _settings_page(self, parent):
        actions = tk.Frame(parent, bg=PALE)
        actions.pack(side="bottom", fill="x", padx=32, pady=(10, 0))
        button(actions, "保存设置并重新打开", self._save_settings, True).pack(side="right")
        label(actions, "修改目录后，记得保存设置。", 9, MUTED, PALE).pack(side="left")
        canvas = tk.Canvas(parent, bg=PALE, bd=0, highlightthickness=0)
        scroll = ttk.Scrollbar(parent, command=canvas.yview)
        scroll.pack(side="right", fill="y", padx=(0, 22))
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(fill="both", expand=True, padx=(32, 0))
        form = tk.Frame(canvas, bg=WHITE, padx=24, pady=22)
        window = canvas.create_window((0, 0), window=form, anchor="nw")
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        form.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        self.bind_all("<MouseWheel>", lambda event: canvas.yview_scroll(-event.delta if sys.platform == "darwin" else -int(event.delta / 120), "units") if self.page == "settings" else None)
        self.setting_vars = {}
        label(form, "数据住在哪里", 16, bold=True).pack(anchor="w")
        label(form, "工作数据库放在本机；完整恢复包放进你选择的 OneDrive 文件夹。", 10, MUTED, wraplength=680, justify="left").pack(anchor="w", pady=(7, 16))
        self._setting_field(form, "data_dir", "本地工作数据目录", browse=True)
        self._setting_field(form, "backup_dir", "备份目录（可选 OneDrive 中的专用文件夹）", browse=True)
        label(form, "备份包含原始对话、记忆、上下文摘要、日志及月度索引，不包含 .env 或 API 密钥。\n生成恢复包后仍需 OneDrive 完成同步；应用不能确认云端同步状态。", 9, MUTED, wraplength=720, justify="left").pack(anchor="w", pady=(0, 14))
        self._setting_field(form, "backup_minutes", "自动备份间隔（分钟）")
        self._setting_field(form, "backup_keep", "保留最近多少个恢复包")
        backup_actions = tk.Frame(form, bg=WHITE)
        backup_actions.pack(fill="x", pady=(5, 10))
        button(backup_actions, "立即备份", self._backup_now).pack(side="left")
        button(backup_actions, "从恢复包恢复…", self._restore).pack(side="left", padx=10)
        self.backup_detail = label(form, "", 9, MUTED, wraplength=720, justify="left")
        self.backup_detail.pack(anchor="w", pady=(0, 18))
        tk.Frame(form, height=1, bg=LINE).pack(fill="x", pady=10)
        label(form, "本鱼的设定", 16, bold=True).pack(anchor="w", pady=(7, 16))
        self._setting_field(form, "character_name", "界面名字（自称为 本鱼 / 本鲸鱼 / 我）")
        self._setting_field(form, "user_name", "本鱼怎样称呼你")
        tk.Frame(form, height=1, bg=LINE).pack(fill="x", pady=15)
        label(form, "模型与记忆", 16, bold=True).pack(anchor="w", pady=(5, 16))
        label(form, "服务地址和密钥从仓库 .env 读取；模型名称留空时自动发现。", 9, MUTED).pack(anchor="w", pady=(0, 10))
        self._setting_field(form, "model", "模型名称")
        button(form, "重新检测模型连接", self.core.connect).pack(anchor="w", pady=(0, 13))
        self._setting_field(form, "context_tokens", "模型上下文容量（tokens，请按实际部署填写）")
        self._setting_field(form, "reply_tokens", "每次回复上限（tokens）")
        self._setting_field(form, "timeout_seconds", "请求超时（秒）")
        self._setting_field(form, "idle_minutes", "对话空闲多少分钟后整理日志")
        self._setting_field(form, "journal_chunk_chars", "当日对话达到多少字符后分段归档")
        label(form, "上下文长度采用保守估算，并留出回复空间。\n保存设置后会重新打开窗口；改变数据目录会先复制当前数据，原目录保留。", 9, MUTED, wraplength=710, justify="left").pack(anchor="w", pady=(8, 16))

    def _setting_field(self, parent, key, title, browse=False):
        label(parent, title, 10).pack(anchor="w", pady=(0, 6))
        row = tk.Frame(parent, bg=WHITE)
        row.pack(fill="x", pady=(0, 13))
        variable = tk.StringVar(value=str(getattr(self.settings, key)))
        self.setting_vars[key] = variable
        entry = tk.Entry(row, textvariable=variable, bg=PALE, fg=INK, relief="flat", borderwidth=8)
        entry.pack(side="left", fill="x", expand=True)
        if browse:
            def select():
                initial = variable.get() or os.environ.get("OneDrive") or str(Path.home())
                path = filedialog.askdirectory(parent=self, title=title, initialdir=initial if Path(initial).is_dir() else str(Path.home()))
                if path:
                    variable.set(path)
            button(row, "选择文件夹", select).pack(side="right", padx=(10, 0))

    def _switch(self, name):
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill="both", expand=True, pady=(0, 8))
        self.page = name
        for key, item in self.nav.items():
            item.configure(bg="#25496F" if key == name else NAVY, fg=WHITE if key == name else "#BFD0E4")
        titles = {"chat": ("今天，也想听你说说。", "一封来信，一段陪伴。"), "memories": ("你说过的，本鱼放在心上。", "长期事实与偏好，都可以由你修正。"), "journals": ("日子有迹可循。", "按日期、按话题，找回我们聊过的事。"), "settings": ("给小屋安个稳稳的家。", "模型、角色，还有不怕丢失的记忆。")}
        self.title_label.configure(text=titles[name][0])
        self.subtitle.configure(text=titles[name][1])
        if name == "memories":
            self._refresh_memories()
        elif name == "journals":
            self._refresh_journals()
        elif name == "settings":
            self._update_backup_status()

    def _refresh_sessions(self):
        sessions = self.core.store.sessions()
        self.session_ids = [item["id"] for item in sessions]
        self.sessions_list.delete(0, "end")
        for item in sessions:
            self.sessions_list.insert("end", "  " + item["title"])
        if self.session in self.session_ids:
            index = self.session_ids.index(self.session)
            self.sessions_list.selection_set(index)

    def _new_session(self):
        if self.core.chat_pending:
            self._notice("请先等待或停止当前回复，再开启新对话。")
            return
        self.session = self.core.store.new_session()
        self._refresh_sessions()
        self._render_chat()
        self._switch("chat")

    def _select_session(self, event=None):
        selection = self.sessions_list.curselection()
        if not selection:
            return
        target = self.session_ids[selection[0]]
        if self.core.chat_pending and target != self.session:
            self._notice("请先等待或停止当前回复，再切换对话。")
            self._refresh_sessions()
            return
        self.session = target
        self._render_chat()
        self._switch("chat")

    def _render_chat(self, streaming=False):
        self.chat_text.configure(state="normal")
        self.chat_text.delete("1.0", "end")
        messages = self.core.store.messages(self.session)
        if not messages:
            self.chat_text.insert("end", "在这里，和本鱼打个招呼吧。\n", "welcome")
            self.chat_text.insert("end", "本鱼会认真听，也会慢慢记住你。\n想聊今天的小事，还是一起琢磨一个新点子？\n\n", "assistant")
            self.chat_text.insert("end", "这些是小屋的欢迎语。真正的回复来自你配置的本地模型。\n", "hint")
        for message in messages:
            name = self.settings.user_name if message["role"] == "user" else self.settings.character_name
            if message["role"] == "user" and name == "你":
                name = "你"
            self.chat_text.insert("end", f"{name}   {message['created_at'][11:16]}\n", "user_name" if message["role"] == "user" else "name")
            if streaming and message["status"] == "pending":
                self.chat_text.insert("end", message["content"], message["role"])
                continue
            self.chat_text.insert("end", message["content"] + "\n", message["role"])
            if message["status"] != "complete":
                note = {"pending": "正在回复…", "interrupted": "生成已中断，可重试", "failed": "生成未完成，可重试", "erased": "已删除"}.get(message["status"], message["status"])
                self.chat_text.insert("end", f"[{note}]\n", "hint")
        self.chat_text.configure(state="disabled")
        self.chat_text.see("end")

    def _enter(self, event):
        if event.state & 0x0001:
            return None
        self._send()
        return "break"

    def _send(self):
        text = self.input.get("1.0", "end-1c").strip()
        if not text:
            return
        try:
            self.chat_error = ""
            self.core.send(self.session, text)
            self.input.delete("1.0", "end")
            self._set_busy(True)
            self._render_chat()
        except Exception as error:
            self._notice(str(error))

    def _retry(self):
        try:
            self.core.retry(self.session)
            self._set_busy(True)
        except Exception as error:
            self._notice(str(error))

    def _stop_reply(self):
        self.core.cancel_reply()
        self.status.configure(text="正在停止，已收到的内容会保留…")
        self.stop_button.configure(state="disabled")

    def _set_busy(self, busy):
        self.send_button.configure(state="disabled" if busy else "normal")
        self.retry_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if busy else "disabled")

    def _refresh_memories(self):
        self.memory_items = self.core.store.memories()
        self.memory_list.delete(0, "end")
        for item in self.memory_items:
            self.memory_list.insert("end", f"[{item['kind']}] {item['content'][:22]}")
        if not self.memory_items:
            self._new_memory()
        else:
            index = next((i for i, item in enumerate(self.memory_items) if item["id"] == self.current_memory), 0)
            self.memory_list.selection_set(index)
            self._select_memory()

    def _select_memory(self, event=None):
        selection = self.memory_list.curselection()
        if not selection:
            return
        item = self.memory_items[selection[0]]
        self.current_memory = item["id"]
        self.memory_kind.set(item["kind"])
        self.memory_text.delete("1.0", "end")
        self.memory_text.insert("1.0", item["content"])
        sources = self.core.store.message_sources(json.loads(item["source_ids"]))
        text = f"记忆 #{item['id']}\n更新：{item['updated_at']}\n\n"
        text += "\n\n".join(f"消息 #{source['id']} · {source['created_at']}\n{source['content']}" for source in sources) if sources else "由你手动添加或修正，没有自动提取的来源。"
        readonly(self.source_text, text)

    def _new_memory(self):
        self.current_memory = None
        self.memory_kind.set("事实")
        self.memory_text.delete("1.0", "end")
        readonly(self.source_text, "记一件你希望本鱼长期记住的事。\n例如：我希望你称呼我为……")
        self.memory_list.selection_clear(0, "end")

    def _require_idle(self) -> bool:
        if self.core.chat_pending or self.core.maintenance_pending or self.core.operation or not self.core.tasks.empty():
            self._notice("正在处理模型请求或整理数据，请稍后再进行此操作。")
            return False
        return True

    def _save_memory(self):
        if not self._require_idle():
            return
        content = self.memory_text.get("1.0", "end-1c").strip()
        if not content:
            self._notice("先写一点记忆内容吧。")
            return
        if self.current_memory:
            self.core.store.update_memory(self.current_memory, self.memory_kind.get(), content)
        else:
            self.core.store.add_memory(self.memory_kind.get(), content)
        self.core.memory.export_journals()
        self._refresh_memories()
        self.status.configure(text="记忆已保存。下次聊天时，本鱼会参考它。")
        self.core.maintain(backup_now=True)

    def _forget_memory(self):
        if not self.current_memory or not self._require_idle():
            return
        if not messagebox.askyesno("删除记忆", "删除这条记忆，并清除其来源所在轮次的原始消息、相关日志和上下文摘要？\n\n其他无关对话保留。先前的恢复包可能仍包含旧记录，需要你另行清理。", parent=self):
            return
        self.core.store.forget_memory(self.current_memory, erase_sources=True)
        self.core.memory.export_journals()
        self._refresh_memories()
        self._new_memory()
        self._render_chat()
        self._refresh_journals()
        self.status.configure(text="已删除该记忆及其关联来源，旧备份需另外清理。")
        self.core.maintain(backup_now=True)

    def _refresh_journals(self):
        query = self.journal_query.get().strip()
        self.journal_items = self.core.store.search(query, 200) if query else self.core.store.journals()
        self.journal_list.delete(0, "end")
        for item in self.journal_items:
            self.journal_list.insert("end", f"{item['day']} · {item['part']}  {item['title'][:12]}")
        if query and not self.journal_items:
            readonly(self.journal_detail, "没有找到相关日志。\n\n可以换一个关键词或完整日期；尚未归档的内容可先点击“整理尚未归档的对话”。")
        elif self.journal_items:
            self.journal_list.selection_set(0)
            self._select_journal()
        else:
            readonly(self.journal_detail, "这里会留下每一天的对话足迹。\n\n对话较多时自动分段；空闲后自动整理；重新打开应用会补写遗漏的日志。\n\n也可以点击左下方，立即整理今天的来信。")

    def _select_journal(self, event=None):
        selection = self.journal_list.curselection()
        if not selection:
            return
        item = self.journal_items[selection[0]]
        text = f"{item['day']} · 第 {item['part']} 段\n\n{item['title']}\n\n{item['content']}\n\n────────────────────\n原始对话依据\n\n"
        for source in self.core.store.message_sources(json.loads(item["source_ids"])):
            role = "你" if source["role"] == "user" else self.settings.character_name
            text += f"#{source['id']} · {source['created_at'][11:19]} · {role}\n{source['content']}\n\n"
        readonly(self.journal_detail, text)

    def _archive_now(self):
        if self.core.chat_pending:
            self._notice("请先完成当前对话，再整理日志。")
            return
        if self.core.maintain(force=True):
            self.status.configure(text="正在整理日志，本地消息已保存…")
        else:
            self._notice("已有日志整理任务正在进行。")

    def _backup_now(self):
        if not self.settings.backup_dir:
            self._notice("请先选择备份目录并保存设置。可以选 OneDrive 里的专用文件夹。")
            return
        if not self.core.maintain(backup_now=True):
            self._notice("请等待当前任务结束后重试备份。")

    def _update_backup_status(self):
        stamp = self.core.store.meta("last_backup_at")
        error = self.core.store.meta("backup_error")
        if error:
            text = "备份待重试"
        elif not self.settings.backup_dir:
            text = "尚未选择备份目录"
        elif stamp:
            text = f"恢复包已生成 {stamp[11:16]} · 云端同步由 OneDrive 完成"
        else:
            text = "等待首次备份"
        self.backup_status.configure(text=text)
        self.backup_detail.configure(text=("最近备份：" + (stamp or "暂无") + "\n" + (self.core.store.meta("last_backup_file") or "请先选择目录并保存。") + ("\n" + error if error else "")))

    def _save_settings(self):
        if not self._require_idle():
            return
        try:
            values = asdict(self.settings)
            for key, variable in self.setting_vars.items():
                values[key] = int(variable.get()) if isinstance(getattr(self.settings, key), int) else variable.get().strip()
            updated = Settings(**values)
            updated.validate()
            destination = Path(updated.data_dir)
            if destination != Path(self.settings.data_dir).resolve():
                destination.mkdir(parents=True, exist_ok=True)
                if any(destination.iterdir()):
                    raise ValueError("新的工作目录必须为空。若要使用以前的数据，请用“从恢复包恢复”。")
                self.core.store.snapshot(destination / "companion.sqlite3")
            if updated.backup_dir:
                # Verify write access before changing the persisted configuration.
                import tempfile
                Path(updated.backup_dir).mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryFile(dir=updated.backup_dir):
                    pass
            updated.save()
            self.restart = True
            self._close(restarting=True)
        except Exception as error:
            messagebox.showerror("设置未保存", str(error), parent=self)

    def _restore(self):
        if not self._require_idle():
            return
        archive = filedialog.askopenfilename(parent=self, title="选择鲸鱼娘恢复包", filetypes=[("完整恢复包", "whale-friend-*.zip"), ("ZIP", "*.zip")], initialdir=self.settings.backup_dir or str(ROOT))
        if not archive:
            return
        try:
            manifest, _ = Backups.inspect(Path(archive))
            counts = manifest.get("counts", {})
            if not messagebox.askyesno("恢复对话与记忆", f"恢复包时间：{manifest['created_at']}\n消息：{counts.get('messages', '?')} 条\n日志：{counts.get('journals', '?')} 段\n\n将恢复到新的本地目录并切换，当前数据保留。继续吗？", parent=self):
                return
            destination = LOCAL / ("restored-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
            Backups.restore(Path(archive), destination)
            updated = Settings(**asdict(self.settings))
            updated.data_dir = str(destination)
            updated.save()
            self.restart = True
            self._close(restarting=True)
        except Exception as error:
            messagebox.showerror("恢复未完成", str(error), parent=self)

    def _notice(self, text):
        messagebox.showinfo("本鱼的小提示", text, parent=self)

    def _poll(self):
        try:
            while True:
                event = self.core.events.get_nowait()
                kind = event["kind"]
                if kind == "connected":
                    self.model_label = event["model"]
                    self.connection.configure(text="●  " + self.model_label[:28], fg="#4D957E")
                elif kind == "user_saved":
                    self._refresh_sessions()
                elif kind == "reply_start":
                    self.streaming_session = event["session"]
                    self._render_chat(streaming=True)
                    self.status.configure(text="本鱼正在写回信…")
                elif kind == "delta" and event["session"] == self.session:
                    self.chat_text.configure(state="normal")
                    self.chat_text.insert("end", event["text"], "assistant")
                    self.chat_text.configure(state="disabled")
                    self.chat_text.see("end")
                elif kind == "reply_done":
                    self.status.configure(text="来信已保存。还有什么想和本鱼说的？")
                elif kind == "reply_error":
                    self.chat_error = event["message"]
                    self.status.configure(text=event["message"])
                    self.chat_text.configure(state="normal")
                    self.chat_text.insert("end", "\n" + event["message"] + "\n", "hint")
                    self.chat_text.configure(state="disabled")
                elif kind == "chat_finished":
                    self._set_busy(False)
                    self._render_chat()
                elif kind == "status":
                    self.status.configure(text=event["text"])
                elif kind == "maintenance_done":
                    if event["count"]:
                        self.status.configure(text=f"已整理 {event['count']} 段日志，记忆已更新。")
                        if self.page == "journals":
                            self._refresh_journals()
                        if self.page == "memories":
                            self._refresh_memories()
                    elif not self.core.chat_pending:
                        issue = self.chat_error or self.core.store.meta("journal_error") or self.core.store.meta("backup_error")
                        self.status.configure(text=issue or "本地记录已保存 · 本鱼在这里。")
                    self._update_backup_status()
                elif kind == "backup_done":
                    self._update_backup_status()
                elif kind == "maintenance_warning":
                    self.status.configure(text="整理或备份待重试，原始消息已保存在本机。")
                elif kind == "error":
                    self.status.configure(text=event["message"])
                    if event.get("operation") == "连接模型":
                        self.connection.configure(text="●  模型未连接", fg="#B57B55")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(60, self._poll)

    def _tick(self):
        self.core.maintain()
        self._update_backup_status()
        self.after(60_000, self._tick)

    def _close(self, restarting=False):
        if self.core.chat_pending and not restarting:
            if not messagebox.askyesno("关闭小屋", "本鱼还在回复，关闭会停止生成。收到的内容和已保存的消息会保留。继续关闭吗？", parent=self):
                return
        self.core.shutdown()
        self.destroy()
