from __future__ import annotations

import json
import io
import os
import queue
import sys
import tkinter as tk
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from PIL import Image, ImageGrab, ImageTk

from .backup import Backups
from .config import ConnectionConfig, LOCAL, ROOT, Settings
from .service import Companion
from .tray import UnreadMessages, WindowsTray
from .assets import AssetError, MAX_ATTACHMENTS, MAX_IMAGE_PIXELS, copy_media
from .memory_actions import KINDS, receipt_label

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
    def __init__(self, settings: Settings, core: Companion | None = None):
        super().__init__()
        self.settings = settings
        try:
            self.core = core or Companion(settings)
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
        self.proactive_session = None
        self.unread = UnreadMessages()
        self.tray = None
        self.hidden_to_tray = False
        self.exiting = False
        self.pending_images = []
        self.chat_images = []
        self.library_window = None
        self.mobile_server = None
        self.memory_hub_window = None
        self.pet_runtime = None
        self.pet_update_busy = False
        self.pet_release = None
        self.pet_update_trial = False
        self.base_title = f"{settings.character_name} · 鲸鱼娘的海边小屋"
        self.title(self.base_title)
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
        self._setup_background_window()
        self._start_mobile()
        self.bind("<FocusIn>", self._on_focus, add="+")
        self.after(80, self._poll)
        self.after(1500, self._tick)
        self.core.connect()
        self.core.maintain()
        if self.settings.pet_enabled:
            self.after(400, self._start_pet)

    def _setup_background_window(self):
        menu = tk.Menu(self)
        house = tk.Menu(menu, tearoff=False)
        house.add_command(label="显示小屋", command=self._show_window)
        house.add_command(label="图片与表情包", command=self._open_library)
        house.add_command(label="显示桌宠", command=self._start_pet)
        house.add_command(label="隐藏桌宠", command=self._stop_pet)
        house.add_command(label="隐藏到托盘" if sys.platform == "win32" else "最小化到 Dock", command=self._close)
        house.add_separator()
        house.add_command(label="退出小屋", command=self._exit_app)
        menu.add_cascade(label="小屋", menu=house)
        self.configure(menu=menu)
        if sys.platform == "win32":
            tray = WindowsTray(self.settings.character_name)
            if tray.start():
                self.tray = tray
            else:
                tray.close()
                self.status.configure(text="系统托盘暂不可用，点关闭将最小化；可从“小屋”菜单退出。")

    def _reading(self, session):
        # Use actual visibility, including maximized windows and restores outside the tray.
        if self.state() not in ("normal", "zoomed") or not self.winfo_viewable() or self.page != "chat" or self.session != session:
            return False
        if self.tray:
            return self.tray.foreground(self.winfo_id())
        focused = self.focus_displayof()
        return focused is not None and focused.winfo_toplevel() == self

    def _update_unread(self):
        count = self.unread.count
        if self.tray:
            self.tray.set_unread(count)
        self.title(self.base_title + (f" · {count} 条未读" if count else ""))
        self._refresh_sessions()

    def _incoming(self, session):
        self.unread.receive(session, reading=self._reading(session))
        self._update_unread()

    def _on_focus(self, event=None):
        if not self.exiting and self.session in self.unread.sessions and self._reading(self.session):
            self.unread.read(self.session)
            self._update_unread()

    def _show_window(self):
        target = self.unread.latest
        self.hidden_to_tray = False
        self.deiconify()
        self.lift()
        self.focus_force()
        if self.tray:
            self.tray.focus(self.winfo_id())
        # Do not change the conversation under a draft or a running reply.
        if target and not self.core.chat_pending and not self.input.get("1.0", "end-1c").strip() and not self.pending_images:
            self.session = target
            self._switch("chat")
            self._render_chat()
        self.after(100, self._on_focus)

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
        sidebar_pet = sys.platform == 'win32'
        portrait_bg = NAVY if sidebar_pet else '#EAF2FF'
        portrait_fg = '#A9BED6' if sidebar_pet else '#4D6E98'
        portrait = tk.Frame(self.sidebar, bg=portrait_bg)
        portrait.pack(side="bottom", fill="x", padx=0 if sidebar_pet else 16, pady=(0, 12 if sidebar_pet else 18), before=self.sessions_list)
        if sidebar_pet:
            from .avatar_ui import AvatarPanel
            self.sidebar_avatar = AvatarPanel(portrait,self.core.animations,lambda:self.session,height=290,
                                              bg=NAVY,foreground=portrait_fg,show_caption=False,trim=True)
            self.sidebar_avatar.pack(fill='x')
        else:
            art_path = ROOT / "assets" / "character-reference.png"
            if art_path.exists():
                self.reference = tk.PhotoImage(file=str(art_path))
                # Clip the first view; the supplied reference stays unmodified.
                self.portrait = tk.PhotoImage()
                self.portrait.tk.call(self.portrait, "copy", self.reference, "-from", 0, 0, 540, 621, "-subsample", 8, 8)
                canvas = tk.Canvas(portrait, width=202, height=82, bg=WHITE, bd=0, highlightthickness=0)
                canvas.pack(fill="x")
                canvas.create_image(67, 2, image=self.portrait, anchor="nw")
        self.mood_label = label(portrait, "本鱼现在 · 平静", 9, portrait_fg, portrait_bg)
        self.mood_label.pack(pady=(8, 0))
        label(portrait, "本鱼在这里，慢慢说就好。", 9, portrait_fg, portrait_bg).pack(pady=(3, 8))
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
        self.proactive_button = button(header, "本鱼的新来信 →", self._open_proactive)
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
        if sys.platform != 'win32':
            from .avatar_ui import AvatarPanel
            self.avatar_panel = AvatarPanel(parent,self.core.animations,lambda:self.session,height=230)
            self.avatar_panel.pack(fill='x',padx=32,pady=(0,10))
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
        self.input.bind("<Control-v>", self._paste_image)
        if sys.platform == "darwin":
            self.input.bind("<Command-v>", self._paste_image)
        self.attachment_bar = tk.Frame(composer, bg=WHITE)
        bar = tk.Frame(composer, bg=WHITE)
        bar.pack(fill="x", padx=16, pady=(0, 12))
        button(bar, "发图片", self._choose_images).pack(side="left", padx=(0, 6))
        button(bar, "图库", self._open_library).pack(side="left", padx=(0, 12))
        button(bar, "动作图鉴", self._open_animation_library).pack(side="left",padx=(0,8))
        label(bar, "Enter 发送 · Shift+Enter 换行", 9, MUTED).pack(side="left")
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
        button(left, "核心档案与主题卡", self._open_memory_hub).pack(fill="x", pady=(0, 6))
        button(left, "本轮想起了什么", self._show_memory_activation).pack(fill="x", pady=(0, 8))
        self.memory_list = tk.Listbox(left, bg=WHITE, fg=INK, selectbackground="#E5EFFF", selectforeground=BLUE, bd=0, highlightthickness=0, activestyle="none", exportselection=False)
        self.memory_list.pack(fill="both", expand=True)
        self.memory_list.bind("<<ListboxSelect>>", self._select_memory)
        button(left, "＋ 手动记一件事", self._new_memory).pack(fill="x", pady=(12, 0))
        label(right, "长期记忆", 16, bold=True).pack(anchor="w")
        label(right, "可修正、可删除；自动提取的内容请核对来源。", 9, MUTED, wraplength=430, justify="left").pack(anchor="w", pady=(5, 18))
        self.memory_kind = tk.StringVar(value="事实")
        kinds = ttk.Combobox(right, textvariable=self.memory_kind, values=KINDS, state="readonly", width=16)
        kinds.pack(anchor="w", pady=(0, 10))
        self.memory_text = tk.Text(right, height=5, bg=PALE, fg=INK, wrap="word", bd=0, padx=12, pady=12, undo=True)
        self.memory_text.pack(fill="x")
        self.memory_pin = tk.BooleanVar(value=False)
        self.memory_active = tk.BooleanVar(value=True)
        tk.Checkbutton(right, text="固定为核心档案（每轮自动带上）", variable=self.memory_pin, bg=WHITE, fg=INK).pack(anchor="w", pady=(8, 0))
        tk.Checkbutton(right, text="当前有效（取消后保留历史，不自动用于回复）", variable=self.memory_active, bg=WHITE, fg=INK).pack(anchor="w")
        self.memory_topic = tk.StringVar(value="自动分类")
        self.memory_topic_box = ttk.Combobox(right, textvariable=self.memory_topic, state="readonly")
        self.memory_topic_box.pack(fill="x", pady=(6, 0))
        from .memory_signals import LEVELS
        self.memory_importance=tk.StringVar(value='自动判断')
        label(right,'重要程度（你的选择优先于自动判断）',9,MUTED).pack(anchor='w',pady=(6,0))
        ttk.Combobox(right,textvariable=self.memory_importance,values=('自动判断',)+LEVELS,state='readonly').pack(fill='x',pady=(6,0))
        actions = tk.Frame(right, bg=WHITE)
        actions.pack(fill="x", pady=12)
        button(actions, "保存记忆", self._save_memory, True).pack(side="left")
        button(actions, "删除…", self._forget_memory).pack(side="left", padx=10)
        button(actions, "修订历史", self._show_memory_versions).pack(side="left")
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
        label(form, "模型连接", 16, bold=True).pack(anchor="w")
        label(form, "在这里修改 API 地址、密钥与模型名；保存后写回仓库 .env，并重新连接。", 10, MUTED, wraplength=700, justify="left").pack(anchor="w", pady=(7, 14))
        self.connection_vars = {key: tk.StringVar() for key in ("base_url", "api_key", "model")}
        self.show_api_key = tk.BooleanVar(value=False)
        for key, title in (("base_url", "API 地址 / Base URL"), ("api_key", "API 密钥（本地服务无需密钥时可留空）"), ("model", "模型名称（留空则自动发现）")):
            label(form, title, 10).pack(anchor="w", pady=(0, 6))
            row = tk.Frame(form, bg=WHITE)
            row.pack(fill="x", pady=(0, 13))
            entry = tk.Entry(row, textvariable=self.connection_vars[key], bg=PALE, fg=INK, relief="flat", borderwidth=8, show="*" if key == "api_key" else "")
            entry.pack(side="left", fill="x", expand=True)
            if key == "api_key":
                self.api_key_entry = entry
                tk.Checkbutton(row, text="显示", variable=self.show_api_key, command=lambda: self.api_key_entry.configure(show="" if self.show_api_key.get() else "*"), bg=WHITE, fg=INK).pack(side="right", padx=(8, 0))
        connection_actions = tk.Frame(form, bg=WHITE)
        connection_actions.pack(fill="x", pady=(0, 8))
        self.test_connection_button = button(connection_actions, "测试填写的连接", self._test_connection)
        self.test_connection_button.pack(side="left")
        self.test_vision_button = button(connection_actions, "测试填写的视觉接口", lambda: self._test_connection(vision=True))
        self.test_vision_button.pack(side="left", padx=(10, 0))
        button(connection_actions, "从 .env 重新读取", self._reload_connection).pack(side="left", padx=10)
        self.connection_detail = label(form, "", 9, MUTED, wraplength=700, justify="left")
        self.connection_detail.pack(anchor="w", pady=(0, 10))
        label(form, "测试会发送一条简短请求，不保存为聊天；保存设置后才切换日常聊天。\n密钥只保存在本机 .env，不进入记忆备份。启动环境变量优先于 .env。", 9, MUTED, wraplength=700, justify="left").pack(anchor="w", pady=(0, 14))
        self._reload_connection(initial=True)
        label(form, "识图模式", 10, INK).pack(anchor="w", pady=(6, 4))
        vision_mode = tk.StringVar(value=self.settings.vision_mode)
        self.setting_vars["vision_mode"] = vision_mode
        ttk.Combobox(form, textvariable=vision_mode, values=("auto", "on", "off"), state="readonly", width=18).pack(anchor="w")
        self.vision_detail = label(form, "auto：收到图片时先用测试图检测；on：直接识图；off：仅发送图片说明。\n支持视觉时会把当前和近期附图发送给你填写的模型服务；动图发送静态首帧。", 9, MUTED, wraplength=700, justify="left")
        self.vision_detail.pack(anchor="w", pady=(5, 12))
        for variable in self.connection_vars.values():
            variable.trace_add("write", self._connection_edited)
        tk.Frame(form, height=1, bg=LINE).pack(fill="x", pady=10)
        label(form, "桌面上的小肥鱼 · dsh-pet", 16, bold=True).pack(anchor="w", pady=(5, 12))
        for key, text in (("pet_enabled", "随小屋启动桌宠"), ("pet_fixed", "固定位置，只播放原地动作")):
            variable = tk.BooleanVar(value=getattr(self.settings, key))
            self.setting_vars[key] = variable
            tk.Checkbutton(form, text=text, variable=variable, bg=WHITE, fg=INK).pack(anchor="w", pady=3)
        self._setting_field(form, "pet_size", "桌宠尺寸（240–900，默认 462）")
        self._setting_field(form, "pet_plugin_dir", "dsh-pet 插件目录（留空自动寻找本机安装）", browse=True)
        self._setting_field(form, "pet_electron_path", "Electron 可执行文件（留空自动寻找）")
        button(form, "选择 Electron 程序", self._choose_pet_electron).pack(anchor="w", pady=(0, 8))
        pet_actions = tk.Frame(form, bg=WHITE)
        pet_actions.pack(fill="x", pady=7)
        button(pet_actions, "启动 / 更新桌宠", self._start_pet).pack(side="left")
        button(pet_actions, "隐藏桌宠", self._stop_pet).pack(side="left", padx=8)
        for key, text in (("animation_enabled", "启用角色动画与模型动作选择"), ("pet_voice_enabled", "朗读桌宠回复（Edge 在线语音）")):
            variable = tk.BooleanVar(value=getattr(self.settings,key))
            self.setting_vars[key] = variable
            tk.Checkbutton(form,text=text,variable=variable,bg=WHITE,fg=INK,anchor="w").pack(anchor="w",pady=(5,4))
        label(form, "音色：晓伊（活泼） / 晓晓（通用） / 云希 / 云健", 9, MUTED).pack(anchor="w")
        voice = tk.StringVar(value=self.settings.pet_voice_name)
        self.setting_vars['pet_voice_name'] = voice
        ttk.Combobox(form,textvariable=voice,values=('zh-CN-XiaoyiNeural','zh-CN-XiaoxiaoNeural','zh-CN-YunxiNeural','zh-CN-YunjianNeural'),state='readonly',width=28).pack(anchor="w",pady=4)
        button(form,"应用音色并试听",self._preview_pet_voice).pack(anchor="w",pady=4)
        label(form,"开启后，回复文本会发送到微软 Edge 在线服务合成语音；本地缓存最多 100 条 / 100 MB。\n右键桌宠 → 查看余额，查询当前模型服务；本地模型不支持时会显示说明。",9,MUTED,wraplength=700,justify="left").pack(anchor="w",pady=5)
        updates = tk.Frame(form,bg=WHITE)
        updates.pack(fill="x",pady=7)
        button(updates,"检查插件更新",lambda:self._pet_update('check')).pack(side="left")
        self.pet_install_button = button(updates,"安装兼容更新",lambda:self._pet_update('install'))
        self.pet_install_button.pack(side="left",padx=8)
        self.pet_install_button.configure(state="disabled")
        button(updates,"回退上一版本",lambda:self._pet_update('rollback')).pack(side="left")
        self.pet_update_detail = label(form,"更新来自 npm 官方 dsh-pet；当前适配 0.4.x。更新失败保留原版，独立于 DSH 与记忆存储。",9,MUTED,wraplength=700,justify="left")
        self.pet_update_detail.pack(anchor="w",pady=5)
        self.pet_detail = label(form, "右键桌宠可对话、选择动作或睡觉；聊天、图片、记忆与小屋共用。\n关闭主窗口到托盘后桌宠仍运行；从“小屋 → 退出小屋”退出时一起关闭。\n动画来自 PC2005-cloud/dsh-pet，本机插件素材按原许可使用。", 9, MUTED, wraplength=700, justify="left")
        self.pet_detail.pack(anchor="w", pady=(5, 14))
        tk.Frame(form, height=1, bg=LINE).pack(fill="x", pady=10)
        label(form, "手机连接 · 同一份记忆", 16, bold=True).pack(anchor="w", pady=(5, 12))
        mobile_enabled = tk.BooleanVar(value=self.settings.mobile_enabled)
        self.setting_vars["mobile_enabled"] = mobile_enabled
        tk.Checkbutton(form, text="允许手机通过同一 Wi-Fi 连接这台电脑", variable=mobile_enabled, bg=WHITE, fg=INK, anchor="w").pack(anchor="w", pady=(0, 10))
        self._setting_field(form, "mobile_port", "手机连接端口（默认 8765）")
        self.mobile_detail = label(form, "保存设置后，手机连接地址与配对码会显示在这里。", 9, MUTED, wraplength=700, justify="left")
        self.mobile_detail.pack(anchor="w", pady=(0, 8))
        self.mobile_code = tk.StringVar(value="尚未开启")
        code = tk.Entry(form, textvariable=self.mobile_code, state="readonly", readonlybackground=PALE, fg=INK, relief="flat", font=(FONT, 18), width=18)
        code.pack(anchor="w", pady=(0, 10))
        phone_actions = tk.Frame(form, bg=WHITE)
        phone_actions.pack(fill="x", pady=(0, 8))
        button(phone_actions, "复制手机地址", self._copy_mobile_url).pack(side="left")
        button(phone_actions, "电脑预览手机页", self._preview_mobile).pack(side="left", padx=8)
        button(phone_actions, "更换配对码…", self._rotate_mobile_pairing).pack(side="left")
        label(form, "聊天、记忆、日志和图库直接共用电脑上的记录；OneDrive 继续备份。\n电脑小屋需保持运行，关闭到托盘后仍可连接；退出或休眠会断开。仅用于可信的局域网，不需要开放路由器端口。", 9, MUTED, wraplength=700, justify="left").pack(anchor="w", pady=(0, 14))
        tk.Frame(form, height=1, bg=LINE).pack(fill="x", pady=10)
        label(form, "数据住在哪里", 16, bold=True).pack(anchor="w")
        label(form, "工作数据库放在本机；完整恢复包放进你选择的 OneDrive 文件夹。", 10, MUTED, wraplength=680, justify="left").pack(anchor="w", pady=(7, 16))
        self._setting_field(form, "data_dir", "本地工作数据目录", browse=True)
        self._setting_field(form, "backup_dir", "备份目录（可选 OneDrive 中的专用文件夹）", browse=True)
        label(form, "备份包含原始对话、图片原件、图库说明、记忆、上下文摘要与日志，不包含 .env 或 API 密钥。\n生成恢复包后仍需 OneDrive 完成同步；应用不能确认云端同步状态。", 9, MUTED, wraplength=720, justify="left").pack(anchor="w", pady=(0, 14))
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
        label(form, "一起慢慢熟悉", 16, bold=True).pack(anchor="w", pady=(5, 12))
        for key, title in (("proactive_enabled", "允许本鱼选择合适的时机主动来信"), ("adaptive_enabled", "记住互动语气，并从明确反馈学习相处偏好")):
            variable = tk.BooleanVar(value=getattr(self.settings, key))
            self.setting_vars[key] = variable
            tk.Checkbutton(form, text=title, variable=variable, bg=WHITE, fg=INK, activebackground=WHITE, selectcolor=PALE, anchor="w").pack(anchor="w", pady=5)
        label(form, "主动来信使用浮动时机：结合话题是否适合延续、你说要忙的时间与回应节奏来选，到了候选时间也可以不打扰。\n应用需保持运行；未获回应时不连续追问；输入框有草稿时先保持安静。", 9, MUTED, wraplength=700, justify="left").pack(anchor="w", pady=(6, 16))
        self._setting_field(form, "proactive_min_minutes", "候选时机最短等待（分钟）")
        self._setting_field(form, "proactive_max_minutes", "候选时机最长等待（分钟；不保证到点发送）")
        self._setting_field(form, "proactive_cooldown_minutes", "两封主动来信的最短间隔（分钟）")
        self._setting_field(form, "proactive_daily_limit", "每天主动来信最多几次")
        self._setting_field(form, "quiet_start_hour", "夜间免打扰开始（本机时间，0–23 点）")
        self._setting_field(form, "quiet_end_hour", "夜间免打扰结束（起止相同则关闭此时段）")
        label(form, "相处偏好保存在“记忆收藏夹”，可查看来源并修改。情绪只影响语气，随时间缓和；不会因为你没回复而扣好感或变得生气。", 9, MUTED, wraplength=700, justify="left").pack(anchor="w", pady=(6, 12))
        tk.Frame(form, height=1, bg=LINE).pack(fill="x", pady=15)
        label(form, "模型与记忆", 16, bold=True).pack(anchor="w", pady=(5, 16))
        memory_tools = tk.BooleanVar(value=self.settings.memory_tools_enabled)
        self.setting_vars["memory_tools_enabled"] = memory_tools
        tk.Checkbutton(form, text="允许本鱼按对话语义查询、新增和修订收藏夹", variable=memory_tools, bg=WHITE, fg=INK, anchor="w").pack(anchor="w", pady=(0, 6))
        label(form, "开启后每轮多一次记忆工具判断请求，能理解“再试试”这类接续语；关闭后保留只读记忆参考与每日归档。", 9, MUTED, wraplength=700, justify="left").pack(anchor="w", pady=(0, 14))
        consolidation = tk.BooleanVar(value=self.settings.memory_consolidation_enabled)
        self.setting_vars["memory_consolidation_enabled"] = consolidation
        tk.Checkbutton(form, text="空闲时后台归纳主题卡（原始记忆保留）", variable=consolidation, bg=WHITE, fg=INK, anchor="w").pack(anchor="w", pady=(0, 12))
        evolution=tk.BooleanVar(value=self.settings.memory_evolution_enabled)
        self.setting_vars['memory_evolution_enabled']=evolution
        tk.Checkbutton(form,text='明确状态变化自动转为新旧版本（含糊变化仅待核对）',variable=evolution,bg=WHITE,fg=INK,anchor='w').pack(anchor='w',pady=(0,12))
        self._setting_field(form, "context_tokens", "模型上下文容量（tokens，请按实际部署填写）")
        self._setting_field(form, "reply_tokens", "每次回复上限（tokens）")
        self._setting_field(form, "timeout_seconds", "请求超时（秒）")
        self._setting_field(form, "idle_minutes", "对话空闲多少分钟后整理日志")
        self._setting_field(form, "journal_chunk_chars", "当日对话达到多少字符后分段归档")
        label(form, "上下文长度采用保守估算，并留出回复空间。\n保存设置后会重新打开窗口；改变数据目录会先复制当前数据，原目录保留。", 9, MUTED, wraplength=710, justify="left").pack(anchor="w", pady=(8, 16))

    def _choose_pet_electron(self):
        path = filedialog.askopenfilename(parent=self, title="选择 Electron 可执行文件")
        if path:
            self.setting_vars['pet_electron_path'].set(path)

    def _start_pet(self):
        from .pet import PetRuntime
        try:
            if self.pet_runtime:
                self.pet_runtime.close()
                self.pet_runtime = None
            values = asdict(self.settings)
            for key in ('pet_plugin_dir','pet_electron_path','pet_size','pet_fixed','pet_voice_enabled','pet_voice_name'):
                value = self.setting_vars[key].get()
                values[key] = int(value) if key == 'pet_size' else bool(value) if key in ('pet_fixed','pet_voice_enabled') else value.strip()
            options = Settings(**values)
            options.validate()
            self.pet_runtime = PetRuntime(self.core, options, self.session)
            self.pet_runtime.start()
            self.setting_vars['pet_enabled'].set(True)
            self.pet_detail.configure(text="桌宠已启动，正在加载透明动画。右键可对话、选择动作或睡觉。\n与当前会话共用角色、记忆和日志；保存设置后可随小屋自动启动。", fg=MUTED)
        except (ValueError, OSError) as error:
            if self.pet_runtime:
                self.pet_runtime.close()
                self.pet_runtime = None
            self.pet_detail.configure(text="桌宠暂未启动：" + str(error), fg="#BA5B49")
            if self.pet_update_trial:
                self.pet_update_trial = False
                self._pet_update('rollback')

    def _preview_pet_voice(self):
        if not self.setting_vars['pet_voice_enabled'].get():
            self.pet_detail.configure(text="先勾选“朗读桌宠回复”，再点击试听。",fg=MUTED)
            return
        self._start_pet()
        if self.pet_runtime:
            # Let the renderer finish subscribing. This never creates a chat row.
            runtime = self.pet_runtime
            self.after(1500,lambda:runtime.bridge.write('main','say',{'text':'Deep！Deep！本鱼的声音来啦，今天也想陪你一起摸鱼。','duration':15000,'voiceTo':'desktop'}) if self.pet_runtime is runtime else None)

    def _open_animation_library(self):
        from .avatar_ui import AnimationLibrary
        AnimationLibrary(self,self.core.animations,lambda:self.session)

    def _pet_update(self,action):
        import threading
        from .pet_updates import PetUpdates
        from .pet_support import find_plugin
        if self.pet_update_busy:
            return
        if action == 'install' and not self.pet_release:
            return
        try:
            current = self.pet_runtime.root if self.pet_runtime else find_plugin(self.setting_vars['pet_plugin_dir'].get())
        except ValueError:
            current = None
        self.pet_update_busy = True
        self.pet_install_button.configure(state='disabled')
        self.pet_update_detail.configure(text={'check':'正在检查 npm 插件版本…','install':'正在下载、校验并安装桌宠更新…','rollback':'正在回退上一版本…'}[action],fg=MUTED)
        release = self.pet_release
        def work():
            manager = PetUpdates()
            try:
                if action == 'check':
                    result = manager.check(current)
                elif action == 'install':
                    result = str(manager.install(release,current,self.settings))
                else:
                    result = str(manager.rollback(self.settings))
                self.core.emit('pet_update_result',action=action,success=True,result=result)
            except Exception as error:
                self.core.emit('pet_update_result',action=action,success=False,message=str(error))
        threading.Thread(target=work,daemon=True,name='pet-updater').start()

    def _pet_update_result(self,event):
        self.pet_update_busy = False
        if not event['success']:
            self.pet_update_detail.configure(text='插件操作未完成：'+event['message'],fg='#BA5B49')
        elif event['action'] == 'check':
            self.pet_release = event['result']
            result = self.pet_release
            text = f"当前版本：{result['current'] or '未安装'}；最新兼容版本：{result['version']}。"
            text += ' 可以安装更新。' if result['available'] else ' 当前已是最新兼容版本。'
            if result['latest'] != result['version']:
                text += f"\n上游最新 {result['latest']} 尚未适配，将保留当前兼容系列。"
            self.pet_update_detail.configure(text=text,fg=MUTED)
        else:
            # Persist only plugin selection; leave all other unsaved form values alone.
            self.setting_vars['pet_plugin_dir'].set('')
            self.settings.pet_plugin_dir = ''
            self.core.animations.root = None
            self.core.animations.reload()
            try:
                self.settings.save()
            except OSError:
                self.pet_update_detail.configure(text='插件已切换，但设置未能保存；下次启动请选择自动寻找。',fg='#BA5B49')
            else:
                self.pet_update_detail.configure(text='插件已更新，可随时回退上一版本。' if event['action']=='install' else '已回退上一版本。',fg=MUTED)
            self.pet_release = None
            self.pet_update_trial = event['action']=='install'
            self._start_pet()
        self.pet_install_button.configure(state='normal' if self.pet_release and self.pet_release.get('available') else 'disabled')

    def _stop_pet(self):
        if self.pet_runtime:
            self.pet_runtime.close()
            self.pet_runtime = None
        self.setting_vars['pet_enabled'].set(False)
        self.pet_detail.configure(text="桌宠已隐藏，小屋聊天继续运行。可从“小屋 → 显示桌宠”重新打开。", fg=MUTED)

    def _start_mobile(self):
        if not self.settings.mobile_enabled:
            self.mobile_detail.configure(text="手机连接尚未开启。勾选上面的开关并保存设置后，查看地址和配对码。")
            return
        try:
            from .mobile import MobileServer
            self.mobile_server = MobileServer(self.core).start()
            self.mobile_code.set(self.mobile_server.pairing.code)
            urls = self.mobile_server.urls()
            self.mobile_detail.configure(text="手机浏览器打开（与电脑连接同一 Wi-Fi）：\n" + "\n".join(url for url in urls if "127.0.0.1" not in url) + "\n电脑预览：" + f"http://127.0.0.1:{self.mobile_server.port}" + "\n手机无法连接时，请检查本机防火墙是否允许此端口的专用网络访问。", fg=MUTED)
        except Exception:
            self.mobile_server = None
            self.mobile_code.set("连接服务未启动")
            self.mobile_detail.configure(text="手机连接未能启动。请检查端口是否被占用，并安装 requirements.txt 中的依赖后重开小屋。电脑聊天仍可使用。", fg="#BA5B49")

    def _copy_mobile_url(self):
        if not self.mobile_server:
            self._notice("先开启手机连接并保存设置。")
            return
        self.clipboard_clear()
        self.clipboard_append(self.mobile_server.urls()[0])
        self.status.configure(text="手机连接地址已复制，请在同一 Wi-Fi 的手机浏览器打开。")

    def _preview_mobile(self):
        if self.mobile_server:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{self.mobile_server.port}")
        else:
            self._notice("先开启手机连接并保存设置。")

    def _rotate_mobile_pairing(self):
        if self.mobile_server and messagebox.askyesno("更换手机配对码", "更换后，已连接的手机都需要重新配对。聊天与记忆保留。继续吗？", parent=self):
            self.mobile_server.pairing.rotate()
            self.mobile_code.set(self.mobile_server.pairing.code)

    def _reload_connection(self, initial=False):
        try:
            connection = ConnectionConfig.load(legacy_model=self.settings.model if initial else "")
            for key, variable in self.connection_vars.items():
                variable.set(getattr(connection, key))
            self.show_api_key.set(False)
            self.api_key_entry.configure(show="*")
            text = "已读取连接配置；修改后点击“保存设置并重新打开”生效。"
            if initial and self.settings.model:
                text += "\n当前有旧版模型覆盖；从 .env 重新读取可采用新模型，保存后统一使用 .env。"
            self.connection_detail.configure(text=text, fg=MUTED)
        except Exception:
            self.connection_detail.configure(text="未能读取 .env，请检查文件权限和 UTF-8 编码；也可直接填写。", fg="#BA5B49")

    def _connection_draft(self):
        connection = ConnectionConfig(**{key: variable.get() for key, variable in self.connection_vars.items()})
        connection.validate()
        return connection

    def _connection_edited(self, *args):
        self.connection_detail.configure(text="连接表单已修改；可先测试，保存设置后应用到聊天。", fg=MUTED)

    def _test_connection(self, vision=False):
        if not self._require_idle():
            return
        try:
            self.core.test_connection(self._connection_draft(), vision=True) if vision else self.core.test_connection(self._connection_draft())
            self.connection_test_values = tuple(variable.get() for variable in self.connection_vars.values())
            self.test_connection_button.configure(state="disabled")
            self.test_vision_button.configure(state="disabled")
            self.connection_detail.configure(text="正在用测试图片验证填写的接口…" if vision else "正在测试填写的连接，请稍候…", fg=MUTED)
        except ValueError as error:
            self.connection_detail.configure(text=str(error), fg="#BA5B49")

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
        self._on_focus()

    def _refresh_sessions(self):
        sessions = self.core.store.sessions()
        self.session_ids = [item["id"] for item in sessions]
        self.sessions_list.delete(0, "end")
        for item in sessions:
            marker = "● " if item["id"] in self.unread.sessions else "  "
            self.sessions_list.insert("end", marker + item["title"])
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
        if self.pet_runtime:
            self.pet_runtime.session = self.session
        self.chat_text.configure(state="normal")
        self.chat_text.delete("1.0", "end")
        self.chat_images = []
        messages = self.core.store.messages(self.session)
        if not messages:
            self.chat_text.insert("end", "在这里，和本鱼打个招呼吧。\n", "welcome")
            self.chat_text.insert("end", "本鱼会认真听，也会慢慢记住你。\n想聊今天的小事，还是一起琢磨一个新点子？\n\n", "assistant")
            self.chat_text.insert("end", "这些是小屋的欢迎语。真正的回复来自你配置的本地模型。\n", "hint")
        for message_index, message in enumerate(messages):
            name = self.settings.user_name if message["role"] == "user" else self.settings.character_name
            if message.get("proactive"):
                name += " · 主动来信"
            if message["role"] == "user" and name == "你":
                name = "你"
            self.chat_text.insert("end", f"{name}   {message['created_at'][11:16]}\n", "user_name" if message["role"] == "user" else "name")
            if streaming and message["status"] == "pending":
                self.chat_text.insert("end", message["content"], message["role"])
                continue
            self.chat_text.insert("end", message["content"] + "\n", message["role"])
            receipt = receipt_label(message.get("memory_action"))
            if receipt:
                self.chat_text.insert("end", "[" + receipt + "]\n", "hint")
            for asset in message.get("attachments", []):
                tag = f"image-{message['id']}-{asset['id']}"
                start = self.chat_text.index("end-1c")
                if message_index >= len(messages) - 40:
                    try:
                        photo = ImageTk.PhotoImage(self.core.assets.thumbnail(asset["id"]), master=self)
                        self.chat_images.append(photo)
                        self.chat_text.image_create("end", image=photo, padx=5, pady=5)
                        self.chat_text.insert("end", "\n")
                    except AssetError:
                        self.chat_text.insert("end", "[图片文件暂不可用]\n", "hint")
                self.chat_text.insert("end", f"[图片 #{asset['id']} · {asset['title']} · 点击查看]\n", "hint")
                self.chat_text.tag_add(tag, start, "end-1c")
                self.chat_text.tag_bind(tag, "<Button-1>", lambda event, identity=asset["id"]: self._view_image(identity))
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
        if not text and not self.pending_images:
            return
        try:
            self.chat_error = ""
            self.core.send(self.session, text, attachments=list(self.pending_images))
            self.input.delete("1.0", "end")
            self.pending_images.clear()
            self._refresh_image_queue()
            self._set_busy(True)
            self._render_chat()
        except Exception as error:
            self._notice(str(error))

    def _queue_image(self, identity):
        asset = self.core.assets.get(identity)
        if asset["archived"]:
            raise AssetError("请先在图库中恢复这张图片")
        self.core.assets.path(identity)
        if identity not in self.pending_images:
            if len(self.pending_images) >= MAX_ATTACHMENTS:
                raise AssetError(f"每条消息最多选择 {MAX_ATTACHMENTS} 张图片")
            self.pending_images.append(identity)
        self._refresh_image_queue()

    def _refresh_image_queue(self):
        for child in self.attachment_bar.winfo_children():
            child.destroy()
        if not self.pending_images:
            self.attachment_bar.pack_forget()
            return
        self.attachment_bar.pack(fill="x", before=self.input, padx=12, pady=(8, 0))
        for identity in self.pending_images:
            asset = self.core.assets.get(identity)
            chip = tk.Frame(self.attachment_bar, bg=PALE)
            chip.pack(side="left", padx=(0, 6))
            tk.Button(chip, text=asset["title"][:14], command=lambda i=identity: self._view_image(i), bg=PALE, fg=INK, bd=0, padx=7, pady=5).pack(side="left")
            tk.Button(chip, text="×", command=lambda i=identity: self._remove_image(i), bg=PALE, fg=MUTED, bd=0, padx=5).pack(side="left")

    def _remove_image(self, identity):
        if identity in self.pending_images:
            self.pending_images.remove(identity)
        self._refresh_image_queue()

    def _choose_images(self):
        from .media_ui import IMAGE_TYPES
        paths = filedialog.askopenfilenames(parent=self, title="给本鱼发图片", filetypes=IMAGE_TYPES)
        if len(paths) + len(self.pending_images) > MAX_ATTACHMENTS:
            self._notice(f"每条消息最多选择 {MAX_ATTACHMENTS} 张图片；更多图片可先导入图库。")
            return
        for path in paths:
            try:
                self._queue_image(self.core.assets.import_file(Path(path))["id"])
            except (AssetError, OSError) as error:
                self._notice(str(error))
        if paths:
            self._assets_changed()

    def _paste_image(self, event=None):
        if sys.platform not in ("win32", "darwin"):
            return None
        try:
            content = ImageGrab.grabclipboard()
            if isinstance(content, Image.Image):
                if content.width * content.height > MAX_IMAGE_PIXELS:
                    raise AssetError("剪贴板图片超过 2500 万像素，请先缩小")
                if len(self.pending_images) >= MAX_ATTACHMENTS:
                    raise AssetError(f"每条消息最多选择 {MAX_ATTACHMENTS} 张图片")
                buffer = io.BytesIO()
                content.save(buffer, format="PNG")
                self._queue_image(self.core.assets.import_bytes(buffer.getvalue(), original_name="剪贴板.png", source_kind="clipboard")["id"])
            elif isinstance(content, list):
                paths = [Path(path) for path in content if Path(path).suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp")]
                if not paths:
                    return None
                if len(paths) + len(self.pending_images) > MAX_ATTACHMENTS:
                    raise AssetError(f"每条消息最多选择 {MAX_ATTACHMENTS} 张图片")
                for path in paths:
                    self._queue_image(self.core.assets.import_file(path, source_kind="clipboard")["id"])
            else:
                return None
            self._assets_changed()
            return "break"
        except (AssetError, OSError) as error:
            self._notice(str(error))
            return "break"

    def _open_library(self):
        from .media_ui import LibraryWindow
        if self.library_window is not None and self.library_window.winfo_exists():
            self.library_window.refresh()
            self.library_window.lift()
        else:
            self.library_window = LibraryWindow(self)

    def _view_image(self, identity):
        from .media_ui import ImageViewer
        ImageViewer(self, identity)

    def _assets_changed(self):
        self.status.configure(text="图片已保存在工作数据目录，可在图库填写说明和用途。")
        self.core.maintain(backup_now=True)

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
        self.memory_items = self.core.memory.hub.records(include_inactive=True)
        self.memory_topics = self.core.memory.hub.topics()
        self.memory_topic_box.configure(values=["自动分类"] + [row["title"] for row in self.memory_topics])
        self.memory_list.delete(0, "end")
        for item in self.memory_items:
            marker = "★ " if item["pinned"] else "历史 · " if not item["active"] else "重要 · " if item['importance']>=2 else ""
            self.memory_list.insert("end", marker + f"[{item['kind']}] {item['content'][:22]}")
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
        self.memory_pin.set(bool(item["pinned"]))
        self.memory_active.set(bool(item["active"]))
        from .memory_signals import LEVELS
        self.memory_importance.set(LEVELS[item['importance']] if item['importance_manual'] else '自动判断')
        topic_ids = item["topics_override"] or []
        self.memory_topic.set(next((row["title"] for row in self.memory_topics if row["id"] in topic_ids), "自动分类"))
        sources = self.core.store.message_sources(json.loads(item["source_ids"]))
        text = f"记忆 #{item['id']}\n更新：{item['updated_at']}\n\n"
        text += '重要程度：'+LEVELS[item['importance']]+('（你指定）' if item['importance_manual'] else '（可修正的自动标签）')+'\n联想线索：'+'、'.join(item['recall_cues'])+'\n\n'
        text += "\n\n".join(f"消息 #{source['id']} · {source['created_at']}\n{source['content']}" for source in sources) if sources else "由你手动添加或修正，没有自动提取的来源。"
        readonly(self.source_text, text)

    def _new_memory(self):
        self.current_memory = None
        self.memory_kind.set("事实")
        self.memory_pin.set(False)
        self.memory_active.set(True)
        self.memory_topic.set("自动分类")
        self.memory_importance.set('自动判断')
        self.memory_text.delete("1.0", "end")
        readonly(self.source_text, "记一件你希望本鱼长期记住的事。\n例如：我希望你称呼我为……")
        self.memory_list.selection_clear(0, "end")

    def _require_idle(self) -> bool:
        if self.core.chat_pending or self.core.maintenance_pending or self.core.proactive_pending or self.core.reflection_pending or self.core.operation or not self.core.tasks.empty():
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
        try:
            topic_ids = next(([row["id"]] for row in self.memory_topics if row["title"] == self.memory_topic.get()), None)
            self.core.memory.hub.validate_annotation(self.current_memory, bool(self.memory_pin.get()), bool(self.memory_active.get()), topic_ids)
            if self.current_memory:
                old = self.core.store.rows("SELECT * FROM memories WHERE id=?", (self.current_memory,))[0]
                annotations = self.core.store.rows("SELECT topics_override FROM memory_annotations WHERE memory_id=?", (self.current_memory,))
                previous_topics = json.loads(annotations[0]["topics_override"]) if annotations and annotations[0]["topics_override"] else None
                if topic_ids and previous_topics and topic_ids[0] in previous_topics:
                    topic_ids = previous_topics
                if old["content"] != content or old["kind"] != self.memory_kind.get():
                    self.core.store.update_memory(self.current_memory, self.memory_kind.get(), content)
            else:
                self.current_memory = self.core.store.add_memory(self.memory_kind.get(), content)
                if self.current_memory is None:
                    self.current_memory = self.core.store.rows("SELECT id FROM memories WHERE content=?", (content,))[0]["id"]
            self.core.memory.hub.annotate(self.current_memory, bool(self.memory_pin.get()), bool(self.memory_active.get()), topic_ids)
            from .memory_signals import LEVELS
            value=self.memory_importance.get()
            self.core.memory.hub.signals.set_importance(self.current_memory,None if value=='自动判断' else LEVELS.index(value))
        except ValueError as error:
            self._notice(str(error))
            return
        self.core.memory.export_journals()
        self._refresh_memories()
        self.status.configure(text="记忆已保存。下次聊天时，本鱼会参考它。")
        self.core.maintain(backup_now=True)

    def _open_memory_hub(self):
        from .memory_ui import HubWindow
        if self.memory_hub_window and self.memory_hub_window.winfo_exists():
            self.memory_hub_window.refresh()
            self.memory_hub_window.lift()
        else:
            self.memory_hub_window = HubWindow(self)

    def _show_memory_activation(self):
        from .memory_ui import show_activation
        show_activation(self)

    def _show_memory_versions(self):
        if self.current_memory:
            from .memory_ui import show_versions
            show_versions(self, self.current_memory)

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
            connection = self._connection_draft()
            values = asdict(self.settings)
            for key, variable in self.setting_vars.items():
                if isinstance(getattr(self.settings, key), bool):
                    values[key] = bool(variable.get())
                else:
                    values[key] = int(variable.get()) if isinstance(getattr(self.settings, key), int) else variable.get().strip()
            values["model"] = ""  # Migrate the old override; .env is the single editable source.
            updated = Settings(**values)
            updated.validate()
            destination = Path(updated.data_dir)
            if destination != Path(self.settings.data_dir).resolve():
                destination.mkdir(parents=True, exist_ok=True)
                if any(destination.iterdir()):
                    raise ValueError("新的工作目录必须为空。若要使用以前的数据，请用“从恢复包恢复”。")
                self.core.store.snapshot(destination / "companion.sqlite3")
                copy_media(destination / "companion.sqlite3", Path(self.settings.data_dir), destination)
            if updated.backup_dir:
                # Verify write access before changing the persisted configuration.
                import tempfile
                Path(updated.backup_dir).mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryFile(dir=updated.backup_dir):
                    pass
            connection.save()
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
        if self.exiting:
            return
        if self.tray:
            try:
                while True:
                    action = self.tray.actions.get_nowait()
                    if action == "exit":
                        self._exit_app()
                        if self.exiting:
                            return
                    elif action == "show":
                        self._show_window()
            except queue.Empty:
                pass
        try:
            while True:
                event = self.core.events.get_nowait()
                kind = event["kind"]
                if kind == "connection_test_done":
                    self.test_connection_button.configure(state="normal")
                    self.test_vision_button.configure(state="normal")
                    if tuple(variable.get() for variable in self.connection_vars.values()) != self.connection_test_values:
                        self.connection_detail.configure(text="表单在测试期间已修改，请重新测试当前填写的连接。", fg=MUTED)
                    elif event["success"] and event.get("vision"):
                        self.connection_detail.configure(text=event['message'] + "\n模型：" + event['model'] + "\n测试不改变当前配置；保存设置后应用所选识图模式。", fg="#4D957E" if event['vision_available'] else "#BA5B49")
                    elif event["success"]:
                        self.connection_detail.configure(text="测试成功 · 模型：" + event["model"] + "\n点击“保存设置并重新打开”应用到聊天。", fg="#4D957E")
                    else:
                        self.connection_detail.configure(text="测试未通过：" + event["message"], fg="#BA5B49")
                elif kind == "vision_status":
                    self.vision_detail.configure(text=event['text'])
                elif kind == 'pet_error':
                    self.pet_detail.configure(text=event['message'], fg="#BA5B49")
                    self.status.configure(text=event['message'])
                    if self.pet_update_trial:
                        self.pet_update_trial = False
                        self._pet_update('rollback')
                elif kind == 'pet_update_result':
                    self._pet_update_result(event)
                elif kind == 'pet_hide':
                    self._stop_pet()
                elif kind == 'pet_reload':
                    self._start_pet()
                elif kind == 'pet_open':
                    self._show_window()
                    target = event.get('session')
                    if target and not self.core.chat_pending and not self.input.get('1.0','end-1c').strip() and not self.pending_images:
                        self.session = target
                        self._refresh_sessions()
                        self._render_chat()
                    self._switch('chat')
                elif kind == "connected":
                    self.model_label = event["model"]
                    self.connection.configure(text="●  " + self.model_label[:28], fg="#4D957E")
                elif kind == "user_saved":
                    self._refresh_sessions()
                    if event["session"] == self.session:
                        self._render_chat()
                elif kind == "data_changed":
                    self._refresh_sessions()
                    if self.memory_hub_window and self.memory_hub_window.winfo_exists():
                        self.memory_hub_window.refresh()
                    if self.page == "memories":
                        self._refresh_memories()
                    elif self.page == "journals":
                        self._refresh_journals()
                elif kind == "memory_action_done":
                    if self.memory_hub_window and self.memory_hub_window.winfo_exists():
                        self.memory_hub_window.refresh()
                    if event["session"] == self.session:
                        self._render_chat()
                    if self.page == "memories":
                        self._refresh_memories()
                    saved_notice = receipt_label(event["result"])
                    if saved_notice:
                        self.status.configure(text=saved_notice)
                elif kind == "memory_organized":
                    if self.memory_hub_window and self.memory_hub_window.winfo_exists():
                        self.memory_hub_window.refresh()
                    self.status.configure(text=f"已归纳 {event['count']} 张主题卡，原始记忆保留。")
                elif kind == "reply_start":
                    self.streaming_session = event["session"]
                    self._set_busy(True)
                    self._render_chat(streaming=True)
                    self.status.configure(text="本鱼正在写回信…")
                elif kind == "delta" and event["session"] == self.session:
                    self.chat_text.configure(state="normal")
                    self.chat_text.insert("end", event["text"], "assistant")
                    self.chat_text.configure(state="disabled")
                    self.chat_text.see("end")
                elif kind == "reply_done":
                    self._incoming(event["session"])
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
                elif kind == "presence_updated":
                    self.mood_label.configure(text="本鱼现在 · " + event["mood"])
                    if self.page == "memories":
                        self._refresh_memories()
                elif kind == "proactive_done":
                    self._incoming(event["session"])
                    self._refresh_sessions()
                    if event["session"] == self.session and self.page == "chat":
                        self._render_chat()
                    else:
                        self.proactive_session = event["session"]
                        self.proactive_button.pack(side="right", padx=(0, 12))
                    self.status.configure(text="本鱼发来一封新的主动来信，不用急着回复。")
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
            # Windows activation can precede or omit Tk's FocusIn event. Reconcile
            # unread state regardless of how the user returned to this window.
            self._on_focus()
            self.after(60, self._poll)

    def _tick(self):
        composing = bool(self.input.get("1.0", "end-1c").strip() or self.pending_images)
        if not self.core.maybe_proactive(composing=composing):
            self.core.maintain()
        self.mood_label.configure(text="本鱼现在 · " + self.core.presence.mood()["mood"])
        self._update_backup_status()
        self.after(60_000, self._tick)

    def _open_proactive(self):
        if self.core.chat_pending:
            self._notice("先等当前回复完成，再查看本鱼的来信。")
            return
        if self.proactive_session:
            self.session = self.proactive_session
            self._refresh_sessions()
            self._render_chat()
            self._switch("chat")
        self.proactive_session = None
        self.proactive_button.pack_forget()

    def _close(self, restarting=False):
        if restarting:
            self._exit_app(restarting=True)
            return
        if self.tray and self.tray.available:
            self.hidden_to_tray = True
            self.withdraw()
        else:
            # Dock/taskbar fallback keeps background work alive if no tray is available.
            self.iconify()

    def _exit_app(self, restarting=False):
        if self.exiting:
            return
        if self.core.chat_pending and not restarting:
            self._show_window()
            if not messagebox.askyesno("退出小屋", "本鱼还在回复，退出会停止生成。收到的内容和已保存的消息会保留。继续退出吗？", parent=self):
                return
        self.exiting = True
        if self.tray:
            self.tray.close()
        if self.mobile_server:
            self.mobile_server.close()
        if self.pet_runtime:
            self.pet_runtime.close()
            self.pet_runtime = None
        self.core.shutdown()
        self.destroy()
