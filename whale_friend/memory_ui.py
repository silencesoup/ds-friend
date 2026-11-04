"""Native views for core memory, topic cards, activation traces and version history."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .media_ui import action

WHITE, INK, PALE = "#FFFFFF", "#233B56", "#F1F6FD"


def show_text(parent, title, content):
    window = tk.Toplevel(parent)
    window.title(title)
    window.geometry("760x620")
    text = tk.Text(window, wrap="word", bg=WHITE, fg=INK, padx=22, pady=18, bd=0)
    scroll = ttk.Scrollbar(window, command=text.yview)
    scroll.pack(side="right", fill="y")
    text.configure(yscrollcommand=scroll.set)
    text.pack(fill="both", expand=True)
    text.insert("1.0", content)
    text.configure(state="disabled")
    return window


def show_activation(parent):
    hub = parent.core.memory.hub
    data = hub.overview(parent.session)
    current = data["activation"]
    if not current:
        show_text(parent, "本轮想起了什么", "这段会话还没有生成自动记忆加载记录。发一条消息后可查看。")
        return
    names = {row["id"]: row["title"] for row in data["topics"]}
    text = "最近加载时间：" + current["created_at"] + "\n\n核心档案编号：" + str(current["core_ids"]) + "\n自动激活主题：" + "、".join(names.get(key, key) for key in current["topic_ids"]) + "\n实际放入上下文的记忆编号：" + str(current["memory_ids"]) + "\n\n下列为这些编号目前的内容，不是模型内部思考。\n\n"
    if current.get('recall_reason'):
        text += '语义关联线索（并非当前原因）：'+current['recall_reason']+'\n\n'
    for row in data["records"]:
        if row["id"] in current["memory_ids"]:
            text += f"#{row['id']} [{row['kind']}] {row['content']}\n\n"
    show_text(parent, "本轮想起了什么", text)


def show_versions(parent, identity):
    rows = parent.core.store.rows("SELECT * FROM memory_versions WHERE memory_id=? ORDER BY id DESC", (identity,))
    text = "修订保留旧版本作为历史依据，不自动用于当前回答。删除这条记忆时，其修订历史也一并移除。\n\n"
    text += "\n\n".join(f"{row['valid_from']} → {row['valid_to']}\n[{row['kind']}] {row['content']}\n来源消息：{row['source_ids']}" for row in rows) or "这条记忆还没有修订历史。"
    show_text(parent, "记忆修订历史", text)


class HubWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent, self.hub = parent, parent.core.memory.hub
        self.title("核心档案与主题卡")
        self.geometry("920x760")
        self.configure(bg=WHITE)
        tabs = ttk.Notebook(self)
        tabs.pack(fill="both", expand=True, padx=16, pady=16)
        core = tk.Frame(tabs, bg=WHITE, padx=18, pady=18)
        topics = tk.Frame(tabs, bg=WHITE, padx=18, pady=18)
        tabs.add(core, text="核心小档案")
        tabs.add(topics, text="主题卡与别名")
        changes=tk.Frame(tabs,bg=WHITE,padx=18,pady=18)
        tabs.add(changes,text='状态变化与核对')
        tk.Label(changes,text='明确改变现状时，旧记忆进入历史，新记忆保留为当前；原文不删除。\n含糊、假设或归属不清的变化只作为候选，可核对来源后确认，自动更新也可恢复旧状态。',justify='left',bg=WHITE,fg=INK,wraplength=830).pack(anchor='w')
        self.changes_list=tk.Listbox(changes,height=6,exportselection=False,bd=0,bg=PALE,fg=INK)
        self.changes_list.pack(fill='x',pady=12)
        self.changes_list.bind('<<ListboxSelect>>',self.select_change)
        self.change_text=tk.Text(changes,bg=PALE,fg=INK,wrap='word',height=10,bd=0,padx=12,pady=12)
        self.change_text.pack(fill='both',expand=True)
        change_actions=tk.Frame(changes,bg=WHITE)
        change_actions.pack(side='bottom',fill='x',pady=(12,0),before=self.change_text)
        self.accept_change=action(change_actions,'确认更新',lambda:self.resolve_change('accept'))
        self.accept_change.pack(side='left')
        self.reject_change=action(change_actions,'不视为替代',lambda:self.resolve_change('reject'))
        self.reject_change.pack(side='left',padx=8)
        self.undo_change=action(change_actions,'恢复旧状态',lambda:self.resolve_change('undo'))
        self.undo_change.pack(side='left')
        action(change_actions,'刷新',self.refresh).pack(side='right')
        tk.Label(core, text="每轮自动带上：固定记忆、称呼、稳定相处偏好和少量长期关键条目。\n在记忆编辑区勾选“固定为核心档案”即可加入；最多手动固定 12 条。", justify="left", bg=WHITE, fg=INK).pack(anchor="w")
        self.core_text = tk.Text(core, bg=PALE, fg=INK, wrap="word", bd=0, padx=15, pady=15)
        self.core_text.pack(fill="both", expand=True, pady=14)
        action(core, "刷新核心档案", self.refresh).pack(anchor="w")
        self.listbox = tk.Listbox(topics, height=6, exportselection=False, bd=0, bg=PALE, fg=INK)
        self.listbox.pack(fill="x")
        self.listbox.bind("<<ListboxSelect>>", self.select)
        self.title_value = tk.StringVar()
        self.aliases = tk.StringVar()
        tk.Label(topics, text="主题名字", bg=WHITE, fg=INK).pack(anchor="w", pady=(12, 3))
        tk.Entry(topics, textvariable=self.title_value, bg=PALE, relief="flat", bd=7).pack(fill="x")
        tk.Label(topics, text="相关说法（逗号分隔，会用于自动分类与话题激活）", bg=WHITE, fg=INK).pack(anchor="w", pady=(10, 3))
        tk.Entry(topics, textvariable=self.aliases, bg=PALE, relief="flat", bd=7).pack(fill="x")
        self.summary = tk.Text(topics, height=9, bg=PALE, fg=INK, wrap="word", bd=0, padx=12, pady=12)
        self.summary.pack(fill="both", expand=True, pady=12)
        buttons = tk.Frame(topics, bg=WHITE)
        buttons.pack(fill="x")
        action(buttons, "保存主题与别名", self.save).pack(side="left")
        action(buttons, "＋ 新建主题", self.new).pack(side="left", padx=8)
        action(buttons, "后台归纳", self.organize).pack(side="left")
        self.current = None
        self.refresh()

    def refresh(self):
        self.data = self.hub.overview()
        labels={'pending':'待核对','applied':'已更新','rejected':'不视为替代','undone':'已恢复旧状态'}
        self.changes_list.delete(0,'end')
        for row in self.data['changes']:
            self.changes_list.insert('end',f"{labels.get(row['status'],row['status'])} · #{row['previous_id']} → #{row['newer_id']}")
        if self.data['changes']:
            self.changes_list.selection_set(0)
        self.select_change()
        self.core_text.configure(state="normal")
        self.core_text.delete("1.0", "end")
        self.core_text.insert("end", "\n\n".join(f"#{row['id']} [{row['kind']}] {'固定' if row['pinned'] else '自动常驻'}\n{row['content']}" for row in self.data["core"]) or "还没有核心条目，可以在收藏夹固定重要记忆。")
        self.core_text.configure(state="disabled")
        self.listbox.delete(0, "end")
        for topic in self.data["topics"]:
            self.listbox.insert("end", f"{topic['title']} · {topic['count']} 条" + (" · 待归纳" if topic["dirty"] else ""))
        index = next((i for i, row in enumerate(self.data["topics"]) if row["id"] == self.current), 0)
        self.listbox.selection_set(index)
        self.select()

    def select_change(self,event=None):
        selected=self.changes_list.curselection()
        row=self.data['changes'][selected[0]] if selected else None
        self.accept_change.configure(state='normal' if row and row['status']=='pending' else 'disabled')
        self.reject_change.configure(state='normal' if row and row['status']=='pending' else 'disabled')
        self.undo_change.configure(state='normal' if row and row['status']=='applied' else 'disabled')
        text='还没有状态变化候选。对话中的明确更新会在后台归纳时检查，原始记忆一直保留。'
        if row:
            status={'pending':'待核对','applied':'已更新','rejected':'不视为替代','undone':'已恢复旧状态'}.get(row['status'],row['status'])
            text=f"状态：{status}\n发现时间：{row['created_at']}\n\n以前 · #{row['previous_id']}\n{row['previous_content']}\n\n后来 · #{row['newer_id']}\n{row['newer_content']}\n\n整理说明（需核对来源）：{row['explanation']}\n\n用户原文依据：\n"
            for source in self.parent.core.store.message_sources(row['evidence_ids']):
                text+=f"\n消息#{source['id']} · {source['created_at']}\n{source['content']}\n"
        self.change_text.configure(state='normal')
        self.change_text.delete('1.0','end')
        self.change_text.insert('1.0',text)
        self.change_text.configure(state='disabled')

    def resolve_change(self,decision):
        if not self.parent._require_idle(): return
        selected=self.changes_list.curselection()
        if not selected: return
        try:
            self.hub.signals.resolve(self.data['changes'][selected[0]]['id'],decision)
            self.hub.index()
            self.parent._refresh_memories()
            self.parent.core.maintain(backup_now=True)
            self.refresh()
        except ValueError as error:
            messagebox.showinfo('状态未更改',str(error),parent=self)

    def select(self, event=None):
        selected = self.listbox.curselection()
        if not selected:
            return
        topic = self.data["topics"][selected[0]]
        self.current = topic["id"]
        self.title_value.set(topic["title"])
        self.aliases.set("，".join(topic["aliases"]))
        text = ("主题摘要（模型归纳，请核对来源）：\n" + topic["summary"] if topic["summary"] else "尚未生成摘要，自动激活仍会直接读取原始记忆。")
        text += "\n\n来源记忆编号：" + str(topic["source_ids"]) + "\n全部关联编号：" + str(topic["memory_ids"])
        for row in self.data["records"]:
            if row["id"] in topic["memory_ids"]:
                text += f"\n\n#{row['id']} [{row['kind']}] {row['content']}"
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", text)
        self.summary.configure(state="disabled")

    def save(self):
        if not self.parent._require_idle():
            return
        try:
            self.hub.save_topic(self.title_value.get(), self.aliases.get(), self.current)
            self.parent.core.maintain(backup_now=True)
            self.refresh()
        except ValueError as error:
            messagebox.showinfo("主题未保存", str(error), parent=self)

    def new(self):
        if not self.parent._require_idle():
            return
        title = simpledialog.askstring("新的主题", "主题名字（例如：考研计划）", parent=self)
        if title:
            try:
                self.current = self.hub.save_topic(title, [title])
                self.parent.core.maintain(backup_now=True)
                self.refresh()
            except ValueError as error:
                messagebox.showinfo("主题未保存", str(error), parent=self)

    def organize(self):
        if self.parent.core.maintain(force=True, backup_now=True):
            messagebox.showinfo("正在后台归纳", "已安排日志与主题归纳。完成后点刷新可查看，原始记忆始终保留。", parent=self)
