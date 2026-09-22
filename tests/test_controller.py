import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtWidgets import QApplication
from desk.controller import Controller

app = QApplication.instance() or QApplication([])


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.mock = patch('desk.capture_service.CaptureService')
        self.mock.start()
        self.ctrl = Controller(Path(self.temp.name))

    def tearDown(self):
        self.ctrl.close()
        self.mock.stop()
        self.temp.cleanup()

    def incoming(self, ident='chat:a', text='测试消息', mid='1', side='other'):
        self.ctrl._on_capture('messages', {'session_id': ident, 'title': '测试联系人', 'source': 'ocr',
                              'messages': [{'id': mid, 'text': text, 'side': side, 'sender': '对方', 'timestamp': None}]})

    def test_duplicate_events_dont_duplicate_context(self):
        self.incoming()
        self.incoming()
        self.assertEqual(len(self.ctrl.sessions['chat:a']['messages']), 1)

    def test_repeat_text_with_different_ids_is_preserved(self):
        self.incoming(mid='1')
        self.incoming(mid='2')
        self.assertEqual(len(self.ctrl.sessions['chat:a']['messages']), 2)

    def test_outgoing_messages_dont_trigger_auto_analysis(self):
        self.ctrl.store.secrets['api_key'] = 'dummy'
        self.incoming(side='me')
        self.assertIsNone(self.ctrl.pending)

    def test_stale_analysis_is_not_published(self):
        self.incoming()
        fingerprint = self.ctrl.fingerprint(self.ctrl.sessions['chat:a'])
        self.incoming(mid='2', text='更新内容')
        self.ctrl._on_analysis('chat:a', 0, fingerprint, {'candidates': []}, None)
        self.assertNotIn('chat:a', self.ctrl.results)

    def test_pause_invalidates_inflight_results(self):
        self.incoming()
        fingerprint = self.ctrl.fingerprint(self.ctrl.sessions['chat:a'])
        self.ctrl.handle('pause_capture', {})
        self.ctrl._on_analysis('chat:a', 0, fingerprint, {'candidates': []}, None)
        self.assertNotIn('chat:a', self.ctrl.results)

    def test_fill_rejects_wrong_session_and_stale_message(self):
        self.incoming()
        self.ctrl.results['chat:a'] = {'candidates': [{'text': '候选'}]}
        self.ctrl.result_fingerprints['chat:a'] = self.ctrl.fingerprint(self.ctrl.sessions['chat:a'])
        with self.assertRaises(ValueError):
            self.ctrl.handle('fill_reply', {'session_id': 'chat:b', 'index': 0})
        self.incoming(mid='2', text='新的消息')
        with self.assertRaises(ValueError):
            self.ctrl.handle('fill_reply', {'session_id': 'chat:a', 'index': 0})
        self.ctrl.capture.fill.assert_not_called()

    def test_manual_context_direction_and_not_fillable(self):
        result = self.ctrl.handle('manual_context', {'title': '模拟', 'text': '我：明天见\n对方：好的'})
        messages = self.ctrl.sessions[result['session_id']]['messages']
        self.assertEqual([m['side'] for m in messages], ['me', 'other'])
        self.assertFalse(self.ctrl.snapshot()['current_session']['active'])

    def test_snapshot_and_errors_redact_credentials(self):
        self.ctrl.store.secrets['api_key'] = 'synthetic-secret'
        self.assertNotIn('synthetic-secret', str(self.ctrl.snapshot()))
        self.assertNotIn('synthetic-secret', self.ctrl.safe_error(RuntimeError('Bad synthetic-secret')))

    def test_historical_scroll_does_not_trigger_or_become_latest(self):
        self.incoming()
        self.ctrl.store.secrets['api_key'] = 'dummy'
        self.ctrl._on_capture('messages', {'session_id': 'chat:a', 'title': '测试联系人', 'source': 'ocr',
            'historical': True, 'messages': [{'id': 'older', 'text': '旧消息', 'side': 'other'}]})
        self.assertEqual(self.ctrl.sessions['chat:a']['messages'][-1]['id'], '1')
        self.assertIsNone(self.ctrl.pending)

    def test_first_explicit_start_can_analyze_initial_context(self):
        self.ctrl.store.secrets['api_key'] = 'dummy'
        self.ctrl.handle('start_capture', {})
        self.ctrl._on_capture('messages', {'session_id': 'chat:a', 'title': '测试联系人', 'source': 'ocr',
            'historical': True, 'messages': [{'id': 'first', 'text': '上下文', 'side': 'other'}]})
        self.assertEqual(self.ctrl.pending, 'chat:a')
        self.assertFalse(self.ctrl.allow_initial)

    def test_late_capture_event_cannot_resume_user_pause(self):
        self.incoming()
        self.ctrl.handle('pause_capture', {})
        self.incoming(mid='late', text='暂停之前排队的消息')
        self.assertEqual(self.ctrl.status['capture'], 'paused')
        self.assertEqual(len(self.ctrl.sessions['chat:a']['messages']), 1)

    def test_own_reply_cancels_debounced_auto_job(self):
        self.ctrl.store.secrets['api_key'] = 'dummy'
        self.incoming()
        self.assertEqual(self.ctrl.pending, 'chat:a')
        self.incoming(mid='2', text='我已经回复了', side='me')
        self.assertIsNone(self.ctrl.pending)
        self.assertFalse(self.ctrl.timer.isActive())

    def test_stale_result_after_own_reply_does_not_auto_retry(self):
        self.incoming()
        fingerprint = self.ctrl.fingerprint(self.ctrl.sessions['chat:a'])
        self.incoming(mid='2', text='我已经回复了', side='me')
        self.ctrl._on_analysis('chat:a', 0, fingerprint, {'candidates': []}, None)
        self.assertIsNone(self.ctrl.pending)


if __name__ == '__main__':
    unittest.main()
