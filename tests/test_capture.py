import io
import importlib.util
import json
import sys
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from desk.capture_service import CaptureService, ScreenHistory, check_fill_target, session_id
from desk.weflow import WeFlowClient, normalize_message, validate_url


class MessageTests(unittest.TestCase):
    def test_direction_and_repeat_overlap(self):
        history = ScreenHistory("demo")
        initial, historical = history.update([("her", None, "你好", 1), ("me", None, "好的", 2)])
        self.assertTrue(historical)
        self.assertEqual([m["side"] for m in initial], ["other", "me"])
        again, _ = history.update([("her", None, "你好", 1), ("me", None, "好的", 2)])
        self.assertEqual(again, [])
        new, historical = history.update([("me", None, "好的", 1), ("her", "小明", "你好", 2)])
        self.assertFalse(historical)
        self.assertEqual(len(new), 1)
        self.assertNotEqual(initial[0]["id"], new[0]["id"])

    def test_repeated_bubbles_are_not_globally_deduped(self):
        history = ScreenHistory("demo")
        history.update([("her", None, "好的", 1)])
        added, historical = history.update([("her", None, "好的", 1), ("her", None, "好的", 2)])
        self.assertEqual(len(added), 1)
        self.assertFalse(historical)

    def test_no_overlap_is_historical_and_similar_titles_distinct(self):
        history = ScreenHistory("demo")
        history.update([("her", None, "今天会议", 1)])
        added, historical = history.update([("her", None, "去年消息", 1)])
        self.assertTrue(historical)
        self.assertTrue(added[0]["historical"])
        self.assertNotEqual(session_id("项目小分队"), session_id("项目小分认"))

    def test_weflow_direction_and_stable_id_across_history_and_stream(self):
        raw = {"serverId": "123", "isSend": 0, "content": "内容", "timestamp": 100}
        first = normalize_message(raw, "session")
        replay = normalize_message({"rawid": "123", "content": "内容", "timestamp": 100}, "session", incoming=True)
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(replay["side"], "other")
        self.assertEqual(normalize_message({**raw, "isSend": 1}, "s")["side"], "me")
        self.assertIsNone(normalize_message({"content": "unknown direction"}, "s"))

    def test_weflow_event_dedup(self):
        events = []
        service = CaptureService(lambda k, p: events.append((k, p)))
        raw = {"rawid": "123", "content": "内容", "timestamp": 100}
        service._weflow_messages("wxid_a", "联系人", [raw], incoming=True)
        service._weflow_messages("wxid_a", "联系人", [raw], incoming=True)
        emitted = [p for k, p in events if k == "messages"]
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["messages"][0]["side"], "other")


