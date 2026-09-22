"""Paste into a freshly verified WeChat input box. Never sends a message."""
import ctypes
from ctypes import wintypes as w
import time

from app.winapi import libraries, process_name, window_title


def set_clipboard(text):
    u, k, _ = libraries()
    data = str(text).encode("utf-16-le") + b"\0\0"
    for _ in range(10):
        if not u.OpenClipboard(None):
            time.sleep(.03)
            continue
        try:
            if not u.EmptyClipboard():
                raise RuntimeError("无法写入剪贴板")
            handle = k.GlobalAlloc(0x2, len(data))
            if not handle:
                raise RuntimeError("剪贴板内存分配失败")
            pointer = k.GlobalLock(handle)
            if not pointer:
                k.GlobalFree(handle)
                raise RuntimeError("剪贴板内存锁定失败")
            ctypes.memmove(pointer, data, len(data))
            k.GlobalUnlock(handle)
            if not u.SetClipboardData(13, handle):
                k.GlobalFree(handle)
                raise RuntimeError("无法设置剪贴板文本")
            return
        finally:
            u.CloseClipboard()
    raise RuntimeError("剪贴板正在被占用，请稍后重试")


def fill(hwnd, area, text, verify=None, expected_window_title=None):
    """verify must OCR a recent frame and raise on any changed conversation."""
    if not callable(verify):
        raise RuntimeError("缺少最新会话核验，已拒绝填入")
    if not str(text).strip():
        raise RuntimeError("回复内容为空")
    u, k, d = libraries()
    expected_window_title = expected_window_title or window_title(hwnd)

    def check():
        if not u.IsWindow(hwnd) or not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd):
            raise RuntimeError("微信窗口不可用，请打开目标聊天后重试")
        if process_name(hwnd) not in ("wechat.exe", "weixin.exe"):
            raise RuntimeError("窗口已改变，已拒绝填入")
        if window_title(hwnd) != expected_window_title:
            raise RuntimeError("微信窗口标题已改变，已拒绝填入")
        return verify()

    check()
    fg = u.GetForegroundWindow()
    if fg != hwnd:
        thread_id = u.GetWindowThreadProcessId(fg, None) if fg else 0
        own = k.GetCurrentThreadId()
        attached = bool(thread_id and u.AttachThreadInput(own, thread_id, True))
        try:
            u.SetForegroundWindow(hwnd)
        finally:
            if attached:
                u.AttachThreadInput(own, thread_id, False)
        time.sleep(.15)
    if u.GetForegroundWindow() != hwnd:
        raise RuntimeError("无法确认微信处于前台，已拒绝填入")
    fresh_area = check()
    area = fresh_area or area
    rect = w.RECT()
    if d.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        if not u.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise RuntimeError("无法定位微信输入框")
    x0, _, x1, y1 = area[:4]
    if x1 - x0 < 160 or rect.bottom - rect.top - y1 < 85:
        raise RuntimeError("输入区域过小，已拒绝填入；请放大微信窗口")
    x, y = rect.left + x0 + 60, rect.top + y1 + 40
    if not (rect.left < x < rect.right and rect.top < y < rect.bottom - 35):
        raise RuntimeError("输入框位置无法确认")
    check()
    set_clipboard(text)
    old = w.POINT()
    u.GetCursorPos(ctypes.byref(old))
    try:
        u.SetCursorPos(x, y)
        u.mouse_event(0x2, 0, 0, 0, 0)
        u.mouse_event(0x4, 0, 0, 0, 0)
    finally:
        u.SetCursorPos(old.x, old.y)
    check()
    if u.GetForegroundWindow() != hwnd:
        raise RuntimeError("焦点已离开微信，已取消填入")
    for key in (0x23, 0x56):
        u.keybd_event(0x11, 0, 0, 0)
        try:
            u.keybd_event(key, 0, 0, 0)
            u.keybd_event(key, 0, 2, 0)
        finally:
            u.keybd_event(0x11, 0, 2, 0)
    return {"success": True, "message": "已填入微信输入框，请检查后手动发送"}
