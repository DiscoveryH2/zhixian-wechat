"""Lazily bound Win32 APIs; all pointer/handle arguments are x64 safe."""
import ctypes
from ctypes import wintypes as w
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def libraries():
    if os.name != "nt":
        raise RuntimeError("微信窗口采集仅支持 Windows")
    u = ctypes.WinDLL("user32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    d = ctypes.WinDLL("dwmapi", use_last_error=True)
    signatures = {
        "IsWindow": ([w.HWND], w.BOOL),
        "IsWindowVisible": ([w.HWND], w.BOOL),
        "IsIconic": ([w.HWND], w.BOOL),
        "GetWindowTextW": ([w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
        "GetWindowThreadProcessId": ([w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
        "ShowWindow": ([w.HWND, ctypes.c_int], w.BOOL),
        "SetWindowPos": ([w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT], w.BOOL),
        "GetWindowRect": ([w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
        "GetForegroundWindow": ([], w.HWND),
        "SetForegroundWindow": ([w.HWND], w.BOOL),
        "AttachThreadInput": ([w.DWORD, w.DWORD, w.BOOL], w.BOOL),
        "GetCursorPos": ([ctypes.POINTER(w.POINT)], w.BOOL),
        "SetCursorPos": ([ctypes.c_int, ctypes.c_int], w.BOOL),
        "OpenClipboard": ([w.HWND], w.BOOL),
        "CloseClipboard": ([], w.BOOL),
        "EmptyClipboard": ([], w.BOOL),
        "SetClipboardData": ([w.UINT, w.HANDLE], w.HANDLE),
        "keybd_event": ([w.BYTE, w.BYTE, w.DWORD, ctypes.c_size_t], None),
        "mouse_event": ([w.DWORD, w.DWORD, w.DWORD, w.DWORD, ctypes.c_size_t], None),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(u, name)
        fn.argtypes, fn.restype = args, result
    for name, args, result in [
        ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
        ("QueryFullProcessImageNameW", [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)], w.BOOL),
        ("CloseHandle", [w.HANDLE], w.BOOL),
        ("GetCurrentThreadId", [], w.DWORD),
        ("GlobalAlloc", [w.UINT, ctypes.c_size_t], w.HGLOBAL),
        ("GlobalLock", [w.HGLOBAL], ctypes.c_void_p),
        ("GlobalUnlock", [w.HGLOBAL], w.BOOL),
        ("GlobalFree", [w.HGLOBAL], w.HGLOBAL),
    ]:
        fn = getattr(k, name)
        fn.argtypes, fn.restype = args, result
    d.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]
    d.DwmGetWindowAttribute.restype = ctypes.c_long
    return u, k, d


def window_title(hwnd):
    u, _, _ = libraries()
    if not u.IsWindow(hwnd):
        return ""
    buf = ctypes.create_unicode_buffer(512)
    u.GetWindowTextW(hwnd, buf, len(buf))
    return buf.value


def process_name(hwnd):
    u, k, _ = libraries()
    pid = w.DWORD()
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = k.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
        if k.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        k.CloseHandle(handle)
