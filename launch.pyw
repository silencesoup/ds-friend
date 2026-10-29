"""Windowed Windows entry point; retain diagnostics without a console window."""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


def report_error(message: str) -> None:
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "鲸鱼娘的小屋未能启动", 0x10 | 0x10000)
    else:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("小屋未能启动", message, parent=root)
        root.destroy()


def main() -> int:
    project = Path(__file__).resolve().parent
    os.chdir(project)
    log_path = project / ".local" / "startup.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if log_path.exists() and log_path.stat().st_size > 1_000_000:
            os.replace(log_path, log_path.with_suffix(".previous.log"))
        with log_path.open("a", encoding="utf-8", buffering=1) as log:
            original_out, original_err = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = log
            try:
                print(f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}] 启动小屋")
                if sys.version_info < (3, 11):
                    raise RuntimeError("请安装 Python 3.11 或更高版本，并保留 Tcl/Tk 支持。")
                from whale_friend.__main__ import main as run
                return run()
            except Exception:
                traceback.print_exc()
                report_error(f"请检查 Python 的 Tkinter 支持、.env 和数据目录权限。\n\n详细错误已保存在：\n{log_path}")
                return 1
            finally:
                sys.stdout, sys.stderr = original_out, original_err
    except OSError:
        report_error("无法写入启动日志，请将小屋放到你有读写权限的目录中。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
