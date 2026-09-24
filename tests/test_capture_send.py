"""Synthetic queue and metadata tests; never types into WeChat."""
import sys
import json
import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from desk.capture_service import CaptureService
from desk.weflow import normalize_message


class SendQueueTests(unittest.TestCase):
    def test_send_worker_audits_its_own_enter_call(self):
        with tempfile.TemporaryDirectory() as root:
            audit = Path(root) / 'send-audit.jsonl'
            service = CaptureService(lambda *_: None, audit)
            service._hwnd, service._native_title = 123, 'WeChat'
            service._cap = SimpleNamespace(area=(0, 0, 300, 150, np.zeros(3, dtype=np.uint8), 0))
            frame = np.zeros((300, 300, 3), dtype=np.uint8)
            task = {'mode': 'send', 'text': 'Synthetic reply', 'title': 'Synthetic group',
                    'expected_incoming': 'Synthetic question', 'expected_sender': 'Synthetic member',
                    'expected_previous': '', 'cancelled': threading.Event(),
                    'epoch': service._control_epoch}

            def fake_sender(*args, **kwargs):
                kwargs['press_send']()
                return {'success': True, 'status': 'sent_unconfirmed'}

            with patch.object(service, '_close_capture'), patch.object(service, '_ensure_capture'), \
                 patch.object(service, '_frame', side_effect=lambda wait=0: (frame, time.monotonic())), \
                 patch.object(service, '_read_frame', side_effect=lambda *_a, **_k: ('Synthetic group', (0, 0, 300, 150), time.monotonic())), \
                 patch('app.ocr.Reader') as reader, patch('app.ocr._engine') as engine, \
                 patch('app.winapi.libraries') as libraries, patch('app.winapi.window_title', return_value='WeChat'), \
                 patch('app.auto_send._assert_target'), patch('app.auto_send._press_enter') as enter, \
                 patch('app.auto_send.send_verified', side_effect=fake_sender):
                reader.return_value.read.return_value = [('her', 'Synthetic member', 'Synthetic question', 10)]
                engine.return_value.return_value = ([], None)
                libraries.return_value[0].GetForegroundWindow.return_value = 123
                result = service._send_worker(task)
            self.assertEqual(result['status'], 'sent_unconfirmed')
            enter.assert_called_once_with()
            stages = [json.loads(line)['stage'] for line in audit.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(stages, ['before_enter', 'enter_called', 'send_result'])

    def test_sender_audit_contains_process_provenance_without_chat_content(self):
        with tempfile.TemporaryDirectory() as root:
            audit = Path(root) / 'send-audit.jsonl'
            service = CaptureService(lambda *_: None, audit)
            service._send_audit('enter_called', {'title': 'Synthetic private title',
                                                  'text': 'Synthetic private draft'}, 'sent_unconfirmed')
            service._send_audit('send_result', {'title': 'Synthetic private title',
                                                 'text': 'Synthetic private draft'}, 'sent')
            raw = audit.read_text(encoding='utf-8')
            rows = [json.loads(line) for line in raw.splitlines()]
            row = rows[0]
            self.assertEqual(row['stage'], 'enter_called')
            self.assertIsInstance(row['pid'], int)
            self.assertEqual(rows[0]['target_ref'], rows[1]['target_ref'])
            self.assertNotIn('Synthetic private title', raw)
            self.assertNotIn('Synthetic private draft', raw)

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
