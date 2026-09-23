"""Synthetic queue and metadata tests; never types into WeChat."""
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from desk.capture_service import CaptureService
from desk.weflow import normalize_message


class SendQueueTests(unittest.TestCase):
    def test_only_explicit_send_mode_reaches_send_worker(self):
        service = CaptureService(lambda *_: None)
        send = {'mode': 'send', 'text': 'synthetic', 'title': 'Synthetic',
                'cancelled': threading.Event(), 'done': threading.Event(),
                'epoch': service._control_epoch}
        fill = {'text': 'synthetic', 'title': 'Synthetic',
                'cancelled': threading.Event(), 'done': threading.Event(),
                'epoch': service._control_epoch}
        service._requests.put(send)
        service._requests.put(fill)
        with patch.object(service, '_send_worker', return_value={'success': True, 'status': 'sent'}) as sender, \
             patch.object(service, '_fill_worker', return_value={'success': True}) as filler:
            service._drain_requests()
        sender.assert_called_once_with(send)
        filler.assert_called_once_with(fill)
        self.assertTrue(send['done'].is_set())
        self.assertTrue(fill['done'].is_set())

    def test_emergency_epoch_invalidates_queued_send(self):
        service = CaptureService(lambda *_: None)
        send = {'mode': 'send', 'text': 'synthetic', 'title': 'Synthetic',
                'cancelled': threading.Event(), 'done': threading.Event(),
                'epoch': service._control_epoch}
        service.cancel_pending_writes()
        service._hwnd, service._native_title = 1, 'WeChat'
        with patch.object(service, '_close_capture'), patch.object(service, '_ensure_capture'), \
             patch('app.ocr.Reader'), patch('app.auto_send.send_verified') as sender:
            with self.assertRaisesRegex(RuntimeError, '已取消'):
                service._send_worker(send)
        sender.assert_not_called()


class MentionMetadataTests(unittest.TestCase):
    def test_only_explicit_boolean_upstream_mention_is_recognized(self):
        base = {'serverId': 'synthetic-id', 'content': 'Synthetic reply?',
                'senderUsername': 'synthetic-friend'}
        self.assertIsNone(normalize_message(base, 'weflow:synthetic', incoming=True)['directed_to_me'])
        self.assertTrue(normalize_message({**base, 'isAtMe': True}, 'weflow:synthetic', incoming=True)['directed_to_me'])
        self.assertFalse(normalize_message({**base, 'isAtMe': False}, 'weflow:synthetic', incoming=True)['directed_to_me'])
        self.assertIsNone(normalize_message({**base, 'isAtMe': 'true'}, 'weflow:synthetic', incoming=True)['directed_to_me'])


if __name__ == '__main__':
    unittest.main()
