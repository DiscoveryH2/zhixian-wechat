"""Synthetic controller boundary checks for the read-only Agent mission."""
import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtWidgets import QApplication
from desk.controller import Controller


app = QApplication.instance() or QApplication([])


class AgentControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.capture_patch = patch('desk.capture_service.CaptureService')
        self.capture_patch.start()
        self.ctrl = Controller(Path(self.temp.name))
        self.ctrl.store.secrets['api_key'] = 'synthetic-secret'

    def tearDown(self):
        self.ctrl.close()
        self.capture_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def completed(value):
        future = Future()
        future.set_result(value)
        return future

    def test_explicit_selection_reads_only_selected_context_and_sends_minimal_config(self):
        sid = self.ctrl.handle('manual_context', {'title': '合成甲',
            'text': '我：明天见\n对方：上午方便吗？'})['session_id']
        other = self.ctrl.handle('manual_context', {'title': '合成乙',
            'text': '对方：这条不能进入任务'})['session_id']
        self.ctrl.store.secrets['weflow_token'] = 'synthetic-weflow-secret'
        expected = {'status': 'done', 'cards': [], 'trace': []}
        with patch.object(self.ctrl.models, 'submit', return_value=self.completed(expected)) as submit:
            result = self.ctrl.handle('run_agent', {'session_ids': [sid]}).result(timeout=10)
        self.assertEqual(result, expected)
        args, _ = submit.call_args
        self.assertEqual(args[0], 'run_agent')
        payload = args[1]
        self.assertEqual([item['id'] for item in payload['sessions']], [sid])
        self.assertEqual(len(payload['sessions'][0]['messages']), 2)
        self.assertNotIn(other, str(payload['sessions']))
        self.assertNotIn('weflow_token', payload['config'])
        self.assertNotIn('synthetic-weflow-secret', str(payload))

    def test_unknown_duplicate_and_overwide_selection_are_rejected(self):
        sid = self.ctrl.handle('manual_context', {'title': '合成', 'text': '对方：你好'})['session_id']
        for ids in ([], [sid, sid], [sid, 'unknown'], [sid] * 4, 'bad'):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.ctrl.handle('run_agent', {'session_ids': ids})

    def test_agent_never_uses_send_path(self):
        sid = self.ctrl.handle('manual_context', {'title': '合成', 'text': '对方：你好'})['session_id']
        with patch.object(self.ctrl.models, 'submit', return_value=self.completed({'status': 'done', 'cards': [], 'trace': []})):
            self.ctrl.handle('run_agent', {'session_ids': [sid]}).result(timeout=10)
        self.ctrl.capture.send.assert_not_called()
        self.ctrl.capture.fill.assert_not_called()

    def test_fresh_database_page_is_required_for_database_selection(self):
        sid = 'wechat_db:synthetic'
        self.ctrl.catalog_items[sid] = {'id': sid, 'title': '合成数据库会话',
                                        'source': 'wechat_db', 'type': 'private'}
        page = {'available': True, 'items': [
            {'id': 'fresh', 'side': 'other', 'text': '最新问题', 'kind': 'text'}]}
        with patch.object(self.ctrl, '_session_page_task', return_value=page) as read, \
             patch.object(self.ctrl.models, 'submit', return_value=self.completed({'status': 'done', 'cards': [], 'trace': []})) as submit:
            self.ctrl.handle('run_agent', {'session_ids': [sid]}).result(timeout=10)
        read.assert_called_once()
        self.assertEqual(submit.call_args.args[1]['sessions'][0]['messages'][0]['id'], 'fresh')

    def test_fresh_page_keeps_user_requested_media_transcript_by_message_id(self):
        sid = 'wechat_db:synthetic-media'
        self.ctrl.sessions[sid] = {'id': sid, 'title': '合成数据库会话', 'source': 'wechat_db',
                                   'type': 'private', 'messages': [
                                       {'id': 'v1', 'side': 'other', 'kind': 'voice',
                                        'text': '[语音转写] 下午三点可以吗？', 'transcript': '下午三点可以吗？'}]}
        page = {'available': True, 'items': [
            {'id': 'v1', 'side': 'other', 'kind': 'voice', 'text': '[语音]'}]}
        with patch.object(self.ctrl, '_session_page_task', return_value=page), \
             patch.object(self.ctrl.models, 'submit', return_value=self.completed({'status': 'done', 'cards': [], 'trace': []})) as submit:
            self.ctrl.handle('run_agent', {'session_ids': [sid]}).result(timeout=10)
        sent = submit.call_args.args[1]['sessions'][0]['messages'][0]
        self.assertEqual(sent['text'], '[语音转写] 下午三点可以吗？')
        self.assertEqual(sent['kind'], 'voice')


if __name__ == '__main__':
    unittest.main()
