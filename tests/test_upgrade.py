import io
import json
import sys
import tempfile
import threading
import unittest
import wave
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from core.agent import run_agent
from core.media import transcribe_audio
from core.followups import extract_followups
from desk.actions import ActionStore
from desk.imports import ChatLabImporter
from desk.platforms import capabilities
from desk.store import Store
from PySide6.QtWidgets import QApplication
from desk.controller import Controller
app = QApplication.instance() or QApplication([])
CONFIG = {'base_url': 'https://openrouter.ai/api', 'model_name': 'synthetic-judge', 'api_key': 'synthetic-only'}


class AgentRegressionTests(unittest.TestCase):
    def test_unknown_chat_type_cannot_invite_group_style_reply(self):
        response = {'answers': {'danger_level': {'type': 'score', 'score': 2}, 'next_step': {'type': 'choice', 'choice': 'reply_now', 'confidence': .9}}}
        result = run_agent([{'id': 's', 'type': 'unknown', 'messages': [{'id': 'm', 'side': 'other', 'text': '报价如何？'}]}], CONFIG,
                           post_json_fn=lambda *a, **k: response, draft_fn=lambda *a, **k: self.fail('No draft for unknown chat type'))
        self.assertEqual(result['cards'][0]['action'], 'review')

    def test_unknown_latest_direction_does_not_analyze_older_turn(self):
        messages = [{'id': 'old', 'side': 'other', 'text': '何时见面？'},
                    {'id': 'new', 'side': 'unknown', 'text': '不用回复。'}]
        result = run_agent([{'id': 's', 'type': 'private', 'messages': messages}], CONFIG,
                           post_json_fn=lambda *a, **k: self.fail('provider must not run'))
        self.assertEqual(result['cards'][0]['action'], 'review')
        self.assertIn('方向', result['cards'][0]['reason'])

    def test_controller_preserves_nontext_kind_and_unavailable_freshness(self):
        with tempfile.TemporaryDirectory() as directory, patch('desk.capture_service.CaptureService'):
            ctrl = Controller(Path(directory))
            ctrl.store.secrets['api_key'] = 'synthetic-only'
            try:
                def submit(kind, payload):
                    future = Future()
                    future.set_result(run_agent(**payload, post_json_fn=lambda *a, **k: self.fail('No provider call')))
                    return future
                for kind, text in [('app', '[应用消息]'), ('unknown', '[无法解码的文字]'), ('card', '[名片]')]:
                    sid = 'wechat_db:' + kind
                    ctrl.catalog_items[sid] = {'id': sid, 'source': 'wechat_db', 'type': 'private'}
                    page = {'available': True, 'items': [{'id': 'm', 'side': 'other', 'kind': kind, 'text': text}]}
                    with patch.object(ctrl, '_session_page_task', return_value=page), patch.object(ctrl.models, 'submit', side_effect=submit):
                        result = ctrl.handle('run_agent', {'session_ids': [sid]}).result(timeout=5)
                    self.assertEqual(result['cards'][0]['action'], 'review')
                sid = 'weflow:cached'
                ctrl.catalog_items[sid] = {'id': sid, 'source': 'weflow', 'type': 'private'}
                page = {'available': False, 'items': [{'id': 'm', 'side': 'other', 'kind': 'text', 'text': '旧问题'}]}
                with patch.object(ctrl, '_session_page_task', return_value=page), patch.object(ctrl.models, 'submit', side_effect=submit):
                    result = ctrl.handle('run_agent', {'session_ids': [sid]}).result(timeout=5)
                self.assertIn('缓存', result['cards'][0]['reason'])
            finally:
                ctrl.close()


