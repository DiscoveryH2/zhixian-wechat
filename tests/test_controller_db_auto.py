"""Database-triggered replies use Zhixian's own DB-bound sender path."""
import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from PySide6.QtWidgets import QApplication
from desk.auto_reply import Decision
from desk.controller import Controller


APP = QApplication.instance() or QApplication([])


class ControllerDBAutoTests(unittest.TestCase):
    def test_database_event_dispatches_db_sender_with_disclosure(self):
        with tempfile.TemporaryDirectory() as temp, patch("desk.capture_service.CaptureService") as capture_type:
            ctrl = Controller(Path(temp))
            try:
                sid = "wxid-synthetic"
                earlier = {"id": "db-old", "text": "之前的合成上下文", "side": "me", "kind": "text"}
                incoming = {"id": "db-new", "text": "现在方便吗？", "side": "other",
                            "sender": "wxid-friend", "kind": "text", "historical": False}
                ctrl.store.config["source"] = "wechat_db"
                ctrl.sessions[sid] = {"id": sid, "title": "合成联系人", "source": "wechat_db",
                                      "type": "private", "messages": [earlier, incoming]}
                ctrl.live_id = sid
                ctrl.status["capture"] = "live"
                ctrl.auto_allowlist = [{"session_id": sid, "title": "合成联系人", "type": "private",
                                        "source": "wechat_db"}]
                ctrl.auto_policy.update(enabled=True, session_ids=[sid])
                trigger = {"session_id": sid, "message": incoming,
                           "event": {"source": "wechat_db", "historical": False, "incoming": True},
                           "epoch": ctrl.auto_epoch, "type": "private"}
                analysis = {"risk": 1, "candidates": [{"text": "方便，你说"}], "best_index": 0}
                judgment = {"should_reply": True, "target_message_id": incoming["id"]}
                completed = Future()
                completed.set_result({"status": "sent_unconfirmed"})
                with patch.object(ctrl.auto_guard, "claim", return_value=Decision(True, "claimed")), \
                     patch.object(ctrl.pool, "submit", return_value=completed) as submit:
                    ctrl._auto_dispatch(trigger, analysis, judgment)
                self.assertIs(submit.call_args.args[0], capture_type.return_value.send_db)
                self.assertEqual(submit.call_args.args[2:], (sid, incoming["id"]))
                self.assertTrue(submit.call_args.args[1].endswith("（以上内容为知弦生成）"))
                self.assertFalse(capture_type.return_value.send.called)
            finally:
                ctrl.close()


if __name__ == "__main__":
    unittest.main()
