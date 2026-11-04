"""Picture library and image viewer; all Tk operations stay on the UI thread."""
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageTk

from .assets import AssetError, FORMATS, MAX_IMAGE_PIXELS
from .config import atomic_write

WHITE, PALE, INK, BLUE = "#FFFFFF", "#F1F6FD", "#233B56", "#327CEC"
IMAGE_TYPES = [("图片和表情包", "*.png *.jpg *.jpeg *.gif *.webp"), ("全部文件", "*.*")]


def action(parent, title, command):
    return tk.Button(parent, text=title, command=command, bg=PALE, fg=INK, relief="flat", padx=12, pady=8, cursor="hand2")


class ImageViewer(tk.Toplevel):
    def __init__(self, parent, identity):
        super().__init__(parent)
        self.library = parent.core.assets
        self.image = None
        self.timer = None
        self.closed = False
        self.configure(bg=WHITE)
        self.title("查看图片")
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda event: self.destroy())
        self.bind("<Destroy>", self._cleanup)
        try:
            asset = self.library.get(identity)
            self.title(asset["title"])
            self.image = Image.open(self.library.path(identity), formats=list(FORMATS))
            if self.image.width * self.image.height > MAX_IMAGE_PIXELS:
                raise AssetError("图片像素过大，无法预览")
            self.label = tk.Label(self, bg=WHITE)
            self.label.pack(padx=15, pady=15)
            note = asset["description"] or "未填写说明。支持视觉的模型可直接理解聊天中发送的图片。"
            tk.Label(self, text=note, fg=INK, bg=WHITE, wraplength=700, justify="left").pack(padx=15, pady=(0, 15))
            self.frame = 0
            self.frames = min(getattr(self.image, "n_frames", 1), 500)
            self._draw()
        except Exception as error:
            self.destroy()
            messagebox.showerror("图片未能打开", str(error), parent=parent)

    def _draw(self):
        if self.closed:
            return
        try:
            self.image.seek(self.frame)
            frame = ImageOps.exif_transpose(self.image).convert("RGBA")
            frame.thumbnail((min(850, self.winfo_screenwidth() - 160), min(650, self.winfo_screenheight() - 240)), Image.Resampling.LANCZOS)
            self.photo = ImageTk.PhotoImage(frame, master=self)
            self.label.configure(image=self.photo)
            if self.frames > 1:
                duration = max(40, min(2000, int(self.image.info.get("duration", 100))))
                self.frame = (self.frame + 1) % self.frames
                self.timer = self.after(duration, self._draw)
        except (OSError, EOFError, ValueError):
            self.label.configure(text="这张动图暂时无法继续播放")

    def _cleanup(self, event):
        if event.widget != self or self.closed:
            return
        self.closed = True
        if self.timer:
            try:
                self.after_cancel(self.timer)
            except tk.TclError:
                pass
        if self.image:
            self.image.close()


