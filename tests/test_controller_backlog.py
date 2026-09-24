"""One-time historical reply orchestration with synthetic conversations only."""
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtWidgets import QApplication
from desk.controller import Controller, AUTO_DISCLOSURE

APP = QApplication.instance() or QApplication([])


class CatchUpControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.native = patch('desk.capture_service.CaptureService')
        self.capture_type = self.native.start()
        self.ctrl = Controller(Path(self.temp.name) / 'data')
        self.ctrl.store.secrets['api_key'] = 'synthetic-only'
        self.sid = 'ocr:synthetic-history'
        self.ctrl._on_capture('session', {'id': self.sid, 'title': 'Synthetic contact', 'source': 'ocr'})
        self.ctrl._on_capture('messages', {'session_id': self.sid, 'title': 'Synthetic contact',
            'source': 'ocr', 'historical': True, 'messages': [
                {'id': 'prior-me', 'side': 'me', 'text': 'Prior context.', 'source': 'ocr',
                 'historical': True, 'kind': 'text', 'timestamp': None},
                {'id': 'last-other', 'side': 'other', 'text': 'Could we continue?',
                 'sender': 'Synthetic contact', 'source': 'ocr', 'historical': True,
                 'kind': 'text', 'timestamp': None}]})
        self.ctrl.handle('configure_auto_reply', {'allowlist': [
            {'session_id': self.sid, 'type': 'private'}], 'hourly_limit': 1, 'daily_limit': 1})

    def tearDown(self):
        self.ctrl.close()
        APP.processEvents()
        self.native.stop()
        self.temp.cleanup()

    def drain(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            APP.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail('Qt catch-up callback did not arrive')

    def prepared(self, should=True):
        return {'judged': {'should_reply': should, 'target_message_id': 'last-other',
                           'reason': 'jev_typed_decision'},
                'analysis': {'should_reply': True, 'risk': 1, 'best_index': 0,
                             'candidates': [{'text': 'Yes, we can continue.'}] } if should else None}

    def test_requires_acknowledgment_and_current_allowed_chat(self):
        with self.assertRaises(ValueError):
            self.ctrl.handle('catch_up_auto_reply', {'session_id': self.sid})
        with self.assertRaises(ValueError):
            self.ctrl.handle('catch_up_auto_reply', {'session_id': 'import:synthetic',
                                                      'acknowledge_send': True})
        self.ctrl._on_capture('session', {'id': 'ocr:other', 'title': 'Other chat', 'source': 'ocr'})
        with self.assertRaises(ValueError):
            self.ctrl.handle('catch_up_auto_reply', {'session_id': self.sid,
                                                      'acknowledge_send': True})
        self.capture_type.return_value.send.assert_not_called()

    def test_replies_once_with_exact_disclosure_and_no_message_text_in_status(self):
        self.capture_type.return_value.send.return_value = {'success': True, 'status': 'sent'}
        with patch.object(self.ctrl, '_prepare_catchup_task', return_value=self.prepared()):
            result = self.ctrl.handle('catch_up_auto_reply', {'session_id': self.sid,
                                                                'acknowledge_send': True})
            self.assertTrue(result['started'])
            self.drain(lambda: self.ctrl.catchup['status'] == 'sent')
            first = self.capture_type.return_value.send.call_args.args
            self.assertEqual(first, ('Yes, we can continue.' + AUTO_DISCLOSURE,
                                     'Synthetic contact', 'Could we continue?', '', 'Prior context.'))
            public = self.ctrl.snapshot()['auto_reply']
            self.assertEqual(public['catchup']['status'], 'sent')
            self.assertNotIn('Could we continue?', str(public['catchup']) + str(public['recent']))
            self.ctrl.handle('catch_up_auto_reply', {'session_id': self.sid,
                                                      'acknowledge_send': True})
            self.drain(lambda: self.ctrl.catchup['status'] == 'skipped')
        self.assertEqual(self.capture_type.return_value.send.call_count, 1)

    def test_no_reply_judgment_skips_without_generation_or_send(self):
        with patch.object(self.ctrl, '_prepare_catchup_task', return_value=self.prepared(False)):
            self.ctrl.handle('catch_up_auto_reply', {'session_id': self.sid,
                                                      'acknowledge_send': True})
            self.drain(lambda: self.ctrl.catchup['status'] == 'skipped')
        self.capture_type.return_value.send.assert_not_called()

    def test_emergency_stop_invalidates_inflight_historical_judgment(self):
        waiting = Future()
        with patch.object(self.ctrl.pool, 'submit', return_value=waiting):
            self.ctrl.handle('catch_up_auto_reply', {'session_id': self.sid,
                                                      'acknowledge_send': True})
            self.ctrl.handle('stop_auto_reply', {'emergency': True})
            waiting.set_result(self.prepared())
            self.drain(lambda: self.ctrl.catchup['status'] == 'skipped')
        self.capture_type.return_value.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
