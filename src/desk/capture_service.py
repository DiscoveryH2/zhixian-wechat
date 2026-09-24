"""Background Windows OCR capture and optional WeFlow reader.

Screenshots remain in RAM. Native imports are delayed so model/store tests can
run without Windows, OCR, Qt, or an installed capture backend.
"""
from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import queue
import re
import secrets
import threading
import time

from desk.weflow import WeFlowClient, normalize_message, stable_id


def session_id(title, source="ocr"):
    return source + ":" + stable_id(str(title).strip())


def check_fill_target(expected, observed, frame_age, same_window=True):
    if not expected or not observed:
        raise RuntimeError("无法可靠识别当前会话标题，已拒绝填入")
    if expected.strip() != observed.strip():
        raise RuntimeError("当前微信会话与所选回复不一致，已拒绝填入")
    if not same_window or frame_age > 2.0 or frame_age < 0:
        raise RuntimeError("微信画面已过期或窗口已改变，请重新采集后填入")
    return True


class ScreenHistory:
    """Exact sequence overlap, preserving direction and repeated bubbles.

    A frame with no overlap is historical/ambiguous (e.g. scrolling), so it does
    not claim incoming activity. Similar text and titles are never merged.
    """
    def __init__(self, sid):
        self.sid = sid
        self.history = []
        self.serial = 0

    def update(self, lines):
        keys = [("me" if w == "me" else "other", str(n or ""), str(t).strip())
                for w, n, t, *_ in lines if str(t).strip() and w in ("me", "her", "other")]
        if not keys:
            return [], not self.history
        # Sender OCR can disappear at the top edge; direction/text define a
        # bubble for overlap but sender is retained in the user-facing record.
        identity = lambda key: (key[0], key[2])
        old = [identity(k) for k in self.history]
        new = [identity(k) for k in keys]
        if old:
            for index in range(len(old) - len(new), -1, -1):
                if old[index:index + len(new)] == new:
                    return [], False
        overlap = 0
        for count in range(min(len(old), len(new)), 0, -1):
            if old[-count:] == new[:count]:
                overlap = count
                break
        historical = not old or not overlap
        added = keys[overlap:]
        self.history.extend(added)
        self.history = self.history[-1000:]
        result = []
        for side, sender, text in added:
            self.serial += 1
            result.append({"id": "ocrmsg:" + stable_id(self.sid, self.serial, side, text),
                           "side": side, "sender": sender, "text": text,
                           "timestamp": None, "source": "ocr", "historical": historical})
        return result, historical


