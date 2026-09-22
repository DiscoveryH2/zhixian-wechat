"""Restore the explicitly tested WeChat main window and this project's assistant only."""
import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from app.winapi import libraries, process_name, window_title

u32, _, _ = libraries()
targets = []
@ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
def visit(hwnd, _):
    name = process_name(hwnd)
    title = window_title(hwnd)
    if name in ('weixin.exe', 'wechat.exe') and title == '微信':
        targets.append(('wechat', hwnd))
    if name in ('pythonw.exe', 'zhixian.exe') and title == '知弦 · 实时对话助手':
        targets.append(('assistant', hwnd))
    return True
u32.EnumWindows.argtypes = [type(visit), ctypes.c_void_p]
u32.EnumWindows(visit, 0)
for kind, hwnd in sorted(targets, reverse=True):
    u32.ShowWindow(hwnd, 9)
    if kind == 'assistant':
        u32.SetForegroundWindow(hwnd)
print({'wechat_restored': any(k == 'wechat' for k, _ in targets), 'assistant_shown': any(k == 'assistant' for k, _ in targets)})
