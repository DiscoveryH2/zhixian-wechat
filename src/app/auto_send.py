"""Fail-closed sending into the currently visible WeChat composer.

This module only confirms that the intended text is present in the focused
composer before pressing Enter. A successful key press is not delivery proof.
"""
import time
import unicodedata

from app import fill as fill_service
from app.winapi import libraries, process_name, window_title


MAX_TEXT_LENGTH = 180
MAX_OBSERVATION_AGE_SECONDS = 1.5
OBSERVATION_WAIT_SECONDS = 1.5
OBSERVATION_POLL_SECONDS = 0.02
_PLACEHOLDERS = {"输入消息", "发送消息", "发消息", "Message", "Type a message"}


def _normalize(text):
    return unicodedata.normalize("NFC", str(text)).replace("\r\n", "\n").replace("\r", "\n")


def _assert_target(hwnd, expected_window_title, *, require_foreground=True):
    u, _, _ = libraries()
    if not u.IsWindow(hwnd) or not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd):
        raise RuntimeError("微信窗口不可用，已取消发送")
    if process_name(hwnd) not in ("wechat.exe", "weixin.exe"):
        raise RuntimeError("目标窗口已改变，已取消发送")
    if not expected_window_title or window_title(hwnd) != expected_window_title:
        raise RuntimeError("微信窗口标题已改变，已取消发送")
    if require_foreground and u.GetForegroundWindow() != hwnd:
        raise RuntimeError("微信已失去前台焦点，已取消发送")


def _observation(inspect_composer, *, newer_than=None, require_focused=True,
                verify=None):
    deadline = time.monotonic() + OBSERVATION_WAIT_SECONDS
    while True:
        if verify is not None:
            verify()
        observed = inspect_composer()
        if not isinstance(observed, dict):
            raise RuntimeError("无法确认微信输入框状态，已取消发送")
        if observed.get("target_ok", True) is not True:
            raise RuntimeError("输入框目标已改变，已取消发送")
        if require_focused and observed.get("focused") is not True:
            raise RuntimeError("微信输入框未获得焦点，已取消发送")
        try:
            at = float(observed["at"])
        except (KeyError, TypeError, ValueError):
            raise RuntimeError("输入框画面时间无效，已取消发送")
        now = time.monotonic()
        if at > now:
            raise RuntimeError("输入框画面时间无效，已取消发送")
        fresh = now - at <= MAX_OBSERVATION_AGE_SECONDS
        new_enough = newer_than is None or at > newer_than
        if fresh and new_enough:
            return observed
        if newer_than is None or now >= deadline:
            if not fresh:
                raise RuntimeError("输入框画面已过期，已取消发送")
            raise RuntimeError("输入框画面未更新，已取消发送")
        time.sleep(OBSERVATION_POLL_SECONDS)


def _press_enter():
    u, _, _ = libraries()
    u.keybd_event(0x0D, 0, 0, 0)
    u.keybd_event(0x0D, 0, 2, 0)


def send_verified(hwnd, area, text, *, verify, inspect_composer,
                  expected_window_title=None, press_send=None):
    """Fill and send only if the visible target and composer are verified.

    ``verify`` must raise if the selected conversation/window is no longer the
    expected target. ``inspect_composer`` must return a fresh dict containing
    ``text``, ``focused`` and monotonic timestamp ``at``. ``press_send`` is an
    optional zero-argument injection point for tests.
    """
    if not callable(verify) or not callable(inspect_composer):
        raise RuntimeError("缺少目标或输入框核验，已取消发送")
    message = str(text)
    if not message.strip():
        raise RuntimeError("回复内容为空，已取消发送")
    if len(message) > MAX_TEXT_LENGTH:
        raise RuntimeError("回复内容超过180字，已取消发送")

    expected_window_title = expected_window_title or window_title(hwnd)
    # The assistant UI may be foreground while this operation starts. At this
    # point we only need a valid, identified window and an empty composer.
    _assert_target(hwnd, expected_window_title, require_foreground=False)
    verify()
    initial = _observation(inspect_composer, require_focused=False,
                           verify=verify)
    draft = _normalize(initial.get("text", ""))
    if draft and draft not in _PLACEHOLDERS:
        raise RuntimeError("输入框已有草稿，已取消发送")

    # fill() has its own target checks and never presses Enter.
    fill_started = time.monotonic()
    fill_service.fill(hwnd, area, message, verify=verify,
                      expected_window_title=expected_window_title)
    fill_completed = time.monotonic()

    _assert_target(hwnd, expected_window_title)
    verify()
    after_fill = _observation(inspect_composer, newer_than=fill_completed,
                               verify=verify)
    if _normalize(after_fill.get("text", "")) != _normalize(message):
        raise RuntimeError("输入框草稿与拟发送内容不一致，已取消发送")

    # Recheck both target and fresh composer immediately before the key event.
    _assert_target(hwnd, expected_window_title)
    verify()
    final = _observation(inspect_composer, newer_than=fill_completed,
                         verify=verify)
    if _normalize(final.get("text", "")) != _normalize(message):
        raise RuntimeError("发送前输入框内容已改变，已取消发送")
    verify()
    _assert_target(hwnd, expected_window_title)
    (press_send or _press_enter)()
    return {"success": True, "status": "sent_unconfirmed",
            "message": "已按下发送键；无法仅凭按键确认消息已送达"}
