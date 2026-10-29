"""Windows notification-area icon with a dedicated native message loop.

No Tk calls occur on the native thread. Commands are delivered through a queue.
Imports remain safe on macOS; the Windows APIs are loaded only when requested.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import queue
import sys
import threading
from ctypes import wintypes as wt

from .config import ROOT

WM_APP = 0x8000
WM_TRAY = WM_APP + 1
WM_UNREAD = WM_APP + 2
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_TIMER = 0x0113
NIN_SELECT = 0x0400
NIN_KEYSELECT = 0x0401
WM_CONTEXTMENU = 0x007B
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
NIM_ADD, NIM_MODIFY, NIM_DELETE, NIM_SETVERSION = 0, 1, 2, 4
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_SHOWTIP = 1, 2, 4, 0x80
SHOW_MESSAGE_NAME = "WhaleFriend.ShowWindow.v1"
INSTANCE_WINDOW = "WhaleFriend.Tray." + hashlib.sha256(str(ROOT).casefold().encode()).hexdigest()[:16]


class UnreadMessages:
    def __init__(self):
        self.sessions: dict[str, int] = {}

    @property
    def count(self) -> int:
        return sum(self.sessions.values())

    @property
    def latest(self) -> str | None:
        return next(reversed(self.sessions), None)

    def receive(self, session: str, reading=False) -> None:
        if reading:
            self.read(session)
        else:
            count = self.sessions.pop(session, 0)
            self.sessions[session] = count + 1

    def read(self, session: str) -> None:
        self.sessions.pop(session, None)


class _WinAPI:
    def __init__(self):
        if sys.platform != "win32":
            raise OSError("Windows tray APIs are unavailable on this platform")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wt.UINT), ("lpfnWndProc", self.WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH),
                        ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR)]

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wt.DWORD), ("Data2", wt.WORD), ("Data3", wt.WORD), ("Data4", wt.BYTE * 8)]

        class NOTIFYICONDATA(ctypes.Structure):
            _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                        ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                        ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                        ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                        ("guidItem", GUID), ("hBalloonIcon", wt.HICON)]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                        ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                        ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]

        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 1)]

        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wt.BOOL), ("xHotspot", wt.DWORD), ("yHotspot", wt.DWORD), ("hbmMask", wt.HBITMAP), ("hbmColor", wt.HBITMAP)]

        self.WNDCLASS, self.NOTIFYICONDATA = WNDCLASS, NOTIFYICONDATA
        self.BITMAPINFO, self.ICONINFO = BITMAPINFO, ICONINFO
        declarations = [
            (self.kernel, "GetModuleHandleW", [wt.LPCWSTR], wt.HMODULE),
            (self.user, "RegisterClassW", [ctypes.POINTER(WNDCLASS)], wt.ATOM),
            (self.user, "UnregisterClassW", [wt.LPCWSTR, wt.HINSTANCE], wt.BOOL),
            (self.user, "CreateWindowExW", [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID], wt.HWND),
            (self.user, "DefWindowProcW", [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], ctypes.c_ssize_t),
            (self.user, "DestroyWindow", [wt.HWND], wt.BOOL),
            (self.user, "GetMessageW", [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT], wt.BOOL),
            (self.user, "TranslateMessage", [ctypes.POINTER(wt.MSG)], wt.BOOL),
            (self.user, "DispatchMessageW", [ctypes.POINTER(wt.MSG)], ctypes.c_ssize_t),
            (self.user, "PostMessageW", [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM], wt.BOOL),
            (self.user, "PostQuitMessage", [ctypes.c_int], None),
            (self.user, "RegisterWindowMessageW", [wt.LPCWSTR], wt.UINT),
            (self.user, "SetTimer", [wt.HWND, ctypes.c_size_t, wt.UINT, wt.LPVOID], ctypes.c_size_t),
            (self.user, "KillTimer", [wt.HWND, ctypes.c_size_t], wt.BOOL),
            (self.user, "CreatePopupMenu", [], wt.HMENU),
            (self.user, "AppendMenuW", [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR], wt.BOOL),
            (self.user, "DestroyMenu", [wt.HMENU], wt.BOOL),
            (self.user, "TrackPopupMenu", [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID], wt.UINT),
            (self.user, "GetCursorPos", [ctypes.POINTER(wt.POINT)], wt.BOOL),
            (self.user, "SetForegroundWindow", [wt.HWND], wt.BOOL),
            (self.user, "GetForegroundWindow", [], wt.HWND),
            (self.user, "GetAncestor", [wt.HWND, wt.UINT], wt.HWND),
            (self.user, "ShowWindow", [wt.HWND, ctypes.c_int], wt.BOOL),
            (self.user, "FindWindowW", [wt.LPCWSTR, wt.LPCWSTR], wt.HWND),
            (self.user, "GetDC", [wt.HWND], wt.HDC),
            (self.user, "ReleaseDC", [wt.HWND, wt.HDC], ctypes.c_int),
            (self.user, "CreateIconIndirect", [ctypes.POINTER(ICONINFO)], wt.HICON),
            (self.user, "DestroyIcon", [wt.HICON], wt.BOOL),
            (self.gdi, "CreateDIBSection", [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT, ctypes.POINTER(wt.LPVOID), wt.HANDLE, wt.DWORD], wt.HBITMAP),
            (self.gdi, "CreateBitmap", [ctypes.c_int, ctypes.c_int, wt.UINT, wt.UINT, wt.LPVOID], wt.HBITMAP),
            (self.gdi, "DeleteObject", [wt.HANDLE], wt.BOOL),
            (self.shell, "Shell_NotifyIconW", [wt.DWORD, ctypes.POINTER(NOTIFYICONDATA)], wt.BOOL),
        ]
        for library, name, arguments, result in declarations:
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result

    def icon(self, badge=False, blank=False):
        """Draw a small code-native whale glyph into a 32-bit alpha icon."""
        size = 32
        pixels, mask = bytearray(size * size * 4), bytearray(size * size // 8)
        for y in range(size):
            for x in range(size):
                color = None
                if not blank:
                    if ((x - 13) / 11) ** 2 + ((y - 19) / 8) ** 2 <= 1:
                        color = (50, 124, 236)
                    if 22 <= x <= 30 and 11 <= y <= 22 and (x >= 27 or abs(y - 16.5) <= (x - 20) / 2):
                        color = (50, 124, 236)
                    if ((x - 12) / 8) ** 2 + ((y - 23) / 3) ** 2 <= 1:
                        color = (185, 220, 255)
                    if (x - 7) ** 2 + (y - 17) ** 2 <= 2:
                        color = (255, 255, 255)
                    if x == 7 and y == 17:
                        color = (19, 45, 75)
                    if ((x - 10) / 1.4) ** 2 + ((y - 7) / 3) ** 2 <= 1 or ((x - 15) / 1.4) ** 2 + ((y - 8) / 2) ** 2 <= 1:
                        color = (114, 184, 255)
                    if badge and (x - 25) ** 2 + (y - 7) ** 2 <= 30:
                        color = (238, 77, 91)
                if color:
                    red, green, blue = color
                    index = (y * size + x) * 4
                    pixels[index:index + 4] = bytes((blue, green, red, 255))
                else:
                    mask[y * 4 + x // 8] |= 0x80 >> (x % 8)
        info = self.BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(info.bmiHeader)
        info.bmiHeader.biWidth, info.bmiHeader.biHeight = size, -size
        info.bmiHeader.biPlanes, info.bmiHeader.biBitCount = 1, 32
        bits = wt.LPVOID()
        dc = self.user.GetDC(None)
        bitmap = self.gdi.CreateDIBSection(dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
        self.user.ReleaseDC(None, dc)
        mask_buffer = ctypes.create_string_buffer(bytes(mask))
        mask_bitmap = self.gdi.CreateBitmap(size, size, 1, 1, mask_buffer)
        try:
            if not bitmap or not bits or not mask_bitmap:
                raise ctypes.WinError(ctypes.get_last_error())
            ctypes.memmove(bits, bytes(pixels), len(pixels))
            icon_info = self.ICONINFO(True, 0, 0, mask_bitmap, bitmap)
            handle = self.user.CreateIconIndirect(ctypes.byref(icon_info))
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            return handle
        finally:
            if bitmap:
                self.gdi.DeleteObject(bitmap)
            if mask_bitmap:
                self.gdi.DeleteObject(mask_bitmap)


def show_existing(instance_window=INSTANCE_WINDOW) -> bool:
    if sys.platform != "win32":
        return False
    api = _WinAPI()
    window = api.user.FindWindowW(None, instance_window)
    message = api.user.RegisterWindowMessageW(SHOW_MESSAGE_NAME)
    return bool(window and message and api.user.PostMessageW(window, message, 0, 0))


class WindowsTray:
    def __init__(self, name: str, instance_window=INSTANCE_WINDOW):
        self.name = name
        self.instance_window = instance_window
        self.actions: queue.Queue[str] = queue.Queue()
        self.ready = threading.Event()
        self.hwnd = None
        self.added = False
        self.unread = 0
        self.lit = True
        self.blink_count = 0
        self.error = ""
        self.api = None
        self.icons = []
        self.thread = threading.Thread(target=self._run, name="whale-system-tray", daemon=True)
        self.stopping = threading.Event()

    @property
    def available(self) -> bool:
        return bool(self.hwnd and self.added and self.thread.is_alive())

    def start(self) -> bool:
        self.thread.start()
        if not self.ready.wait(timeout=3):
            self.error = "系统托盘启动超时"
            self.close()
            return False
        return self.available

    def set_unread(self, count: int):
        if self.hwnd and self.api:
            self.api.user.PostMessageW(self.hwnd, WM_UNREAD, max(0, count), 0)

    def foreground(self, child_handle: int) -> bool:
        if not self.api:
            return False
        root = self.api.user.GetAncestor(child_handle, 2)
        return bool(root and root == self.api.user.GetForegroundWindow())

    def focus(self, child_handle: int):
        if self.api:
            root = self.api.user.GetAncestor(child_handle, 2)
            if root:
                self.api.user.ShowWindow(root, 9)
                self.api.user.SetForegroundWindow(root)

    def close(self):
        self.stopping.set()
        if self.hwnd and self.api:
            self.api.user.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def _add(self):
        self.nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        self.added = bool(self.api.shell.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self.nid)))
        if self.added:
            self.nid.uVersion = 4
            self.api.shell.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(self.nid))

    def _update(self):
        self.nid.uFlags = NIF_ICON | NIF_TIP | NIF_SHOWTIP
        self.nid.hIcon = self.icons[1 if self.lit else 2] if self.unread else self.icons[0]
        self.nid.szTip = (f"{self.name} · {self.unread} 条未读来信" if self.unread else f"{self.name} · 点击打开小屋，右键退出")[:127]
        if self.added:
            self.api.shell.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def _menu(self):
        api = self.api
        menu = api.user.CreatePopupMenu()
        if not menu:
            return
        try:
            api.user.AppendMenuW(menu, 0, 1001, "查看新来信" if self.unread else "打开小屋")
            api.user.AppendMenuW(menu, 0x800, 0, None)
            api.user.AppendMenuW(menu, 0, 1002, "退出小屋")
            point = wt.POINT()
            api.user.GetCursorPos(ctypes.byref(point))
            api.user.SetForegroundWindow(self.hwnd)
            command = api.user.TrackPopupMenu(menu, 0x100 | 0x2, point.x, point.y, 0, self.hwnd, None)
            api.user.PostMessageW(self.hwnd, 0, 0, 0)
            if command in (1001, 1002):
                self.actions.put("show" if command == 1001 else "exit")
        finally:
            api.user.DestroyMenu(menu)

    def _dispatch(self, hwnd, message, wparam, lparam):
        api = self.api
        if message == self.show_message:
            self.actions.put("show")
            return 0
        if message == self.taskbar_created:
            self._add()
            self._update()
            return 0
        if message == WM_UNREAD:
            self.unread, self.lit = int(wparam), True
            if self.unread:
                api.user.SetTimer(hwnd, 1, 550, None)
            else:
                api.user.KillTimer(hwnd, 1)
            self._update()
            return 0
        if message == WM_TIMER and wparam == 1:
            self.lit = not self.lit
            self.blink_count += 1
            self._update()
            return 0
        if message == WM_TRAY:
            event = lparam & 0xFFFF
            if event in (NIN_SELECT, NIN_KEYSELECT, WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                self.actions.put("show")
            elif event in (WM_CONTEXTMENU, WM_RBUTTONUP):
                self._menu()
            return 0
        if message == WM_CLOSE:
            api.user.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            api.user.KillTimer(hwnd, 1)
            api.user.PostQuitMessage(0)
            return 0
        return api.user.DefWindowProcW(hwnd, message, wparam, lparam)

    def _run(self):
        registered = False
        try:
            self.api = api = _WinAPI()
            self.show_message = api.user.RegisterWindowMessageW(SHOW_MESSAGE_NAME)
            self.taskbar_created = api.user.RegisterWindowMessageW("TaskbarCreated")
            self.class_name = f"WhaleFriend.Tray.{os.getpid()}.{id(self)}"
            self.instance = api.kernel.GetModuleHandleW(None)

            @api.WNDPROC
            def callback(hwnd, message, wparam, lparam):
                try:
                    return self._dispatch(hwnd, message, wparam, lparam)
                except Exception:
                    self.error = "系统托盘事件处理失败"
                    return api.user.DefWindowProcW(hwnd, message, wparam, lparam)

            self.callback = callback
            window_class = api.WNDCLASS()
            window_class.lpfnWndProc, window_class.hInstance = callback, self.instance
            window_class.lpszClassName = self.class_name
            if not api.user.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered = True
            self.hwnd = api.user.CreateWindowExW(0, self.class_name, self.instance_window, 0, 0, 0, 0, 0, None, None, self.instance, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            for kwargs in ({}, {"badge": True}, {"blank": True}):
                self.icons.append(api.icon(**kwargs))
            self.nid = api.NOTIFYICONDATA()
            self.nid.cbSize, self.nid.hWnd, self.nid.uID = ctypes.sizeof(self.nid), self.hwnd, 1
            self.nid.uCallbackMessage, self.nid.hIcon = WM_TRAY, self.icons[0]
            self.nid.szTip = f"{self.name} · 点击打开小屋，右键退出"[:127]
            self._add()
            if not self.added:
                raise RuntimeError("无法添加系统托盘图标")
            self.ready.set()
            if self.stopping.is_set():
                return
            message = wt.MSG()
            while True:
                result = api.user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result <= 0:
                    break
                api.user.TranslateMessage(ctypes.byref(message))
                api.user.DispatchMessageW(ctypes.byref(message))
        except Exception as error:
            self.error = str(error)
        finally:
            if self.api:
                if self.added:
                    self.api.shell.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
                self.added = False
                if self.hwnd:
                    self.api.user.DestroyWindow(self.hwnd)
                for icon in self.icons:
                    self.api.user.DestroyIcon(icon)
                if registered:
                    self.api.user.UnregisterClassW(self.class_name, self.instance)
            self.hwnd = None
            self.ready.set()
