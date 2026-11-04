"""Bounded streaming WebM playback in Tk, preserving VP9 alpha with libvpx."""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from functools import lru_cache
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

from .animations import IDLE
from .config import ROOT


def _rgba_frames(path):
    import av
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        # FFmpeg's default VP9 decoder discards WebM alpha. Use libvpx and
        # convert the RGBA plane directly; VideoFrame.to_image() defaults RGB.
        decoder = av.CodecContext.create('libvpx-vp9' if stream.codec_context.name=='vp9' else stream.codec_context.name,'r')
        decoder.thread_count = 1
        fps = float(stream.average_rate or 24)
        delay = round(1000/max(1,min(fps,60)))
        for packet in container.demux(stream):
            for frame in decoder.decode(packet):
                rgba = frame.reformat(format='rgba')
                image = Image.frombytes('RGBA',(rgba.width,rgba.height),bytes(rgba.planes[0]),'raw','RGBA',rgba.planes[0].line_size)
                image.putalpha(image.getchannel('A').point([0 if value<=2 else value for value in range(256)]))
                yield image,delay


@lru_cache(maxsize=128)
def _content_bounds(path,modified):
    # One union per clip keeps the pet's size and position steady while moving.
    bounds = None
    for image,_ in _rgba_frames(path):
        box = image.getchannel('A').getbbox()
        if box:
            bounds = box if bounds is None else (min(bounds[0],box[0]),min(bounds[1],box[1]),max(bounds[2],box[2]),max(bounds[3],box[3]))
    return bounds


def video_frames(path,width=600,height=250,*,trim=False):
    bounds = _content_bounds(str(path),Path(path).stat().st_mtime_ns) if trim else None
    for image,delay in _rgba_frames(path):
        if bounds:
            image = image.crop(bounds)
        scale = min(width/image.width,height/image.height)
        dimensions = max(2,round(image.width*scale)),max(2,round(image.height*scale))
        # Scale after RGBA conversion. Some libswscale builds leave
        # alpha artifacts along the left edge when resizing YUVA.
        image = image.resize(dimensions,Image.Resampling.BILINEAR)
        yield image,delay


class ClipDecoder:
    """One decode thread and at most three frames in memory per panel."""
    def __init__(self):
        self.requests = queue.Queue(maxsize=1)
        self.frames = queue.Queue(maxsize=3)
        self.closed = threading.Event()
        self.cancel = threading.Event()
        self.epoch = 0
        self.thread = threading.Thread(target=self._run,daemon=True,name='avatar-decoder')
        self.thread.start()

    def stop_current(self):
        self.cancel.set()
        self.epoch += 1
        for items in (self.requests,self.frames):
            while True:
                try: items.get_nowait()
                except queue.Empty: break

    def play(self,path,width,height,*,trim=False):
        self.stop_current()
        self.cancel = threading.Event()
        self.requests.put_nowait((self.epoch,path,width,height,self.cancel,trim))

    def _put(self,item,cancel):
        while not cancel.is_set() and not self.closed.is_set():
            try:
                self.frames.put(item,timeout=.1)
                return
            except queue.Full:
                pass

    def _run(self):
        while not self.closed.is_set():
            try: epoch,path,width,height,cancel,trim = self.requests.get(timeout=.2)
            except queue.Empty: continue
            try:
                for image,delay in video_frames(path,width,height,trim=trim):
                    if cancel.is_set() or self.closed.is_set(): break
                    self._put((epoch,image,delay,''),cancel)
                self._put((epoch,None,0,'ended'),cancel)
            except Exception:
                self._put((epoch,None,0,'error'),cancel)

    def close(self):
        self.closed.set()
        self.stop_current()