class SafetyTests(unittest.TestCase):
    def test_fill_rejects_unknown_stale_or_different_target(self):
        for args in [("甲", "", 0), ("甲", "乙", 0), ("甲", "甲", 3), ("甲", "甲", 0, False)]:
            with self.subTest(args=args), self.assertRaises(RuntimeError):
                check_fill_target(*args)
        self.assertTrue(check_fill_target("甲", "甲", .1))

    def test_unsafe_fill_cannot_paste_without_verifier(self):
        from app.fill import fill
        with self.assertRaisesRegex(RuntimeError, "缺少最新会话核验"):
            fill(1, (1, 2, 3, 4), "reply")

    def test_low_level_fill_checks_target_before_clipboard(self):
        from app.fill import fill
        u = Mock()
        u.IsWindow.return_value = True
        u.IsWindowVisible.return_value = True
        u.IsIconic.return_value = False
        verifier = Mock(side_effect=RuntimeError("会话不一致"))
        with patch("app.fill.libraries", return_value=(u, Mock(), Mock())), \
             patch("app.fill.window_title", return_value="微信"), \
             patch("app.fill.process_name", return_value="weixin.exe"), \
             patch("app.fill.set_clipboard") as clipboard:
            with self.assertRaisesRegex(RuntimeError, "会话不一致"):
                fill(1, (1, 2, 300, 400), "reply", verify=verifier)
            clipboard.assert_not_called()
            u.keybd_event.assert_not_called()

    def test_unknown_title_clears_previous_chat(self):
        service = CaptureService(lambda *args: None)
        service._active_title = "上一位联系人"
        fake_frame = Mock()
        fake_frame.__getitem__ = Mock(return_value=object())
        capture_module = types.SimpleNamespace(chat_area=lambda _: (1, 2, 3, 4, 0, 0))
        ocr_module = types.SimpleNamespace(Reader=Mock(), read_title=lambda _: "",
                                           sidebar_tail_matches=lambda *args, **kwargs: False)
        with patch.dict(sys.modules, {"app.capture": capture_module, "app.ocr": ocr_module}):
            with self.assertRaisesRegex(RuntimeError, "无法识别会话标题"):
                service._read_frame(fake_frame, time.monotonic())
        self.assertEqual(service._active_title, "")

    def test_url_rejects_remote_and_credentials(self):
        self.assertEqual(validate_url("http://127.0.0.1:5031/"), "http://127.0.0.1:5031")
        self.assertEqual(validate_url("http://[::1]:5031"), "http://[::1]:5031")
        for url in ("https://example.org", "http://192.168.1.5:5031", "file:///x", "http://x:y@localhost", "http://localhost?token=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)


class TransportAndLifecycleTests(unittest.TestCase):
    def test_health_is_only_request_during_discovery_and_has_no_token(self):
        client = WeFlowClient(token="secret")
        with patch.object(client, "get", return_value={"status": "ok"}) as get:
            self.assertTrue(client.health())
            get.assert_called_once_with("/health", authenticated=False)

    def test_sse_reconnect_id_and_incoming_event(self):
        client = WeFlowClient(token="secret")
        payload = json.dumps({"event": "message.new", "rawid": "m1", "content": "hello"})
        body = (": ping\n\nid: 22\nevent: message.new\ndata: " + payload + "\n\n").encode()
        with patch.object(client, "_open", return_value=io.BytesIO(body)):
            events = list(client.events(threading.Event()))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "message.new")
        self.assertEqual(client.last_event_id, "22")
        self.assertIsNone(client._stream)

    def test_pause_stop_releases_capture_and_worker(self):
        events = []
        cap = Mock()
        cap.settled.return_value = None
        service = CaptureService(lambda *args: events.append(args))

        def ensure():
            service._cap = cap
            return cap

        with patch.object(service, "_ensure_capture", side_effect=ensure):
            service.start({"source": "ocr"})
            deadline = time.monotonic() + 2
            while service._cap is None and time.monotonic() < deadline:
                time.sleep(.01)
            service.pause()
            deadline = time.monotonic() + 2
            while service._cap is not None and time.monotonic() < deadline:
                time.sleep(.01)
            service.stop()
        self.assertIsNone(service._thread)
        self.assertIsNone(service._cap)
        cap.stop.assert_called()
        cap.wait.assert_called()


@unittest.skipUnless(all(importlib.util.find_spec(name) for name in ("numpy", "PIL", "rapidocr_onnxruntime")),
                     "Synthetic OCR requires the installed production OCR dependencies")