class ActionTests(unittest.TestCase):
    def test_evidence_identity_persistence_timezone_and_revision_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ActionStore(directory)
            session = {'id': 's1', 'title': '合成客户', 'messages': [{'id': 'm1', 'side': 'other', 'text': '请核对报价'}]}
            action = store.create({'title': '核对报价', 'message_ids': ['m1'], 'due_at': '2026-10-05T09:00:00+08:00'}, session)
            self.assertEqual(action['evidence'][0]['text'], '请核对报价')
            self.assertEqual(action['due_at'], '2026-10-05T01:00:00+00:00')
            self.assertEqual(ActionStore(directory).list()['items'][0]['id'], action['id'])
            done = store.transition(action['id'], 'done', 1)
            self.assertEqual(done['revision'], 2)
            self.assertEqual(store.list()['items'], [])
            with self.assertRaisesRegex(ValueError, '已被更新'):
                store.transition(action['id'], 'dismissed', 1)
            with self.assertRaisesRegex(ValueError, '证据'):
                store.create({'title': '假的证据', 'message_ids': ['another-session-id']}, session)
            with self.assertRaisesRegex(ValueError, '时区'):
                store.create({'title': '无时区', 'due_at': '2026-10-05T09:00:00'}, session)


class FollowupTests(unittest.TestCase):
    def test_only_exact_quotes_bound_to_input_ids_survive_and_dates_are_not_invented(self):
        messages = [{'id': 'm1', 'side': 'me', 'kind': 'text', 'text': '周四前核对报价后给你答复。'}]
        proposals = [
            {'title': '核对报价', 'detail': '核实再回复', 'evidence': [{'message_id': 'm1', 'quote': '周四前核对报价'}], 'deadline_quote': '周四前'},
            {'title': '虚构行动', 'evidence': [{'message_id': 'missing', 'quote': '周五发送合同'}]},
            {'title': '改写原文', 'evidence': [{'message_id': 'm1', 'quote': '明天发送合同'}]},
        ]
        response = {'choices': [{'message': {'content': json.dumps({'proposals': proposals})}}]}
        result = extract_followups(messages, CONFIG, post_json_fn=lambda *a, **k: response)
        self.assertEqual(len(result['proposals']), 1)
        self.assertEqual(result['rejected_count'], 2)
        self.assertEqual(result['proposals'][0]['deadline_quote'], '周四前')
        self.assertIsNone(result['proposals'][0]['due_at'])

    def test_no_raw_media_or_bad_json_can_create_action_proposals(self):
        with self.assertRaises(Exception):
            extract_followups([{'id': 'm', 'side': 'other', 'kind': 'image', 'text': '[图片]'}], CONFIG,
                              post_json_fn=lambda *a, **k: self.fail('No network request'))
        with self.assertRaisesRegex(Exception, '模型未返回'):
            extract_followups([{'id': 'm', 'side': 'other', 'text': '核对一下报价。'}], CONFIG,
                              post_json_fn=lambda *a, **k: {'choices': [{'message': {'content': 'not json'}}]})


class ArchiveSearchTests(unittest.TestCase):
    def test_search_reimport_updates_index_and_context_includes_old_hit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            export = root / 'synthetic.json'
            payload = {'chatlab': {'version': '0.0.2'}, 'meta': {'name': '合成客户', 'type': 'private', 'ownerId': 'me'},
                       'members': [{'platformId': 'me'}, {'platformId': 'customer'}],
                       'messages': [{'platformMessageId': str(i), 'sender': 'customer', 'timestamp': 1700000000+i, 'type': 0, 'content': '报价待确认' if i == 2 else '合成消息'} for i in range(100)]}
            export.write_text(json.dumps(payload), encoding='utf-8')
            archive = ChatLabImporter(root / 'data')
            archive.import_files([str(export)])
            hits = archive.search('报价待')
            self.assertEqual(len(hits['items']), 1)
            hit = hits['items'][0]
            page = archive.context(hit['session_id'], hit['id'])
            self.assertIn(hit['id'], [m['id'] for m in page['items']])
            payload['messages'][2]['content'] = '合同已确认'
            export.write_text(json.dumps(payload), encoding='utf-8')
            archive.import_files([str(export)])
            self.assertEqual(archive.search('报价待')['items'], [])
            self.assertEqual(len(archive.search('合同已')['items']), 1)
            self.assertEqual(archive.search('" OR "')['items'], [])


