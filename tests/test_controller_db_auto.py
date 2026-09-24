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
    def test_saved_database_allowlist_starts_after_catalog_cache_is_empty(self):
        with tempfile.TemporaryDirectory() as temp, patch("desk.capture_service.CaptureService"):
            ctrl = Controller(Path(temp))
            try:
                ctrl.store.config['source'] = 'wechat_db'
                saved = {'session_id': 'room@chatroom', 'title': '合成群聊',
                         'type': 'group', 'source': 'wechat_db'}
                ctrl.auto_allowlist = [saved]
                self.assertNotIn('room@chatroom', ctrl.catalog_items)
                class Reader:
                    def sessions(self, limit=500, offset=0):
                        return ([{'id': 'room@chatroom', 'name': '合成群聊', 'type': 'group'}]
                                if offset == 0 else [])
                with patch.object(ctrl, '_wechat_db_reader', return_value=Reader()):
                    ctrl._configure_auto({'allowlist': [saved], 'group_mode': 'mention_only'})
                self.assertEqual(ctrl.auto_allowlist[0]['session_id'], 'room@chatroom')
                self.assertEqual(ctrl.auto_allowlist[0]['source'], 'wechat_db')
            finally:
                ctrl.close()

    def test_old_ocr_group_allowlist_migrates_to_unique_database_session(self):
        with tempfile.TemporaryDirectory() as temp, patch("desk.capture_service.CaptureService"):
            ctrl = Controller(Path(temp))
            try:
                ctrl.store.config['source'] = 'wechat_db'
                old = {'session_id': 'ocr:synthetic', 'title': '合成群聊 (12)',
                       'type': 'group', 'source': 'ocr'}
                ctrl.auto_allowlist = [old]
                class Reader:
                    def sessions(self, limit=500, offset=0):
                        return ([{'id': 'room@chatroom', 'name': '合成群聊', 'type': 'group'}]
                                if offset == 0 else [])
                with patch.object(ctrl, '_wechat_db_reader', return_value=Reader()):
                    ctrl._configure_auto({'allowlist': [old], 'group_mode': 'mention_only'})
                self.assertEqual(ctrl.auto_allowlist[0]['session_id'], 'room@chatroom')
                self.assertEqual(ctrl.auto_allowlist[0]['source'], 'wechat_db')
                self.assertFalse(ctrl.auto_policy['enabled'])
                ctrl.store.secrets['api_key'] = 'synthetic-test-key'
                ctrl.status['capture'] = 'live'
                with patch('core.client.resolve_decision', return_value=object()), \
                     patch('core.client.resolve_reply', return_value=object()):
                    started = ctrl._start_auto({'acknowledge_send': True})
                self.assertTrue(started['enabled'])
            finally:
                ctrl.close()

    def test_ambiguous_old_ocr_title_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp, patch("desk.capture_service.CaptureService"):
            ctrl = Controller(Path(temp))
            try:
                ctrl.store.config['source'] = 'wechat_db'
                old = {'session_id': 'ocr:synthetic', 'title': '合成群聊',
                       'type': 'group', 'source': 'ocr'}
                ctrl.auto_allowlist = [old]
                class Reader:
                    def sessions(self, limit=500, offset=0):
                        return ([{'id': 'a@chatroom', 'name': '合成群聊', 'type': 'group'},
                                 {'id': 'b@chatroom', 'name': '合成群聊', 'type': 'group'}]
                                if offset == 0 else [])
                with patch.object(ctrl, '_wechat_db_reader', return_value=Reader()):
                    with self.assertRaisesRegex(ValueError, '无法唯一对应'):
                        ctrl._configure_auto({'allowlist': [old]})
                self.assertEqual(ctrl.auto_allowlist, [old])
            finally:
                ctrl.close()

    def test_direct_group_at_uses_normal_confidence_gate(self):
        with tempfile.TemporaryDirectory() as temp, patch("desk.capture_service.CaptureService") as capture_type:
            ctrl = Controller(Path(temp))
            try:
                sid = "synthetic@chatroom"
                previous = {"id": "group-old", "text": "之前聊过", "side": "me", "kind": "text"}
                incoming = {"id": "group-new", "text": "@合成本人 这个问题怎么办？", "side": "other",
                            "sender": "wxid-friend", "kind": "text", "historical": False,
                            "directed_to_me": True}
                ctrl.store.config["source"] = "wechat_db"
                ctrl.sessions[sid] = {"id": sid, "title": "合成群聊", "source": "wechat_db",
                                      "type": "group", "messages": [previous, incoming]}
                ctrl.live_id = "another-session"
                ctrl.status["capture"] = "live"
                ctrl.auto_allowlist = [{"session_id": sid, "title": "合成群聊", "type": "group",
                                        "source": "wechat_db"}]
                ctrl.auto_policy.update(enabled=True, session_ids=[sid], group_mode="all")
                trigger = {"session_id": sid, "message": incoming,
                           "event": {"source": "wechat_db", "historical": False, "incoming": True},
                           "epoch": ctrl.auto_epoch, "type": "group"}
                analysis = {"risk": 1, "candidates": [{"text": "我可以帮你看看"}], "best_index": 0}
                judgment = {"should_reply": True, "target_message_id": incoming["id"], "confidence": .6}
                completed = Future()
                completed.set_result({"status": "sent_unconfirmed"})
                with patch.object(ctrl.auto_guard, "claim", return_value=Decision(True, "claimed")), \
                     patch.object(ctrl.pool, "submit", return_value=completed) as submit:
                    ctrl._auto_dispatch(trigger, analysis, judgment)
                self.assertIs(submit.call_args.args[0], capture_type.return_value.send_db)
                self.assertEqual(submit.call_args.args[2:], (sid, incoming["id"]))
            finally:
                ctrl.close()

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
                ctrl.live_id = "another-session-updated-after-this-one"
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
