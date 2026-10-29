from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path

from .config import Settings
from .model import ModelClient


def main():
    parser = argparse.ArgumentParser(description="鲸鱼娘的海边小屋")
    parser.add_argument("--check-model", action="store_true", help="检查 .env 里的模型连接，不显示密钥")
    parser.add_argument("--smoke-model", action="store_true", help="向配置的模型发送一条简短测试，不写入个人记忆")
    args = parser.parse_args()
    if args.check_model or args.smoke_model:
        try:
            settings = Settings.load()
            client = ModelClient(settings)
            print(json.dumps({"connected": True, "model": client.discover()}, ensure_ascii=False))
            if args.smoke_model:
                response = client.complete([{"role": "system", "content": "你是蓝色鲸鱼娘助手，自称本鱼。简短回答。"}, {"role": "user", "content": "你好，请用一句话打个招呼。"}], max_tokens=128)
                print(response)
        except Exception as error:
            print(str(error), file=sys.stderr)
            return 1
        return 0
    # A loopback socket prevents two desktop instances from sharing a live database.
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.bind(("127.0.0.1", 47631))
    except OSError:
        from .tray import show_existing
        try:
            if show_existing():
                guard.close()
                return 0
        except OSError:
            pass
        import tkinter as tk
        from tkinter import messagebox
        window = tk.Tk()
        window.withdraw()
        messagebox.showinfo("小屋已经打开", "鲸鱼娘的小屋已在运行，请切换到已有窗口。")
        window.destroy()
        guard.close()
        return 1
    try:
        from .app import Desktop
        while True:
            try:
                app = Desktop(Settings.load())
            except Exception as error:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                messagebox.showerror("小屋未能打开", f"{error}\n\n请检查 .env、工作目录权限和 .local/settings.json 配置。")
                root.destroy()
                return 1
            app.mainloop()
            if not app.restart:
                break
    finally:
        guard.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
