"""Controller contracts for the read-only, non-screenshot database source."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from PySide6.QtWidgets import QApplication
from desk.controller import Controller


APP = QApplication.instance() or QApplication([])


class FakeDBReader:
    def __init__(self):
        self.catalog = [
            {"id": "wxid-synthetic", "name": "合成联系人", "type": "private", "preview": "合成摘要", "timestamp": 20},
            {"id": "group@chatroom", "name": "合成群聊", "type": "group", "preview": "[图片]", "timestamp": 10,
             "last_msg_type": 3},
        ]

    def sessions(self, limit=100, offset=0, query=None):
        return self.catalog[offset:offset + limit]

    def messages(self, session_id, limit=100, before=None):
        rows = [{"id": f"synthetic-{index}", "text": f"合成消息 {index}", "side": "other",
                 "sender": "wxid-synthetic", "kind": "text", "timestamp": index,
                 "source": "wechat_db", "historical": True, "_cursor": (index, index, index)}
                for index in range(4, 0, -1)]
        if before:
            rows = [row for row in rows if row["_cursor"] < tuple(before)]
        return rows[:limit]


class ControllerWechatDBTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.capture_patch = patch("desk.capture_service.CaptureService")
        self.capture_patch.start()
        self.ctrl = Controller(Path(self.temp.name))
        self.ctrl.store.config["source"] = "wechat_db"
        self.reader_patch = patch.object(Controller, "_wechat_db_reader", return_value=FakeDBReader())
        self.reader_patch.start()

    def tearDown(self):
        self.ctrl.close()
        self.reader_patch.stop()
        self.capture_patch.stop()
        self.temp.cleanup()

    def test_global_directory_is_paged_and_searchable_without_capture(self):
        first = self.ctrl._list_sessions_task(self.ctrl.store.full_config(), "", None, 1, "all")
        self.assertEqual(first["source"], "wechat_db")
        self.assertEqual(first["items"][0]["title"], "合成联系人")
        self.assertTrue(first["has_more"])
        second = self.ctrl._list_sessions_task(self.ctrl.store.full_config(), "", first["next_cursor"], 1, "all")
        self.assertEqual(second["items"][0]["title"], "合成群聊")
        matched = self.ctrl._list_sessions_task(self.ctrl.store.full_config(), "群聊", None, 10, "wechat_db")
        self.assertEqual([item["id"] for item in matched["items"]], ["group@chatroom"])
        self.assertEqual(matched["items"][0]["last_message_kind"], "image")

    def test_selected_history_is_chronological_and_cannot_claim_live(self):
        self.ctrl._list_sessions_task(self.ctrl.store.full_config(), "", None, 5, "wechat_db")
        page = self.ctrl._session_first_page(self.ctrl.store.full_config(), "wxid-synthetic")
        self.assertEqual(page["session"]["source"], "wechat_db")
        self.assertEqual([row["timestamp"] for row in page["page"]["items"]], [1, 2, 3, 4])
        self.assertTrue(all(row["historical"] for row in page["page"]["items"]))
        self.assertEqual(self.ctrl.capture.send.call_count, 0)


if __name__ == "__main__":
    unittest.main()
