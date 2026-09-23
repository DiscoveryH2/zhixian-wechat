import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from app import auto_send


class _FakeUser32:
    foreground = 123
    def IsWindow(self, hwnd): return True
    def IsWindowVisible(self, hwnd): return True
    def IsIconic(self, hwnd): return False
    def GetForegroundWindow(self): return self.foreground


class _FakeClock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SendVerifiedTests(unittest.TestCase):
    def setUp(self):
        self.user32 = _FakeUser32()
        self.presses = []
        self.verify_calls = []
        self.inspect_calls = []
        self.frames = None
        self.clock = _FakeClock()
        clock_patch = patch.object(
            auto_send, "time",
            SimpleNamespace(monotonic=self.clock.monotonic,
                            sleep=self.clock.sleep))
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        fill_patch = patch.object(auto_send.fill_service, "fill")
        self.fill_mock = fill_patch.start()
        self.addCleanup(fill_patch.stop)
        def simulated_fill(*args, **kwargs):
            self.clock.sleep(0.01)
            return {"success": True}
        self.fill_mock.side_effect = simulated_fill

        self.patches = [
            patch.object(auto_send, "libraries", return_value=(self.user32, None, None)),
            patch.object(auto_send, "process_name", return_value="wechat.exe"),
            patch.object(auto_send, "window_title", return_value="目标联系人 - 微信"),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def make_frames(self, post_text="你好", *, post_focused=True,
                    stale_post=False, pre_text="", pre_focused=True):
        frames = [pre_text, post_text, post_text]
        calls = [0]

        def inspect():
            self.inspect_calls.append(True)
            index = calls[0]
            calls[0] += 1
            return {"text": frames[min(index, len(frames) - 1)],
                    "focused": pre_focused if index == 0 else post_focused,
                    "at": 90.0 if index and stale_post else self.clock.now}
        return inspect

    def call(self, inspect, verify=None):
        def verify_ok():
            self.verify_calls.append(True)
            if verify:
                verify()
        return auto_send.send_verified(
            123, (0, 0, 400, 300), "你好", verify=verify_ok,
            inspect_composer=inspect, expected_window_title="目标联系人 - 微信",
            press_send=lambda: self.presses.append("enter"))

    def test_existing_composer_draft_is_never_overwritten_or_sent(self):
        with self.assertRaisesRegex(RuntimeError, "已有草稿"):
            self.call(self.make_frames(pre_text="用户正在编辑"))
        self.fill_mock.assert_not_called()
        self.assertEqual(self.presses, [])

    def test_wrong_ocr_draft_never_sends(self):
        with self.assertRaisesRegex(RuntimeError, "不一致"):
            self.call(self.make_frames(post_text="你好呀"))
        self.assertEqual(self.presses, [])

    def test_stale_observation_never_sends(self):
        with self.assertRaisesRegex(RuntimeError, "过期"):
            self.call(self.make_frames(stale_post=True))
        self.assertEqual(self.presses, [])

    def test_focus_loss_never_sends(self):
        with self.assertRaisesRegex(RuntimeError, "焦点"):
            self.call(self.make_frames(post_focused=False))
        self.assertEqual(self.presses, [])

    def test_changed_target_never_sends(self):
        def changed():
            if len(self.verify_calls) == 3:
                raise RuntimeError("conversation changed")
        with self.assertRaisesRegex(RuntimeError, "conversation changed"):
            self.call(self.make_frames(), verify=changed)
        self.assertEqual(self.presses, [])

    def test_success_presses_once_and_reports_unconfirmed_delivery(self):
        result = self.call(self.make_frames())
        self.assertEqual(self.presses, ["enter"])
        self.assertEqual(result["status"], "sent_unconfirmed")
        self.assertGreaterEqual(len(self.verify_calls), 4)

    def test_initial_assistant_foreground_and_unfocused_composer_are_allowed(self):
        self.user32.foreground = 999

        def simulated_fill(*args, **kwargs):
            self.clock.sleep(0.01)
            self.user32.foreground = 123
            return {"success": True}

        self.fill_mock.side_effect = simulated_fill
        result = self.call(self.make_frames(pre_focused=False))
        self.assertEqual(result["status"], "sent_unconfirmed")
        self.assertEqual(self.presses, ["enter"])


if __name__ == "__main__":
    unittest.main()
