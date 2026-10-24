"""Synthetic Work Agent controller and persistence boundary checks."""
import sys
import json
import tempfile
import threading
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtWidgets import QApplication
from desk.controller import Controller
from desk.bridge import Bridge


app = QApplication.instance() or QApplication([])


class WorkAgentControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.capture_patch = patch('desk.capture_service.CaptureService')
        self.capture_patch.start()
        self.ctrl = Controller(Path(self.temp.name))
        self.ctrl.store.secrets['work_api_key'] = 'synthetic-test-key'
        self.sid = self.ctrl.handle('work_agent_create', {})['session']['id']

    def tearDown(self):
        self.ctrl.close()
        self.capture_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def done(value):
        future = Future()
        future.set_result(value)
        return future

    def test_two_turn_chat_uses_same_session_and_keeps_artifact_private(self):
        response = {'answer': '已整理。', 'artifacts': [{'name': 'summary.md',
            'content': '# 合成摘要', 'mime_type': 'text/markdown'}],
            'sources': [], 'trace': [{'tool': 'write_artifact', 'status': 'done'}],
            'mode': 'tools', 'usage': {}}
        with patch.object(self.ctrl.models, 'submit', return_value=self.done(response)) as submit:
            first = self.ctrl.handle('work_agent_turn', {'session_id': self.sid,
                'text': '请整理合成材料', 'attachment_ids': []}).result(timeout=10)
            second = self.ctrl.handle('work_agent_turn', {'session_id': self.sid,
                'text': '接着写下一步', 'attachment_ids': []}).result(timeout=10)
        self.assertEqual(first['session']['messages'][-1]['content'], '已整理。')
        self.assertEqual(len(second['session']['messages']), 4)
        self.assertEqual(submit.call_args.args[0], 'work_turn')
        self.assertEqual([m['role'] for m in submit.call_args.args[1]['messages']],
                         ['user', 'assistant', 'user'])
        self.assertNotIn('synthetic-test-key', str(second))
        self.assertNotIn('# 合成摘要', str(second['result']['artifacts']))
        self.ctrl.capture.send.assert_not_called()
        self.ctrl.capture.fill.assert_not_called()

    def test_selected_file_is_read_without_exposing_source_path_to_ui(self):
        source = Path(self.temp.name) / 'synthetic-notes.txt'
        source.write_text('仅供合成测试的项目计划', encoding='utf-8')
        item = self.ctrl.work_agent.attach_files(self.sid, [str(source)])[0]
        response = {'answer': '已看到计划。', 'artifacts': [], 'sources': [
            {'id': item['id'], 'name': item['name']}], 'trace': [], 'mode': 'direct'}
        with patch.object(self.ctrl.models, 'submit', return_value=self.done(response)) as submit:
            result = self.ctrl.handle('work_agent_turn', {'session_id': self.sid,
                'text': '分析材料', 'attachment_ids': [item['id']]}).result(timeout=10)
        self.assertEqual(submit.call_args.args[1]['attachments'][0]['id'], item['id'])
        self.assertNotIn(str(source), str(result))
        self.assertIn(item['id'], result['session']['messages'][0]['attachment_ids'])

    def test_work_key_only_reuses_main_key_for_same_origin(self):
        self.ctrl.store.secrets['work_api_key'] = ''
        self.ctrl.store.secrets['api_key'] = 'synthetic-main-key'
        same = self.ctrl._work_model_config()
        self.assertEqual(same['work_api_key'], 'synthetic-main-key')
        other = self.ctrl._work_model_config({'work_base_url': 'https://different.example/v1'})
        self.assertEqual(other['work_api_key'], '')

    def test_connection_test_sends_only_builtin_text(self):
        response = {'answer': '连接正常。', 'artifacts': [], 'sources': [], 'trace': [], 'mode': 'direct'}
        with patch.object(self.ctrl.models, 'submit', return_value=self.done(response)) as submit:
            result = self.ctrl.handle('work_agent_test', {}).result(timeout=10)
        self.assertTrue(result['success'])
        payload = submit.call_args.args[1]
        self.assertEqual(len(payload['messages']), 1)
        self.assertEqual(payload['attachments'], [])
        self.assertNotIn(self.sid, str(payload))

    def test_cancel_stops_uncommitted_turn_and_keeps_session_unchanged(self):
        started = threading.Event()
        pending = Future()
        def submit(kind, payload):
            self.assertEqual(kind, 'work_turn')
            started.set()
            return pending
        with patch.object(self.ctrl.models, 'submit', side_effect=submit):
            turn = self.ctrl.handle('work_agent_turn', {'session_id': self.sid,
                'text': '请处理合成任务', 'attachment_ids': []})
            self.assertTrue(started.wait(5))
            self.assertTrue(self.ctrl.handle('work_agent_cancel', {'session_id': self.sid})['success'])
            pending.set_result({'answer': '迟到的结果', 'artifacts': [], 'sources': [], 'trace': []})
            with self.assertRaisesRegex(ValueError, '已停止'):
                turn.result(timeout=10)
        self.assertEqual(self.ctrl.work_agent.get_session(self.sid)['messages'], [])
        self.assertFalse(self.ctrl.handle('work_agent_cancel', {'session_id': self.sid})['success'])

    def test_bridge_progress_event_contains_only_session_and_phase(self):
        bridge = Bridge(self.ctrl)
        events = []
        bridge.event.connect(lambda raw: events.append(json.loads(raw)))
        self.ctrl.workAgentProgress.emit(self.sid, 'reading')
        self.assertEqual(events[-1], {'type': 'work_agent_progress',
                                      'data': {'session_id': self.sid, 'phase': 'reading'}})

    def test_export_requires_matching_artifact_id(self):
        self.ctrl.work_agent.commit_turn(self.sid, '生成文件', [], {'answer': '完成',
            'artifacts': [{'name': 'safe.txt', 'content': 'synthetic artifact', 'mime_type': 'text/plain'}]})
        artifact = self.ctrl.work_agent.get_session(self.sid)['artifacts'][0]
        preview = self.ctrl.handle('work_agent_preview', {'session_id': self.sid,
            'artifact_id': artifact['id']})
        self.assertEqual(preview, {'name': 'safe.txt', 'text': 'synthetic artifact'})
        target = Path(self.temp.name) / 'exported.txt'
        with patch('desk.controller.QFileDialog.getSaveFileName', return_value=(str(target), '')):
            self.ctrl.handle('work_agent_export', {'session_id': self.sid,
                'artifact_id': artifact['id']}).result(timeout=10)
        self.assertEqual(target.read_text(encoding='utf-8'), 'synthetic artifact')
        with self.assertRaises(ValueError):
            self.ctrl.handle('work_agent_export', {'session_id': self.sid, 'artifact_id': 'wrong'})
        with self.assertRaises(ValueError):
            self.ctrl.handle('work_agent_preview', {'session_id': self.sid, 'artifact_id': 'wrong'})


if __name__ == '__main__':
    unittest.main()