class SyntheticOcrTests(unittest.TestCase):
    def test_synthetic_chinese_title_and_bubble_directions(self):
        """Entirely synthetic image, kept in RAM; never captures a real chat."""
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
        from app.ocr import Reader, read_title
        font_path = Path(__file__).resolve().parents[1] / 'ui/fonts/NotoSansSC-Variable.ttf'
        if not font_path.exists():
            self.skipTest("Run scripts/fetch_font.py for the offline test font")
        font = ImageFont.truetype(str(font_path), 28)
        header = Image.new("RGB", (720, 64), (245, 245, 245))
        ImageDraw.Draw(header).text((25, 12), "项目讨论", font=font, fill=(20, 20, 20))
        self.assertIn("项目", read_title(np.array(header)))
        pane = Image.new("RGB", (720, 320), (245, 245, 245))
        draw = ImageDraw.Draw(pane)
        draw.rounded_rectangle((30, 25, 370, 91), radius=8, fill="white")
        draw.text((48, 40), "明天下午三点开会。", font=font, fill=(15, 15, 15))
        draw.rounded_rectangle((355, 160, 700, 230), radius=8, fill=(149, 236, 105))
        draw.text((375, 179), "好的，稍后回复。", font=font, fill=(15, 15, 15))
        lines = Reader().read(np.array(pane), np.array([245, 245, 245]))
        self.assertTrue(any(w == "her" and "开会" in text for w, _, text, _ in lines), lines)
        self.assertTrue(any(w == "me" and "回复" in text for w, _, text, _ in lines), lines)


class SidebarTailTests(unittest.TestCase):
    """Synthetic pixels and mocked OCR; this never reads a desktop frame."""

    @staticmethod
    def _row(y, text, confidence=.99):
        return ([[80, y], [180, y], [180, y + 12], [80, y + 12]], text, confidence)

    def _check(self, body="最新消息正文", preview=None, sender=None, sidebar_time="14:00",
               chat_time="13:55", min_body_chars=4):
        import numpy as np
        from datetime import datetime
        from app import ocr

        full = np.full((400, 800, 3), 245, dtype=np.uint8)
        # One synthetic selected-chat band in the sidebar.
        full[110:155, 72:400] = (40, 180, 80)
        area = (400, 80, 780, 380)
        headline = [self._row(8, sidebar_time)]
        if sender:
            headline.append(self._row(22, f"{sender}:"))
        headline.append(self._row(38, body if preview is None else preview))
        pane_rows = [self._row(30, chat_time)]

        def fake_engine(image, use_cls=False):
            if image.shape[1] == 328:
                return headline, None
            return pane_rows, None

        lines = [("her", sender, body, 200)]
        with patch.object(ocr, "_engine", return_value=fake_engine):
            return ocr.sidebar_tail_matches(
                full, area, lines, now=datetime(2026, 9, 24, 14, 2),
                min_body_chars=min_body_chars)

    def test_selected_preview_and_recent_chat_time_anchor_match(self):
        self.assertTrue(self._check())

    def test_rejects_old_or_nonmatching_sidebar_preview(self):
        self.assertFalse(self._check(sidebar_time="13:54"))
        self.assertFalse(self._check(preview="另一条完全不同的内容"))

    def test_rejects_short_body_by_default_but_allows_explicit_two_char_mode(self):
        self.assertFalse(self._check(body="好的", min_body_chars=4))
        # A two-character body can pass only with matching selected preview,
        # fresh timestamps, and the normal speaker checks in place.
        self.assertTrue(self._check(body="好的", min_body_chars=2))

    def test_rejects_stale_chat_timestamp_and_wrong_sender(self):
        self.assertFalse(self._check(chat_time="13:40"))
        self.assertFalse(self._check(sender="Alice", preview="Bob: 最新消息正文"))

    def test_group_count_prefix_does_not_break_sender_match(self):
        self.assertTrue(self._check(sender="Alice", preview="[4条]Alice: 最新消息正文"))


class OneShotTests(unittest.TestCase):
    def test_one_shot_does_not_enable_continuous_capture(self):
        service = CaptureService(lambda *args: None)
        cap = Mock()
        cap.settled.return_value = None
        called = threading.Event()
        def read(*args, **kwargs):
            called.set()
        with patch.object(service, "_ensure_capture", return_value=cap), \
             patch.object(service, "_frame", return_value=(object(), time.monotonic())), \
             patch.object(service, "_read_frame", side_effect=read):
            service.one_shot()
            self.assertTrue(called.wait(2))
            self.assertFalse(service._wanted.is_set())
            service.stop()


if __name__ == "__main__":
    unittest.main()
