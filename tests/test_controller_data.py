"""Qt controller contracts for indexed archives and public social advice."""
import json
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtWidgets import QApplication
from desk.controller import Controller


APP = QApplication.instance() or QApplication([])


class IndexedControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.native = patch('desk.capture_service.CaptureService')
        self.native.start()
        self.ctrl = Controller(self.root / 'data')
        self.ctrl.store.config['source'] = 'ocr'  # This suite exercises the legacy collected/OCR catalog.
        archive = {
            'chatlab': {'version': '0.0.2'},
            'meta': {'name': 'Synthetic project group', 'platform': 'wechat', 'type': 'group',
                     'groupId': 'synthetic-group-1', 'ownerId': 'synthetic-self'},
            'members': [{'platformId': 'synthetic-self'}, {'platformId': 'synthetic-friend'}],
            'messages': [{'sender': 'synthetic-friend' if i % 2 else 'synthetic-self',
                          'timestamp': 100000+i, 'type': 0, 'content': f'Synthetic message {i}',
                          'platformMessageId': f'synthetic-message-{i}'} for i in range(80)],
        }
        file = self.root / 'archive.json'
        file.write_text(json.dumps(archive), encoding='utf-8')
        self.ctrl.imports.import_files([str(file)])

    def tearDown(self):
        self.ctrl.close()
        self.native.stop()
        self.temp.cleanup()

    def drain(self, predicate, timeout=2):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            APP.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail('Queued Qt state update did not arrive')

    def test_global_modal_catalog_import_and_history_page(self):
        self.ctrl._on_capture('messages', {'session_id': 'ocr:synthetic', 'title': 'Visible chat', 'source': 'ocr',
            'messages': [{'id': 'ocrmsg:1', 'side': 'other', 'sender': 'S', 'text': 'Visible content', 'timestamp': None}]})
        first = self.ctrl.handle('list_sessions', {'source': 'all', 'limit': 1}).result(timeout=5)
        self.assertEqual(len(first['items']), 1)
        self.assertTrue(first['has_more'])
        second = self.ctrl.handle('list_sessions', {'source': 'all', 'limit': 1,
                                                    'cursor': first['next_cursor']}).result(timeout=5)
        self.assertEqual({first['items'][0]['source'], second['items'][0]['source']}, {'ocr', 'import'})
        archive = self.ctrl.handle('list_sessions', {'source': 'import', 'query': 'project'}).result(timeout=5)
        sid = archive['items'][0]['id']
        self.assertTrue(archive['items'][0]['is_group'])
        self.ctrl.handle('select_session', {'session_id': sid}).result(timeout=5)
        self.drain(lambda: self.ctrl.current_id == sid)
        self.assertEqual(len(self.ctrl.snapshot()['current_session']['messages']), 50)
        self.assertTrue(self.ctrl.snapshot()['current_session']['has_more'])
        older = self.ctrl.handle('load_session_messages', {'session_id': sid, 'cursor': '50', 'limit': 20}).result(timeout=5)
        self.drain(lambda: len(self.ctrl.sessions[sid]['messages']) == 70)
        self.assertEqual(len(older['items']), 20)
        self.assertEqual(self.ctrl.sessions[sid]['messages'][-1]['text'], 'Synthetic message 79')

    def test_selected_archive_does_not_jump_to_visible_chat(self):
        sid = self.ctrl.handle('list_sessions', {'source': 'import'}).result(timeout=5)['items'][0]['id']
        self.ctrl.handle('select_session', {'session_id': sid}).result(timeout=5)
        self.drain(lambda: self.ctrl.current_id == sid)
        self.ctrl._on_capture('session', {'session_id': 'ocr:new', 'title': 'Another visible chat', 'source': 'ocr'})
        self.assertEqual(self.ctrl.current_id, sid)
        self.assertEqual(self.ctrl.live_id, 'ocr:new')

    def test_manual_moment_and_model_result_match_ui_contract(self):
        item = self.ctrl.handle('manual_moment', {'author': 'Synthetic friend', 'text': 'Project launched'})['item']
        page = self.ctrl.handle('list_moments', {}).result(timeout=5)
        self.assertTrue(page['available'])
        self.assertEqual(page['items'][0]['id'], item['id'])
        self.ctrl.store.secrets['api_key'] = 'synthetic-only'
        model = Future()
        model.set_result({'like_recommendation': 'skip', 'comment_label': '不必公开评论',
                          'candidates': [{'text': '好消息', 'score': None}], 'topic': '项目动态',
                          'automatic_actions': False, 'warning': '基于已提供内容'})
        with patch.object(self.ctrl.models, 'submit', return_value=model):
            advice = self.ctrl.handle('analyze_moment', {'moment_id': item['id']}).result(timeout=5)
        self.assertFalse(advice['like'])
        self.assertEqual(advice['comments'], ['好消息'])
        self.assertFalse(advice['automatic_actions'])

    def test_media_without_asset_is_explicitly_unavailable(self):
        self.ctrl._on_capture('messages', {'session_id': 'ocr:text-only', 'title': 'Synthetic', 'source': 'ocr',
            'messages': [{'id': 'ocrmsg:text', 'side': 'other', 'sender': 'S', 'text': '[图片]', 'kind': 'image'}]})
        result = self.ctrl.handle('load_media', {'session_id': 'ocr:text-only', 'message_id': 'ocrmsg:text'}).result(timeout=5)
        self.assertFalse(result['available'])
        self.assertIn('媒体', result['message'])


if __name__ == '__main__':
    unittest.main()