class PlatformTests(unittest.TestCase):
    def test_mac_unsupported_capture_rejected_before_thread_and_folder_uses_desktop_url(self):
        from desk.capture_service import CaptureService
        with patch('desk.capture_service.sys.platform', 'darwin'):
            capture = CaptureService(lambda *args: None)
            with self.assertRaisesRegex(RuntimeError, 'macOS'):
                capture.one_shot({'source': 'ocr'})
            with self.assertRaisesRegex(RuntimeError, '归档'):
                capture.one_shot({'source': 'archive'})
            self.assertIsNone(capture._thread)
        with tempfile.TemporaryDirectory() as directory, patch('desk.capture_service.CaptureService'):
            ctrl = Controller(Path(directory))
            try:
                with patch('desk.controller.QDesktopServices.openUrl', return_value=True) as opening:
                    self.assertTrue(ctrl.handle('open_data_folder', {})['success'])
                self.assertEqual(opening.call_args.args[0].toLocalFile(), str(Path(directory).resolve()))
            finally:
                ctrl.close()

    def test_invalid_setting_types_and_migrated_windows_source_recover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config.json').write_text(json.dumps({'font_scale': [], 'background_dim': 999, 'context_limit': 'bad', 'source': 'ocr'}))
            (root / 'knowledge.json').write_text(json.dumps({'notes': None, 'contacts': [None, 'bad']}))
            with patch('desk.store.sys.platform', 'darwin'):
                store = Store(root)
            self.assertEqual(store.config['source'], 'archive')
            self.assertEqual(store.config['font_scale'], 1.0)
            self.assertEqual(store.config['context_limit'], 30)
            self.assertEqual(store.notes, [])
            self.assertEqual(store.contacts, [])

    def test_mac_credentials_roundtrip_uses_keychain_and_never_secret_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secure = {}
            with patch('desk.store.sys.platform', 'darwin'), patch('desk.platforms.keychain_set', side_effect=lambda d,v: secure.update(value=v)), patch('desk.platforms.keychain_get', side_effect=lambda d: secure.get('value', '{}')):
                store = Store(root)
                store.save_config({'api_key': 'synthetic-test-key'})
                self.assertNotIn('synthetic-test-key', (root / 'credentials.json').read_text())
                self.assertEqual(Store(root).secrets['api_key'], 'synthetic-test-key')
                self.assertFalse(capabilities('darwin')['send'])
                with patch('desk.platforms.keychain_set', side_effect=RuntimeError('denied')):
                    with self.assertRaisesRegex(RuntimeError, '钥匙串'):
                        store.save_config({'theme': 'paper'})
                self.assertEqual(store.config['theme'], 'aurora')

    def test_malformed_configuration_recovers_without_startup_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config.json').write_text('[]')
            (root / 'credentials.json').write_text('[]')
            store = Store(root)
            self.assertFalse(store.public_config()['has_api_key'])
            self.assertTrue(store.error)


class MultipartTests(unittest.TestCase):
    def test_actual_multipart_audio_body_and_no_cross_origin_key(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((dict(self.headers), self.rfile.read(int(self.headers['Content-Length']))))
                data = json.dumps({'text': '合成语音转写'}).encode()
                self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        output = io.BytesIO()
        with wave.open(output, 'wb') as stream:
            stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(16000); stream.writeframes(b'\0\0' * 16000)
        try:
            cfg = {**CONFIG, 'stt_backend': 'cloud', 'stt_protocol': 'multipart', 'stt_model': 'synthetic-stt',
                   'stt_base_url': f'http://127.0.0.1:{server.server_port}/v1'}
            result = transcribe_audio(output.getvalue(), 'audio/wav', cfg)
            self.assertEqual(result['text'], '合成语音转写')
            headers, body = requests[0]
            self.assertNotIn('Authorization', headers)
            self.assertIn('multipart/form-data', headers['Content-Type'])
            self.assertIn(b'filename="audio.wav"', body)
            self.assertIn(output.getvalue(), body)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
