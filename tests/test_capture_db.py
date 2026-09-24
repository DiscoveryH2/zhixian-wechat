import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from desk.capture_service import CaptureService


class FakeWechatDB:
    def __init__(self):
        self.talker = "wxid_fake"
        self.rows = [self._message(1, "旧消息", int(time.time()) - 20)]

    @staticmethod
    def _message(local_id, text, timestamp, side="other", kind="text"):
        return {"id": f"fake-{local_id}", "side": side, "sender": "wxid_friend",
                "text": text, "kind": kind, "timestamp": timestamp,
                "source": "wechat_db", "historical": True,
                "_cursor": (local_id, timestamp, local_id)}

    def sessions(self, limit=100, offset=0, query=None):
        return [{"id": self.talker, "name": "合成联系人", "type": "private",
                 "source": "wechat_db", "historical": True}]

    def latest_session_heads(self, limit=200, offset=0):
        if offset:
            return []
        row = self.rows[-1]
        cursor = row["_cursor"]
        return [{"id": self.talker, "sort_timestamp": cursor[0], "timestamp": cursor[1],
                 "last_msg_local_id": cursor[2], "last_msg_type": 1,
                 "source": "wechat_db", "historical": True}]

    def messages(self, session_id, limit=100, before=None):
        if session_id != self.talker:
            return []
        return sorted(self.rows, key=lambda row: row["_cursor"], reverse=True)[:limit]

    def append(self, local_id, text, timestamp=None, side="other", kind="text"):
        self.rows.append(self._message(local_id, text, timestamp or int(time.time()), side, kind))


class WechatDBCaptureTests(unittest.TestCase):
    def test_tray_hidden_window_is_restored_after_sender_check(self):
        class FakeUser32:
            visible = False
            iconic = False
            def EnumWindows(self, callback, value):
                callback(123, value)
            def IsWindowVisible(self, _hwnd):
                return self.visible
            def IsIconic(self, _hwnd):
                return self.iconic
            def ShowWindow(self, _hwnd, command):
                self.visible = command != 0
                return True

        user32 = FakeUser32()
        service = CaptureService(lambda *_: None)
        with patch("app.winapi.libraries", return_value=(user32, None, None)), \
             patch("app.winapi.process_name", return_value="weixin.exe"), \
             patch("app.winapi.window_title", return_value="微信"):
            with service._temporary_db_send_window() as hwnd:
                self.assertEqual(hwnd, 123)
                self.assertTrue(user32.visible)
            self.assertFalse(user32.visible)

    def test_db_sender_rejects_outgoing_head_before_any_capture(self):
        fake = FakeWechatDB()
        fake.rows[-1]["side"] = "me"
        service = CaptureService(lambda *_: None, db_source_factory=lambda: fake)
        service._config = {"source": "wechat_db"}
        task = {"text": "合成回复（以上内容为知弦生成）", "session_id": fake.talker,
                "message_id": fake.rows[-1]["id"], "title": fake.talker,
                "cancelled": threading.Event(), "epoch": service._control_epoch}
        with patch.object(service, "_ensure_capture", side_effect=AssertionError("No screen read permitted")):
            with self.assertRaisesRegex(RuntimeError, "已有更新"):
                service._send_db_visible_worker(task, expected_hwnd=0)

    def test_db_source_emits_initial_history_then_only_recent_new_inbound_text_live(self):
        fake = FakeWechatDB()
        events = []
        ready = threading.Event()

        def callback(kind, payload):
            events.append((kind, payload))
            if kind == "status" and payload.get("capture") == "live":
                ready.set()

        service = CaptureService(callback, db_source_factory=lambda: fake)
        with patch.object(service, "_ensure_capture", side_effect=AssertionError("OCR/WGC must not start")), \
             patch.object(service, "_read_frame", side_effect=AssertionError("OCR must not read chats")):
            service.start({"source": "wechat_db", "db_poll_seconds": .25})
            self.assertTrue(ready.wait(2))
            deadline = time.monotonic() + 2
            while not any(kind == "messages" for kind, _ in events) and time.monotonic() < deadline:
                time.sleep(.01)
            initial = [payload for kind, payload in events if kind == "messages"]
            self.assertEqual(len(initial), 1)
            self.assertTrue(initial[0]["historical"])
            self.assertTrue(all(message["historical"] for message in initial[0]["messages"]))

            fake.append(2, "新的具体问题")
            deadline = time.monotonic() + 3
            while not any(kind == "messages" and not payload["historical"] for kind, payload in events) \
                    and time.monotonic() < deadline:
                time.sleep(.01)
            live = [payload for kind, payload in events if kind == "messages" and not payload["historical"]]
            self.assertEqual(len(live), 1)
            self.assertEqual(live[0]["messages"][0]["text"], "新的具体问题")
            self.assertEqual(live[0]["messages"][0]["side"], "other")
            self.assertEqual(live[0]["messages"][0]["kind"], "text")

            fake.append(3, "旧语音", int(time.time()) - 301, kind="voice")
            deadline = time.monotonic() + 3
            while len([1 for kind, payload in events if kind == "messages" and payload["messages"]
                       and payload["messages"][0]["id"] == "fake-3"]) == 0 and time.monotonic() < deadline:
                time.sleep(.01)
            stale = [payload for kind, payload in events if kind == "messages" and payload["messages"]
                     and payload["messages"][0]["id"] == "fake-3"]
            self.assertEqual(len(stale), 1)
            self.assertTrue(stale[0]["historical"])
            service.stop()

    def test_unknown_or_duplicate_head_never_emits_live_message(self):
        fake = FakeWechatDB()
        events = []
        service = CaptureService(lambda kind, payload: events.append((kind, payload)),
                                 db_source_factory=lambda: fake)
        with patch.object(service, "_ensure_capture", side_effect=AssertionError("OCR/WGC must not start")):
            service.start({"source": "wechat_db", "db_poll_seconds": .25})
            deadline = time.monotonic() + 2
            while not any(kind == "status" and payload.get("capture") == "live" for kind, payload in events) \
                    and time.monotonic() < deadline:
                time.sleep(.01)
            fake.append(2, "只应出现一次")
            deadline = time.monotonic() + 2
            while not any(kind == "messages" and not payload["historical"] for kind, payload in events) \
                    and time.monotonic() < deadline:
                time.sleep(.01)
            count = sum(1 for kind, payload in events if kind == "messages" and payload["messages"]
                        and payload["messages"][0]["id"] == "fake-2")
            self.assertEqual(count, 1)
            service.stop()


if __name__ == "__main__":
    unittest.main()