class LibraryWindow(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.parent = parent
        self.library = parent.core.assets
        self.current = None
        self.items = []
        self.title("图片与表情包 · 小肥鱼的收藏夹")
        self.geometry("1000x780")
        self.minsize(860, 680)
        self.configure(bg=WHITE)
        self.query = tk.StringVar()
        self.include_archived = tk.BooleanVar(value=False)
        self.name = tk.StringVar()
        self.tags = tk.StringVar()
        self.allow = tk.BooleanVar(value=False)
        left = tk.Frame(self, bg=WHITE, width=280, padx=16, pady=18)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        tk.Label(left, text="图片与表情包", bg=WHITE, fg=INK, font=("", 15, "bold")).pack(anchor="w", pady=(0, 12))
        search = tk.Entry(left, textvariable=self.query, bg=PALE, relief="flat", borderwidth=7)
        search.pack(fill="x")
        search.bind("<Return>", lambda event: self.refresh())
        action(left, "搜索名字、说明或标签", self.refresh).pack(fill="x", pady=8)
        tk.Checkbutton(left, text="包含已移出图库的图片", variable=self.include_archived, command=self.refresh, bg=WHITE).pack(anchor="w")
        self.listbox = tk.Listbox(left, bg=WHITE, fg=INK, selectbackground="#E5EFFF", selectforeground=BLUE, bd=0, highlightthickness=0, exportselection=False)
        self.listbox.pack(fill="both", expand=True, pady=10)
        self.listbox.bind("<<ListboxSelect>>", self._select)
        action(left, "＋ 导入图片", self._import).pack(fill="x")
        right = tk.Frame(self, bg=WHITE, padx=20, pady=18)
        right.pack(side="left", fill="both", expand=True)
        self.preview = tk.Label(right, text="选择一张图片查看", bg=PALE, fg=INK, height=12, cursor="hand2")
        self.preview.pack(fill="x")
        self.preview.bind("<Button-1>", lambda event: self._view())
        self.details = tk.Label(right, text="PNG / JPEG / GIF / WebP · 单张最多 20 MB", bg=WHITE, fg="#7B8DA3", anchor="w", justify="left", wraplength=640)
        self.details.pack(fill="x", pady=(6, 10))
        tk.Label(right, text="名字", bg=WHITE, fg=INK).pack(anchor="w")
        tk.Entry(right, textvariable=self.name, bg=PALE, relief="flat", borderwidth=7).pack(fill="x", pady=(4, 10))
        tk.Label(right, text="说明（可选；表情复用时用于判断用途）", bg=WHITE, fg=INK).pack(anchor="w")
        self.description = tk.Text(right, bg=PALE, fg=INK, height=4, wrap="word", bd=0, padx=10, pady=8)
        self.description.pack(fill="x", pady=(4, 10))
        tk.Label(right, text="用途标签（用逗号分隔，如：开心、点头、撒娇）", bg=WHITE, fg=INK).pack(anchor="w")
        tk.Entry(right, textvariable=self.tags, bg=PALE, relief="flat", borderwidth=7).pack(fill="x", pady=(4, 8))
        tk.Checkbutton(right, text="允许本鱼在聊天中选作表情包使用", variable=self.allow, bg=WHITE, fg=INK, anchor="w").pack(anchor="w", pady=(0, 8))
        self.source = tk.Label(right, text="", bg=WHITE, fg="#7B8DA3", justify="left", wraplength=640, anchor="w")
        self.source.pack(fill="x", pady=(0, 10))
        buttons = tk.Frame(right, bg=WHITE)
        buttons.pack(fill="x", pady=(0, 12))
        for title, callback in (("保存说明", self._save), ("添加到聊天", self._add), ("另存原图", self._export)):
            action(buttons, title, callback).pack(side="left", padx=(0, 8))
        self.archive_button = action(right, "移出图库（保留历史）", self._archive)
        self.archive_button.pack(anchor="w")
        self.refresh()

    def refresh(self, select_id=None):
        self.items = self.library.list(self.query.get(), self.include_archived.get())
        self.listbox.delete(0, "end")
        for item in self.items:
            status = "已移出" if item["archived"] else "可复用" if item["allow_reuse"] else "仅保存"
            self.listbox.insert("end", f"[{status}] {item['title']}")
        if self.items:
            target = select_id or self.current
            index = next((i for i, item in enumerate(self.items) if item["id"] == target), 0)
            self.listbox.selection_set(index)
            self._select()
        else:
            self.current = None
            self.name.set("")
            self.tags.set("")
            self.allow.set(False)
            self.description.delete("1.0", "end")
            self.preview.configure(image="", text="还没有图片，先导入一张吧", height=12)
            self.source.configure(text="")

    def _select(self, event=None):
        selected = self.listbox.curselection()
        if not selected:
            return
        row = self.items[selected[0]]
        self.current = row["id"]
        self.name.set(row["title"])
        self.tags.set("，".join(row["tags"]))
        self.allow.set(bool(row["allow_reuse"]))
        self.description.delete("1.0", "end")
        self.description.insert("1.0", row["description"])
        try:
            self.photo = ImageTk.PhotoImage(self.library.thumbnail(self.current, (560, 220)), master=self)
            self.preview.configure(image=self.photo, text="", height=0)
        except AssetError as error:
            self.preview.configure(image="", text=str(error), height=8)
        animation = f" · {row['frames']} 帧动图" if row["frames"] > 1 else ""
        self.details.configure(text=f"图片 #{row['id']} · {row['width']}×{row['height']} · {row['byte_size']/1024:.0f} KB{animation} · 点击预览查看大图")
        origins = {"user": "用户导入", "clipboard": "剪贴板", "web": "网页收藏", "assistant": "助手收藏"}
        self.source.configure(text="来源：" + origins.get(row["source_kind"], row["source_kind"]) + ("\n" + row["source_url"] if row["source_url"] else ""))
        self.archive_button.configure(text="恢复到图库" if row["archived"] else "移出图库（保留历史）")

    def _view(self):
        if self.current:
            ImageViewer(self.parent, self.current)

    def _import(self):
        paths = filedialog.askopenfilenames(parent=self, title="收藏图片和表情包", filetypes=IMAGE_TYPES)
        latest = None
        errors = []
        for path in paths:
            try:
                latest = self.library.import_file(Path(path))["id"]
            except (AssetError, OSError) as error:
                errors.append(Path(path).name + "：" + str(error))
        if latest:
            self.parent._assets_changed()
            self.query.set("")
            self.refresh(latest)
        if errors:
            messagebox.showinfo("部分图片未导入", "\n".join(errors[:5]), parent=self)

    def _save(self):
        if not self.current or not self.parent._require_idle():
            return
        try:
            self.library.update(self.current, self.name.get(), self.description.get("1.0", "end-1c"), self.tags.get(), self.allow.get())
            self.parent._assets_changed()
            self.refresh(self.current)
        except AssetError as error:
            messagebox.showinfo("图片说明未保存", str(error), parent=self)

    def _add(self):
        if self.current:
            try:
                self.parent._queue_image(self.current)
                self.parent._show_window()
                self.parent._switch("chat")
                self.parent.input.focus_set()
            except AssetError as error:
                messagebox.showinfo("暂时不能添加", str(error), parent=self)

    def _archive(self):
        if self.current and self.parent._require_idle():
            row = self.library.get(self.current)
            self.library.archive(self.current, not bool(row["archived"]))
            self.parent._assets_changed()
            self.refresh()

    def _export(self):
        if not self.current:
            return
        row = self.library.get(self.current)
        target = filedialog.asksaveasfilename(parent=self, title="保存图片原件", initialfile=row["original_name"], defaultextension=Path(row["relative_path"]).suffix)
        if target:
            try:
                atomic_write(Path(target), self.library.path(self.current).read_bytes())
            except (AssetError, OSError) as error:
                messagebox.showerror("图片未导出", str(error), parent=self)