class AvatarPanel(tk.Frame):
    def __init__(self,parent,director,session,height=250,*,bg='#EDF4FD',foreground='#6883A4',show_caption=True,trim=False):
        super().__init__(parent,bg=bg,height=height)
        self.pack_propagate(False)
        self.director,self.session = director,session
        self.decoder = ClipDecoder()
        self.last_event = None
        self.current = None
        self.failed = False
        self.due = 0
        self.show_caption = show_caption
        self.trim = trim
        self.image_label = tk.Label(self,bg=bg,bd=0)
        self.image_label.place(relx=.5,rely=.47 if show_caption else .5,anchor='center')
        self.caption = tk.Label(self,text='本鱼在这里，慢慢说就好。',bg=bg,fg=foreground,font=('',9))
        if show_caption:
            self.caption.place(relx=.5,rely=.94,anchor='center')
        self._fallback()
        self.jobs = [self.after(30,self._pump),self.after(100,self._sync)]

    def _fallback(self):
        try:
            with Image.open(ROOT/'assets/character-reference.png') as image:
                image = image.crop((0,0,540,621)).convert('RGBA')
                image.thumbnail((190,215))
                self.photo = ImageTk.PhotoImage(image)
                self.image_label.configure(image=self.photo)
        except OSError:
            pass

    def play(self,snapshot):
        if not snapshot.get('enabled'):
            self.decoder.stop_current()
            return
        self.current = snapshot
        self.failed = False
        width,height = max(2,self.winfo_width()-8),max(2,self.winfo_height()-(24 if self.show_caption else 8))
        self.decoder.play(self.director.path(snapshot['id']),width,height,trim=self.trim)
        self.due = time.monotonic()
        self.caption.configure(text='本鱼现在 · '+snapshot.get('mood','平静'))

    def preview(self,name):
        if name not in self.director.clips: return
        self.play({'enabled':True,'id':name,'mood':'动作预览','loop':False})

    def _sync(self):
        if self.winfo_viewable():
            snapshot = self.director.snapshot(self.session())
            event = (self.session(),snapshot.get('event'),snapshot.get('enabled'))
            if event!=self.last_event:
                self.last_event = event
                if snapshot.get('enabled'):
                    self.play(snapshot)
                else:
                    self.decoder.stop_current()
                    self._fallback()
                    self.caption.configure(text='本鱼在这里，慢慢说就好。')
        elif self.last_event is not None:
            self.last_event = None
            self.decoder.stop_current()
        self.jobs[1] = self.after(600,self._sync)

    def _pump(self):
        delay = 15
        if time.monotonic()>=self.due:
            try:
                epoch,image,duration,kind = self.decoder.frames.get_nowait()
                if epoch==self.decoder.epoch:
                    if image is not None:
                        self.photo = ImageTk.PhotoImage(image)
                        self.image_label.configure(image=self.photo)
                        self.due = max(self.due+duration/1000,time.monotonic())
                        delay = duration
                    elif kind=='error':
                        self.failed = True
                        self._fallback()
                        self.caption.configure(text='动画暂不可用，请检查 av 依赖；聊天仍可继续。')
                    elif kind=='ended' and self.current and not self.failed:
                        next_clip = self.current if self.current.get('loop') else self.director.snapshot(self.session(),include_reply=False)
                        self.play(next_clip)
            except queue.Empty:
                pass
        self.jobs[0] = self.after(max(5,delay),self._pump)

    def destroy(self):
        for job in self.jobs:
            try: self.after_cancel(job)
            except tk.TclError: pass
        self.decoder.close()
        super().destroy()


class AnimationLibrary(tk.Toplevel):
    def __init__(self,parent,director,session):
        super().__init__(parent)
        self.title('本鱼的动作图鉴')
        self.geometry('960x690')
        self.configure(bg='#F1F6FD')
        self.director,self.selected = director,None
        self.clips = director.catalog()
        left = tk.Frame(self,bg='#FFFFFF')
        left.pack(side='left',fill='y',padx=16,pady=16)
        tk.Label(left,text='每段动画都有自己的小表情',bg='white',pady=10).pack()
        self.list = tk.Listbox(left,width=29,bd=0,exportselection=False)
        self.list.pack(side='left',fill='both',expand=True)
        scroll = ttk.Scrollbar(left,command=self.list.yview)
        scroll.pack(side='right',fill='y')
        self.list.configure(yscrollcommand=scroll.set)
        for clip in self.clips:
            self.list.insert('end',clip['id']+(' · 系统动作' if not clip['model_selectable'] else ''))
        self.list.bind('<<ListboxSelect>>',self._select)
        right = tk.Frame(self,bg='#F1F6FD')
        right.pack(side='left',fill='both',expand=True,padx=(0,16),pady=16)
        self.preview = AvatarPanel(right,director,session,height=250)
        self.preview.pack(fill='x')
        tk.Label(right,text='画面描述（会提供给模型作为选动作的参考）',bg='#F1F6FD').pack(anchor='w',pady=(16,4))
        self.description = tk.Text(right,height=4,wrap='word',bd=0,padx=12,pady=12)
        self.description.pack(fill='x')
        tk.Label(right,text='相关话题（逗号分隔）',bg='#F1F6FD').pack(anchor='w',pady=(12,4))
        self.keywords = tk.Entry(right,bd=0)
        self.keywords.pack(fill='x',ipady=8)
        self.note = tk.Label(right,text='余额、拖拽和睡眠衔接由系统控制，其余动作可随回复选择。',bg='#F1F6FD',fg='#6883A4',wraplength=540,justify='left')
        self.note.pack(anchor='w',pady=12)
        tk.Button(right,text='保存描述并立即生效',command=self._save,bg='#357BE4',fg='white',bd=0,padx=16,pady=10).pack(anchor='e')
        if self.clips:
            self.list.selection_set(0)
            self.after(100,self._select)

    def _select(self,event=None):
        if not self.list.curselection(): return
        self.selected = self.clips[self.list.curselection()[0]]
        self.description.delete('1.0','end')
        self.description.insert('1.0',self.selected['description'])
        self.keywords.delete(0,'end')
        self.keywords.insert(0,'，'.join(self.selected['keywords']))
        self.preview.preview(self.selected['id'])

    def _save(self):
        if not self.selected: return
        try:
            keywords = [x.strip() for x in self.keywords.get().replace('，',',').split(',') if x.strip()]
            self.director.update_description(self.selected['id'],self.description.get('1.0','end-1c'),keywords,self.selected['moods'])
            self.clips = self.director.catalog()
            self.note.configure(text='已保存。下一次聊天会读取新描述；更新插件不会覆盖这份图鉴。')
        except (ValueError,OSError) as error:
            messagebox.showerror('描述未保存',str(error),parent=self)