class CaptureService:
    def __init__(self, callback, audit_path=None, db_source_factory=None):
        self.callback = callback
        self.audit_path = Path(audit_path) if audit_path else None
        self._audit_targets = {}
        self._config = {}
        self._lock = threading.RLock()
        self._wanted = threading.Event()
        self._once = threading.Event()
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._reset = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._requests = queue.Queue()
        self._cap = None
        self._hwnd = None
        self._native_title = ""
        self._active_title = ""
        self._readers = {}
        self._histories = {}
        self._client = None
        self._wf_seen = set()
        self._wf_seen_order = deque()
        self._status_key = None
        self._announced = None
        self._wf_loaded = set()
        self._config_revision = 0
        self._control_epoch = 0
        self._last_wechat_foreground_at = 0.0
        self._sidebar_signatures = {}
        # Optional factory keeps the database polling path testable with a fake
        # source. Production discovery is lazy and never loads native capture.
        self._db_source_factory = db_source_factory
        self._db_seen = set()
        self._db_seen_order = deque()

    def _send_audit(self, stage, task, status=""):
        """Record sender provenance without retaining titles, drafts or keys."""
        if self.audit_path is None:
            return
        target_ref = self._audit_targets.get(task["title"])
        if target_ref is None:
            target_ref = self._audit_targets[task["title"]] = secrets.token_hex(8)
        record = {"at": time.time(), "pid": os.getpid(), "stage": stage,
                  "target_ref": target_ref,
                  "status": status}
        payload = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self.audit_path.open("ab") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    def _emit(self, kind, payload):
        if self._stop.is_set():
            return
        try:
            self.callback(kind, payload)
        except Exception:
            # A UI callback failure must not abandon the capture resource.
            pass

    def _status(self, state, detail, source=None):
        source = source or self._config.get("source", "ocr")
        value = (state, detail, source)
        if value != self._status_key:
            self._status_key = value
            self._emit("status", {"capture": state, "detail": detail, "source": source,
                                  "connected": state == "live"})

    def _ensure_thread(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="zhixian-capture", daemon=True)
            self._thread.start()

    def start(self, config):
        with self._lock:
            if not self._wanted.is_set():
                self._sidebar_signatures.clear()
            next_config = dict(config or {})
            requested_source = str(next_config.get("source") or "ocr")
            next_config["source"] = requested_source if requested_source in ("weflow", "wechat_db") else "ocr"
            self._config = next_config
            self._config_revision += 1
            self._control_epoch += 1
            self._restart.set()
            self._wanted.set()
            self._once.clear()
        self._ensure_thread()
        self._wake.set()
        return {"success": True}

    def pause(self):
        self._control_epoch += 1
        self._wanted.clear()
        self._once.clear()
        self._wake.set()
        return {"success": True}

    def cancel_pending_writes(self):
        """Invalidate queued/in-flight paste or send requests without pausing OCR."""
        with self._lock:
            self._control_epoch += 1
        self._wake.set()

    def stop(self, wait=True):
        self._control_epoch += 1
        self._stop.set()
        self._wanted.clear()
        self._once.clear()
        self._wake.set()
        thread = self._thread
        if wait and thread and thread is not threading.current_thread():
            thread.join(timeout=8)
            if thread.is_alive():
                raise RuntimeError("采集线程尚在结束当前识别，请稍后关闭")
        self._thread = None
        return {"success": True}

    def one_shot(self, config=None):
        if config is not None:
            with self._lock:
                self._config = dict(config)
                self._restart.set()
        self._once.set()
        self._ensure_thread()
        self._wake.set()
        return {"success": True}

    def fill(self, text, session_title):
        if self._config.get("source") == "wechat_db":
            raise RuntimeError("数据库读取模式暂不支持基于截图核验的填入操作")
        if not str(text).strip() or not str(session_title).strip():
            raise RuntimeError("请选择有内容的候选回复和明确会话")
        task = {"text": str(text), "title": str(session_title), "done": threading.Event(),
                "cancelled": threading.Event(), "epoch": self._control_epoch}
        self._requests.put(task)
        self._ensure_thread()
        self._wake.set()
        if not task["done"].wait(20):
            task["cancelled"].set()
            raise RuntimeError("安全核验超时，已取消填入；请重新采集")
        if task.get("error"):
            raise RuntimeError(task["error"])
        return task.get("result", {"success": False})

    def send(self, text, session_title, expected_incoming, expected_sender="",
             expected_previous="", expected_sidebar_signature=""):
        """Send only after a new on-screen target and composer check.

        This method never switches chats. If the target conversation is not
        already visible in WeChat, the worker refuses the send.
        """
        if self._config.get("source") == "wechat_db":
            raise RuntimeError("数据库读取模式尚无可靠的目标会话发送适配器，已拒绝发送")
        if not str(text).strip() or not str(session_title).strip() or not str(expected_incoming).strip():
            raise RuntimeError("自动发送需要明确的新消息与目标会话")
        task = {"mode": "send", "text": str(text), "title": str(session_title),
                "expected_incoming": str(expected_incoming), "expected_sender": str(expected_sender or ''),
                "expected_previous": str(expected_previous or ''),
                "expected_sidebar_signature": str(expected_sidebar_signature or ''),
                "done": threading.Event(), "cancelled": threading.Event(), "epoch": self._control_epoch}
        self._requests.put(task)
        self._ensure_thread()
        self._wake.set()
        if not task["done"].wait(30):
            task["cancelled"].set()
            raise RuntimeError("自动发送核验超时；状态可能不确定，请检查微信")
        if task.get("error"):
            raise RuntimeError(task["error"])
        return task.get("result", {"success": False, "status": "unconfirmed"})

    def send_db(self, text, session_id, message_id):
        """Send from one DB event; only header/composer pixels may be inspected.

        The chat record and newest inbound identity are read from SQLCipher.
        This intentionally cannot navigate to another conversation by name.
        """
        if self._config.get("source") != "wechat_db":
            raise RuntimeError("请先切换至微信数据库消息来源")
        if not self._wanted.is_set() or self._stop.is_set():
            raise RuntimeError("数据库监听已暂停，不能自动发送")
        if not all(isinstance(value, str) and value.strip() for value in (text, session_id, message_id)):
            raise RuntimeError("数据库发送需要明确会话与入站消息 ID")
        disclosure = "（以上内容为知弦生成）"
        outgoing = text.replace(disclosure, "").strip() + disclosure
        if len(outgoing) > 180:
            raise RuntimeError("数据库回复超过 180 字，已拒绝发送")
        task = {"mode": "send_db", "text": outgoing, "session_id": session_id,
                "message_id": message_id, "title": session_id,
                "done": threading.Event(), "cancelled": threading.Event(), "epoch": self._control_epoch}
        self._requests.put(task)
        self._ensure_thread()
        self._wake.set()
        if not task["done"].wait(60):
            task["cancelled"].set()
            raise RuntimeError("数据库发送核验超时；结果可能不确定，请检查微信")
        if task.get("error"):
            raise RuntimeError(task["error"])
        return task.get("result", {"success": False, "status": "unconfirmed"})

    def reset_history(self):
        self._reset.set()
        self._wake.set()

    def sources(self, config=None):
        """Discovery touches process/window metadata and loopback /health only."""
        result = []
        try:
            from app.capture import find_wechat_hwnd
            find_wechat_hwnd()
            result.append({"id": "ocr", "name": "微信窗口 OCR", "available": True,
                           "detail": "已发现微信窗口，无需数据库密钥"})
        except Exception:
            result.append({"id": "ocr", "name": "微信窗口 OCR", "available": False,
                           "detail": "请启动 Windows 微信并打开聊天窗口"})
        try:
            client = WeFlowClient((config or self._config).get("weflow_url"), timeout=1)
            available = client.health()
            detail = "已发现本机 WeFlow；选用后需配置 Token" if available else "未发现 WeFlow"
        except Exception:
            available, detail = False, "未发现本机 WeFlow API（可选）"
        result.append({"id": "weflow", "name": "WeFlow 本地 API", "available": available, "detail": detail})
        try:
            from desk.wechat_sqlcipher import discover_cipher_source
            discover_cipher_source()
            db_available = True
            db_detail = "已发现本机 CipherTalk 微信数据库配置；启动时只读验证"
        except Exception:
            db_available = False
            db_detail = "未发现可用的本机 CipherTalk 数据库配置"
        result.append({"id": "wechat_db", "name": "微信本地数据库", "available": db_available,
                       "detail": db_detail})
        return result

    def _new_wechat_db_source(self):
        if self._db_source_factory is not None:
            return self._db_source_factory()
        from desk.wechat_sqlcipher import discover_cipher_source
        from desk.wechat_db_source import WeChatDBSource
        account, db_storage, connect_factory = discover_cipher_source()
        return WeChatDBSource(db_storage, self_wxid=account.wxid, connect_factory=connect_factory)

    @staticmethod
    def _db_head_marker(head):
        return (int(head.get("sort_timestamp") or 0), int(head.get("timestamp") or 0),
                int(head.get("last_msg_local_id") or 0), int(head.get("last_msg_type") or 0))

    @staticmethod
    def _db_cursor(message):
        cursor = message.get("_cursor")
        if isinstance(cursor, (tuple, list)) and len(cursor) == 3:
            try:
                return tuple(int(value or 0) for value in cursor)
            except (TypeError, ValueError, OverflowError):
                return None
        return None

    def _remember_db(self, message_id):
        if not message_id or message_id in self._db_seen:
            return False
        self._db_seen.add(message_id)
        self._db_seen_order.append(message_id)
        while len(self._db_seen_order) > 10000:
            self._db_seen.discard(self._db_seen_order.popleft())
        return True

    def _emit_db_messages(self, source, talker, title, rows, historical):
        messages = []
        for row in rows:
            if not isinstance(row, dict) or not self._remember_db(str(row.get("id") or "")):
                continue
            message = dict(row)
            message.pop("_cursor", None)
            message["source"] = "wechat_db"
            message["historical"] = bool(historical)
            messages.append(message)
        if not messages:
            return
        session_type = "group" if str(talker).endswith("@chatroom") else "private"
        sid = talker
        self._announce_session(sid, title, "wechat_db", session_type, talker)
        self._emit("messages", {"session_id": sid, "title": title, "source": "wechat_db",
                                 "messages": messages, "historical": bool(historical),
                                 "type": session_type, "talker": talker})

    def _run_wechat_db(self):
        """Poll known database session heads. This path never initializes WGC/OCR."""
        source = self._new_wechat_db_source()
        self._db_seen.clear()
        self._db_seen_order.clear()
        loaded_contexts = set()
        sessions = source.sessions(limit=500)
        labels = {}
        offset = 0
        while not self._stop.is_set():
            page = sessions if offset == 0 else source.sessions(limit=500, offset=offset)
            for item in page:
                if not isinstance(item, dict):
                    continue
                talker = str(item.get("id") or "")
                if talker:
                    labels[talker] = str(item.get("name") or talker)
            offset += len(page)
            if len(page) < 500:
                break
            if offset >= 100000:
                raise RuntimeError("微信会话数量超过当前监测上限")

        baseline = {}
        offset = 0
        while not self._stop.is_set():
            heads = source.latest_session_heads(limit=500, offset=offset)
            for head in heads:
                if isinstance(head, dict) and head.get("id"):
                    talker = str(head["id"])
                    baseline[talker] = self._db_head_marker(head)
            offset += len(heads)
            if len(heads) < 500:
                break
            if offset >= 100000:
                raise RuntimeError("微信会话数量超过当前监测上限，已拒绝建立不完整基线")
        # A small number of old message tables can be absent from SessionTable.
        # Seed their last known IDs as historical so the first later index
        # appearance is not mistaken for a brand-new conversation history.
        for talker in labels:
            if talker in baseline or talker.startswith("wxdb:"):
                continue
            latest = source.messages(talker, limit=1)
            if latest and (cursor := self._db_cursor(latest[0])):
                baseline[talker] = (cursor[0], int(latest[0].get("timestamp") or 0), cursor[2], 0)

        # Present the newest known session and its bounded context immediately;
        # the initial batch is always historical and cannot trigger a reply.
        if sessions:
            first = sessions[0]
            talker = str(first.get("id") or "")
            if talker and talker in labels:
                title = labels[talker]
                sid = talker
                session_type = first.get("type") if first.get("type") in ("private", "group") else None
                self._announce_session(sid, title, "wechat_db", session_type, talker)
                limit = min(200, max(10, int(self._config.get("context_limit", 50))))
                initial = source.messages(talker, limit=limit)
                initial = sorted(initial, key=lambda row: self._db_cursor(row) or (0, 0, 0))
                self._emit_db_messages(source, talker, title, initial, historical=True)
                loaded_contexts.add(talker)

        self._status("live", "正在只读监听微信本地数据库", "wechat_db")
        if self._once.is_set() and not self._wanted.is_set():
            self._once.clear()
            return

        poll_seconds = max(.25, min(float(self._config.get("db_poll_seconds", .75)), 5.0))
        while self._wanted.is_set() and not self._stop.is_set() and not self._restart.is_set():
            self._drain_requests()
            if self._once.is_set():
                self._once.clear()
                return
            try:
                current_heads = source.latest_session_heads(limit=500)
                for head in current_heads:
                    if not isinstance(head, dict):
                        continue
                    talker = str(head.get("id") or "")
                    if not talker:
                        continue
                    if talker not in labels:
                        for item in source.sessions(limit=500):
                            if isinstance(item, dict) and item.get("id") == talker:
                                labels[talker] = str(item.get("name") or talker)
                                break
                    if talker not in labels:
                        continue
                    marker = self._db_head_marker(head)
                    previous = baseline.get(talker)
                    if previous is None:
                        baseline[talker] = marker
                        continue
                    if marker == previous:
                        continue
                    title = labels[talker]
                    latest = source.messages(talker, limit=min(200, max(20, int(self._config.get("context_limit", 50)))))
                    latest = sorted(latest, key=lambda row: self._db_cursor(row) or (0, 0, 0))
                    # Commit the watermark only after the bounded message read
                    # succeeds; a transient read failure must be retried.
                    baseline[talker] = marker
                    baseline_local_id = previous[2]
                    # Find the previous committed head in the bounded page.
                    # If it vanished (many rapid messages, shard rollover, or
                    # stale metadata), show context but do not declare liveness.
                    prior_index = next((index for index, row in enumerate(latest)
                                        if self._db_cursor(row) and self._db_cursor(row)[2] == baseline_local_id
                                        and int(row.get("timestamp") or 0) == previous[1]), None)
                    if talker not in loaded_contexts:
                        context = latest[:prior_index + 1] if prior_index is not None else latest
                        self._emit_db_messages(source, talker, title, context, historical=True)
                        loaded_contexts.add(talker)
                    for index, row in enumerate(latest):
                        cursor = self._db_cursor(row)
                        if not cursor or (prior_index is not None and index <= prior_index):
                            continue
                        msg = dict(row)
                        msg_id = str(msg.get("id") or "")
                        if not msg_id or msg_id in self._db_seen:
                            continue
                        stamp = msg.get("timestamp")
                        try:
                            stamp = float(stamp)
                            # Millisecond timestamps are normalized only for age checks.
                            age_stamp = stamp / 1000 if stamp > 10_000_000_000 else stamp
                        except (TypeError, ValueError, OverflowError):
                            age_stamp = 0
                        eligible = (prior_index is not None and msg.get("side") == "other" and msg.get("kind") == "text"
                                    and 0 <= time.time() - age_stamp <= 300)
                        self._emit_db_messages(source, talker, title, [msg], historical=not eligible)
                    self._status("live", "正在只读监听微信本地数据库", "wechat_db")
            except Exception:
                # Do not expose paths, SQL, key material, or message data.
                self._status("error", "微信本地数据库暂时无法读取；请检查账号与文件状态", "wechat_db")
                self._stop.wait(1)
                continue
            self._stop.wait(poll_seconds)

    def _close_capture(self):
        cap, self._cap = self._cap, None
        self._hwnd = None
        self._active_title = ""
        self._native_title = ""
        self._announced = None
        if cap:
            try:
                cap.stop()
            except Exception:
                pass
            try:
                cap.wait()
            except Exception:
                # WGC reports an already-failed capture through wait(). Closing
                # that session must not strand later commands or the app exit.
                pass

    def _ensure_capture(self):
        from app.capture import Capture, find_wechat_hwnd
        from app.winapi import libraries, window_title
        u, _, _ = libraries()
        hwnd = find_wechat_hwnd()
        if u.IsIconic(hwnd):
            raise RuntimeError("请还原微信窗口并打开目标聊天")
        if self._cap and (hwnd != self._hwnd or not self._cap.alive()):
            self._close_capture()
        if self._cap is None:
            self._hwnd = hwnd
            self._native_title = window_title(hwnd)
            self._cap = Capture(hwnd)
        return self._cap

    def _frame(self, wait=0):
        cap = self._ensure_capture()
        until = time.monotonic() + wait
        while not self._stop.is_set():
            latest = cap.latest()
            if latest:
                return latest
            if time.monotonic() >= until:
                break
            self._stop.wait(.05)
        raise RuntimeError("暂未获得新鲜微信画面，请打开目标聊天后重试")

    def _read_frame(self, full, at, emit=True):
        from app.capture import chat_area
        from app.ocr import Reader, read_title, sidebar_latest_meta, sidebar_tail_matches
        from app.winapi import libraries
        area = chat_area(full)
        if area is None:
            self._active_title = ""
            raise RuntimeError("无法定位微信消息区，请放大窗口并打开具体聊天")
        x0, y0, x1, y1, bg, pane = area
        title = read_title(full[pane:y0, x0:x1])
        if not title:
            self._active_title = ""
            raise RuntimeError("无法识别会话标题，已暂停读取；请打开具体聊天")
        self._active_title = title
        self._cap.area = area
        sid = session_id(title)
        if not emit:
            return title, area[:4], at
        reader = self._readers.get(title)
        if reader is None:
            reader = self._readers[title] = Reader()
        history = self._histories.setdefault(sid, ScreenHistory(sid))
        lines = reader.read(full[y0:y1, x0:x1], bg)
        sidebar = sidebar_latest_meta(full, area, max_age_minutes=None)
        previous_sidebar = self._sidebar_signatures.get(sid)
        sidebar_changed = bool(sidebar and previous_sidebar and
                               sidebar['signature'] != previous_sidebar)
        if sidebar:
            self._sidebar_signatures[sid] = sidebar['signature']
        messages, historical = history.update(lines)
        baseline_verified = sidebar_tail_matches(full, area, lines, min_body_chars=2,
                                                 meta=sidebar) if messages else False
        tail_verified = baseline_verified and len(re.sub(r'\s+', '', str(lines[-1][2]))) >= 4
        u, _, _ = libraries()
        foreground = u.GetForegroundWindow() == self._hwnd
        if foreground:
            self._last_wechat_foreground_at = at
        # A background WeChat window cannot receive a user's scroll input. A
        # two-second quiet period avoids treating a just-finished manual scroll
        # as a newly advanced viewport after focus changes.
        background_stable = not foreground and at - self._last_wechat_foreground_at > 2.0
        if threading.current_thread() is self._thread and not (self._wanted.is_set() or self._once.is_set()):
            return title, area[:4], at
        self._announce_session(sid, title, "ocr")
        if messages:
            self._emit("messages", {"session_id": sid, "title": title, "source": "ocr",
                                    "messages": messages, "historical": historical,
                                    "background_stable": background_stable,
                                    "sidebar_changed": sidebar_changed,
                                    "sidebar_signature": sidebar['signature'] if sidebar else '',
                                    "tail_baseline_verified": baseline_verified,
                                    "tail_verified": tail_verified})
        self._status("live", "正在读取当前微信聊天；截图仅在内存中处理", "ocr")
        return title, area[:4], at

    def _announce_session(self, sid, title, source, session_type=None, talker=None):
        if self._announced != (sid, title, source):
            self._announced = (sid, title, source)
            self._emit("session", {"id": sid, "title": title, "source": source, "active": True,
                                   "type": session_type or 'unknown', "talker": talker or ''})

    def _fill_worker(self, task):
        from app.fill import fill
        from app.winapi import window_title
        # A new WGC session guarantees the first validation frame was acquired
        # after the user's explicit fill request, not from a cached old view.
        self._close_capture()
        cap = self._ensure_capture()
        hwnd, native = self._hwnd, self._native_title

        def verify():
            if task["cancelled"].is_set() or self._stop.is_set() or task.get("epoch") != self._control_epoch:
                raise RuntimeError("填入请求已取消")
            full, at = self._frame(wait=2)
            title, area, at = self._read_frame(full, at, emit=False)
            check_fill_target(task["title"], title, time.monotonic() - at,
                              self._hwnd == hwnd and window_title(hwnd) == native)
            return area

        area = verify()
        return fill(hwnd, area, task["text"], verify=verify, expected_window_title=native)

    def _send_worker(self, task):
        from app.auto_send import send_verified, _press_enter, _assert_target
        from app.ocr import Reader, _engine, sidebar_latest_meta, sidebar_tail_matches
        from app.winapi import libraries, window_title
        self._close_capture()
        self._ensure_capture()
        hwnd, native = self._hwnd, self._native_title
        reader = Reader()

        def target_frame(require_tail=True):
            if task['cancelled'].is_set() or self._stop.is_set() or task['epoch'] != self._control_epoch:
                raise RuntimeError('自动发送已取消')
            full, at = self._frame(wait=2)
            title, area, at = self._read_frame(full, at, emit=False)
            check_fill_target(task['title'], title, time.monotonic() - at,
                              self._hwnd == hwnd and window_title(hwnd) == native)
            x0, y0, x1, y1 = area
            chat = full[y0:y1, x0:x1]
            pane_bg = self._cap.area[4] if self._cap and self._cap.area else full[y0, x0]
            visible = [line for line in reader.read(chat, pane_bg) if line[0] in ('her', 'other', 'me')]
            min_chars = 8 if task['expected_sender'] else 4
            meta = sidebar_latest_meta(full, self._cap.area) if require_tail else None
            expected_signature = task.get('expected_sidebar_signature') or ''
            if require_tail and expected_signature and (not meta or meta['signature'] != expected_signature):
                raise RuntimeError('左侧最新消息已变化，已取消过期自动回复')
            if require_tail and not sidebar_tail_matches(full, self._cap.area, visible,
                                                          min_body_chars=min_chars, meta=meta):
                raise RuntimeError('左侧最新消息摘要与当前聊天尾部无法核对，已拒绝发送')
            if require_tail and (not visible or visible[-1][0] not in ('her', 'other')):
                raise RuntimeError('当前聊天已有己方新消息，已拒绝重复回复')
            incoming = [line for line in visible if line[0] in ('her', 'other')]
            if not incoming or re.sub(r'\s+', '', incoming[-1][2]) != re.sub(r'\s+', '', task['expected_incoming']):
                raise RuntimeError('当前窗口最后一条对方消息与待回复消息不同，已拒绝发送')
            if task['expected_sender'] and incoming[-1][1] != task['expected_sender']:
                raise RuntimeError('群聊发言人已改变，已拒绝发送')
            if task['expected_previous']:
                last_index = next((index for index in range(len(visible)-1, -1, -1)
                                   if visible[index] is incoming[-1]), -1)
                before = visible[:last_index]
                expected = re.sub(r'\s+', '', task['expected_previous'])
                if not before or not any(re.sub(r'\s+', '', line[2]) == expected for line in before[-3:]):
                    raise RuntimeError('当前窗口缺少可核对的上一条聊天上下文，已拒绝发送')
            return full, area, at

        def verify():
            return target_frame()[1]

        def inspect_composer(require_tail=True):
            deadline = time.monotonic() + 1.5
            while True:
                full, area, at = target_frame(require_tail=require_tail)
                # WGC continuously timestamps frames. Require a frame captured
                # after this probe begins so a pre-paste image is never reused.
                if time.monotonic() - at < .12:
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('无法取得最新输入框画面，已取消发送')
                time.sleep(.035)
            x0, _, x1, y1 = area
            h = full.shape[0]
            if h - y1 < 85 or x1 - x0 < 200:
                raise RuntimeError('微信输入区过小，已取消发送')
            composer = full[y1 + 30:h - 42, x0 + 24:x1 - 72]
            if composer.size == 0:
                raise RuntimeError('无法定位微信输入框，已取消发送')
            rows, _ = _engine()(composer, use_cls=False)
            text = ''.join(str(row[1]) for row in sorted(rows or [], key=lambda row: (row[0][0][1], row[0][0][0])))
            u, _, _ = libraries()
            return {'text': text.strip(), 'focused': bool(u.GetForegroundWindow() == hwnd),
                    'target_ok': True, 'at': at}

        area = verify()
        def press_if_still_armed():
            # Share the epoch lock with emergency stop. Once stop returns, an
            # in-flight task cannot slip an Enter key event past that boundary.
            with self._lock:
                if task['cancelled'].is_set() or self._stop.is_set() or task['epoch'] != self._control_epoch:
                    raise RuntimeError('自动发送已取消')
                _assert_target(hwnd, native)
                self._send_audit('before_enter', task)
                _assert_target(hwnd, native)
                _press_enter()
                try:
                    self._send_audit('enter_called', task)
                except OSError:
                    # Enter may already have reached WeChat. Never retry it.
                    pass
        result = send_verified(hwnd, area, task['text'], verify=verify,
                               inspect_composer=inspect_composer, expected_window_title=native,
                               press_send=press_if_still_armed)
        # A key press is not delivery proof. Confirm both an empty composer and
        # a matching outgoing bubble; otherwise keep the result uncertain.
        try:
            time.sleep(.35)
            full, area, _ = target_frame(require_tail=False)
            x0, y0, x1, y1 = area
            chat = full[y0:y1, x0:x1]
            pane_bg = self._cap.area[4] if self._cap and self._cap.area else full[y0, x0]
            outgoing = [line for line in reader.read(chat, pane_bg) if line[0] == 'me']
            composer = inspect_composer(require_tail=False)
            if outgoing and re.sub(r'\s+', '', outgoing[-1][2]) == re.sub(r'\s+', '', task['text']) and not composer['text']:
                result = {'success': True, 'status': 'sent', 'message': '已在当前微信会话确认新发出的文字气泡'}
        except Exception as exc:
            try:
                self._send_audit('confirm_failed', task, type(exc).__name__)
            except OSError:
                pass
        try:
            self._send_audit('send_result', task, result.get('status', 'unconfirmed'))
        except OSError:
            pass
        return result

    def _send_db_worker(self, task):
        """Use DB for chat state; inspect only WeChat's title and composer."""
        from app.auto_send import send_verified, _assert_target, _press_enter
        from app.capture import chat_area
        from app.ocr import _engine, read_title
        from app.winapi import libraries, window_title
        from desk.store import normalize_title

        source = self._new_wechat_db_source()
        sid = task["session_id"]
        labels = []
        offset = 0
        while offset < 100000:
            page = source.sessions(limit=500, offset=offset)
            for item in page:
                if isinstance(item, dict) and item.get("id"):
                    labels.append((str(item["id"]), str(item.get("name") or item["id"])))
            offset += len(page)
            if len(page) < 500:
                break
        if offset >= 100000:
            raise RuntimeError("会话目录过大，无法完成发送身份核验")
        title = next((name for ident, name in labels if ident == sid), None)
        if not title or not normalize_title(title):
            raise RuntimeError("数据库中找不到可核验的目标会话")
        if sum(normalize_title(name) == normalize_title(title) for _, name in labels) != 1:
            raise RuntimeError("存在同名会话，无法仅凭微信标题安全确认目标")

        def current_inbound():
            messages = source.messages(sid, limit=1)
            return bool(messages and messages[0].get("id") == task["message_id"]
                        and messages[0].get("side") == "other"
                        and messages[0].get("kind") == "text")

        if not current_inbound():
            raise RuntimeError("目标会话已有更新，旧消息不会自动回复")
        self._close_capture()
        self._ensure_capture()
        hwnd, native = self._hwnd, self._native_title
        u, _, _ = libraries()

        def target_frame():
            if task["cancelled"].is_set() or self._stop.is_set() or task["epoch"] != self._control_epoch:
                raise RuntimeError("数据库发送已取消")
            full, at = self._frame(wait=2)
            area = chat_area(full)
            if area is None:
                raise RuntimeError("无法定位微信会话标题与输入框")
            x0, y0, x1, y1, _, pane = area
            observed = read_title(full[pane:y0, x0:x1])
            if not observed or normalize_title(observed) != normalize_title(title):
                raise RuntimeError("微信当前会话标题与数据库目标不一致")
            if time.monotonic() - at > 1 or self._hwnd != hwnd or window_title(hwnd) != native:
                raise RuntimeError("微信目标窗口或标题画面已变化")
            self._cap.area = area
            return full, area[:4], at

        def verify():
            return target_frame()[1]

        def inspect_composer():
            full, area, at = target_frame()
            x0, _, x1, y1 = area
            height = full.shape[0]
            if height - y1 < 85 or x1 - x0 < 200:
                raise RuntimeError("微信输入区域过小")
            composer = full[y1 + 30:height - 42, x0 + 24:x1 - 72]
            if not composer.size:
                raise RuntimeError("无法定位微信输入框")
            rows, _ = _engine()(composer, use_cls=False)
            value = ''.join(str(row[1]) for row in sorted(rows or [],
                            key=lambda row: (row[0][0][1], row[0][0][0])))
            return {"text": value.strip(), "focused": bool(u.GetForegroundWindow() == hwnd),
                    "target_ok": True, "at": at}

        def press_if_still_armed():
            with self._lock:
                if task["cancelled"].is_set() or self._stop.is_set() or task["epoch"] != self._control_epoch:
                    raise RuntimeError("数据库发送已取消")
                if not current_inbound():
                    raise RuntimeError("发送前数据库入站消息已变化")
                target_frame()
                _assert_target(hwnd, native)
                self._send_audit("before_enter", task)
                if not current_inbound():
                    raise RuntimeError("最终发送前最新入站消息已变化")
                _assert_target(hwnd, native)
                _press_enter()
                try:
                    self._send_audit("enter_called", task)
                except OSError:
                    pass

        try:
            area = verify()
            result = send_verified(hwnd, area, task["text"], verify=verify,
                                   inspect_composer=inspect_composer,
                                   expected_window_title=native,
                                   press_send=press_if_still_armed)
            # Delivery evidence comes from the same DB session, never OCR chat.
            deadline = time.monotonic() + 7
            while time.monotonic() < deadline:
                latest = source.messages(sid, limit=1)
                if (latest and latest[0].get("side") == "me"
                        and str(latest[0].get("text") or "").strip() == task["text"]):
                    result = {"success": True, "status": "sent",
                              "message": "已从目标会话数据库确认发出的消息"}
                    break
                if task["cancelled"].is_set() or self._stop.is_set():
                    break
                time.sleep(.25)
            self._send_audit("send_result", task, result.get("status", "unconfirmed"))
            return result
        finally:
            self._close_capture()

    def _drain_requests(self):
        while True:
            try:
                task = self._requests.get_nowait()
            except queue.Empty:
                break
            try:
                if task["cancelled"].is_set() or self._stop.is_set():
                    raise RuntimeError("填入请求已取消")
                task["result"] = (self._send_db_worker(task) if task.get("mode") == "send_db" else
                                  self._send_worker(task) if task.get('mode') == 'send' else self._fill_worker(task))
            except Exception as exc:
                task["error"] = str(exc)[:200]
                if task.get("mode") in ("send", "send_db"):
                    try:
                        self._send_audit('blocked', task)
                    except OSError:
                        pass
            finally:
                task["done"].set()

    def _remember_wf(self, message):
        key = message["id"]
        if key in self._wf_seen:
            return False
        self._wf_seen.add(key)
        self._wf_seen_order.append(key)
        while len(self._wf_seen_order) > 5000:
            self._wf_seen.discard(self._wf_seen_order.popleft())
        return True

    def _weflow_messages(self, talker, title, rows, historical=False, incoming=False):
        sid = session_id(talker, "weflow")
        messages = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            msg = normalize_message(row, sid, incoming=incoming)
            if msg and self._remember_wf(msg):
                msg["historical"] = historical
                messages.append(msg)
        session_type = 'group' if talker.endswith('@chatroom') else 'private'
        self._announce_session(sid, title, "weflow", session_type, talker)
        if messages:
            self._emit("messages", {"session_id": sid, "title": title, "source": "weflow",
                                    "messages": messages, "historical": historical,
                                    "type": session_type, "talker": talker})

    def _weflow_initial(self, client):
        sessions = client.sessions(limit=1)
        if not sessions:
            self._status("live", "WeFlow 已连接，暂时没有会话", "weflow")
            return
        session = sessions[0]
        talker = str(session.get("username") or session.get("id") or "")
        if talker:
            title = str(session.get("displayName") or session.get("name") or talker)
            rows = client.messages(talker, min(200, max(10, int(self._config.get("context_limit", 50)))))
            rows = sorted(rows, key=lambda m: float(m.get("timestamp", m.get("createTime", 0)) or 0))
            self._weflow_messages(talker, title, rows, historical=True)
            self._wf_loaded.add(talker)
        self._status("live", "WeFlow 已连接，等待新消息", "weflow")

    def _run_weflow(self):
        config = dict(self._config)
        client = self._client = WeFlowClient(config.get("weflow_url"), config.get("weflow_token"), timeout=2)
        # The upstream replays an in-memory backlog to a new subscriber. Old
        # backlog must never masquerade as newly received conversation activity.
        started_at = int(time.time())
        try:
            self._weflow_initial(client)
            if self._once.is_set() and not self._wanted.is_set():
                self._once.clear()
                return
            while self._wanted.is_set() and not self._stop.is_set() and not self._restart.is_set():
                self._drain_requests()
                if self._once.is_set():
                    self._weflow_initial(client)
                    self._once.clear()
                try:
                    for event, payload in client.events(self._stop):
                        if not self._wanted.is_set() or self._restart.is_set() or self._stop.is_set():
                            break
                        self._drain_requests()
                        if event in ("message.new", "message") and payload.get("event", event) == "message.new":
                            talker = str(payload.get("sessionId") or "")
                            if talker:
                                title = str(payload.get("groupName") or payload.get("sourceName") or talker)
                                if talker not in self._wf_loaded:
                                    rows = client.messages(talker, min(200, max(10, int(config.get("context_limit", 50)))))
                                    current = normalize_message(payload, session_id(talker, "weflow"), incoming=True)
                                    prior = []
                                    for row in rows:
                                        item = normalize_message(row, session_id(talker, "weflow"))
                                        if item and current and item["id"] == current["id"]:
                                            continue
                                        prior.append(row)
                                    prior.sort(key=lambda m: float(m.get("timestamp", m.get("createTime", 0)) or 0))
                                    self._weflow_messages(talker, title, prior, historical=True)
                                    self._wf_loaded.add(talker)
                                stamp = float(payload.get("timestamp") or 0)
                                self._weflow_messages(talker, title, [payload], incoming=True,
                                                       historical=stamp <= started_at)
                                self._status("live", "正在接收 WeFlow 消息", "weflow")
                        elif event == "message.revoke":
                            self._emit("error", {"message": "检测到消息撤回，请重新核对当前会话后使用候选回复"})
                except (TimeoutError, OSError):
                    # Short socket timeout keeps pause/stop responsive; replay ID
                    # and normalized IDs prevent reconnect duplicates.
                    pass
                if self._wanted.is_set() and not self._restart.is_set():
                    self._stop.wait(.15)
        finally:
            client.close()
            self._client = None

    def _run(self):
        try:
            while not self._stop.is_set():
                self._drain_requests()
                if self._reset.is_set():
                    self._reset.clear()
                    self._histories.clear()
                    self._readers.clear()
                    self._wf_seen.clear()
                    self._wf_seen_order.clear()
                    self._wf_loaded.clear()
                    self._sidebar_signatures.clear()
                    self._close_capture()
                if self._restart.is_set():
                    self._restart.clear()
                    self._close_capture()
                    self._status_key = None
                if not self._wanted.is_set() and not self._once.is_set():
                    self._close_capture()
                    self._status("paused", "采集已暂停")
                    self._wake.wait(.2)
                    self._wake.clear()
                    continue
                source = self._config.get("source", "ocr")
                try:
                    if source == "weflow":
                        self._status("searching", "正在连接本机 WeFlow", source)
                        self._run_weflow()
                    elif source == "wechat_db":
                        self._status("searching", "正在连接微信本地数据库", source)
                        self._run_wechat_db()
                    else:
                        if self._cap is None:
                            self._status("searching", "正在查找微信窗口", "ocr")
                        cap = self._ensure_capture()
                        if self._once.is_set():
                            full, at = self._frame(wait=3)
                            self._read_frame(full, at)
                            self._once.clear()
                        else:
                            full = cap.settled()
                            if full is not None:
                                self._read_frame(full, time.monotonic())
                        self._stop.wait(.08)
                except Exception as exc:
                    self._active_title = ""
                    self._status("error", str(exc)[:200], source)
                    if self._once.is_set():
                        self._once.clear()
                    self._stop.wait(1)
        finally:
            try:
                self._close_capture()
            finally:
                if self._client:
                    self._client.close()
                    self._client = None
                while not self._requests.empty():
                    try:
                        task = self._requests.get_nowait()
                    except queue.Empty:
                        break
                    task["error"] = "采集服务已停止"
                    task["done"].set()
