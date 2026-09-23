"""Media and social-post controller regressions using synthetic files and models."""
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from unittest.mock import patch
import wave

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtWidgets import QApplication
from desk.controller import Controller

APP = QApplication.instance() or QApplication([])


def completed(value):
    future = Future()
    future.set_result(value)
    return future


class ControllerMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.native = patch('desk.capture_service.CaptureService')
        self.native.start()
        self.addCleanup(self.native.stop)
        self.model_class = patch('desk.controller.ModelTasks')
        self.model_class.start()
        self.addCleanup(self.model_class.stop)
        self.ctrl = Controller(self.root / 'data')
        self.sid = self.ctrl.handle('manual_context', {'title': 'Synthetic media chat',
                         'text': '我：合成的前文\n对方：请看这份合成内容'})['session_id']
        self.ctrl.store.secrets['api_key'] = 'synthetic-only'
        self.ctrl.store.config['auto_analyze'] = False

    def tearDown(self):
        self.ctrl.close()
        APP.processEvents()
        self.temp.cleanup()

    def drain(self, predicate, timeout=4):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            APP.processEvents()
            if predicate():
                return
            time.sleep(.005)
        self.fail('Synthetic asynchronous controller result did not arrive')

    def image_file(self):
        path = self.root / 'synthetic.png'
        Image.new('RGB', (12, 10), 'green').save(path)
        return path

    def audio_file(self):
        path = self.root / 'synthetic.wav'
        with wave.open(str(path), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b'\x00\x00' * 800)
        return path

    def test_choose_image_updates_only_target_message_without_exposing_bytes_or_keys(self):
        path = self.image_file()
        self.ctrl.store.secrets['vision_api_key'] = 'synthetic-image-credential-never-issued'
        self.ctrl.models.submit.return_value = completed({'success': True, 'status': 'ok', 'kind': 'image',
            'text': '一张合成绿色图片', 'model': 'synthetic-vision', 'warning': 'Synthetic model result'})
        with patch('desk.controller.QFileDialog.getOpenFileName', return_value=(str(path), '')):
            result = self.ctrl.handle('choose_media', {'session_id': self.sid, 'kind': 'image'}).result(timeout=5)
        self.drain(lambda: self.ctrl.sessions[self.sid]['messages'][-1].get('image_description'))
        message = self.ctrl.sessions[self.sid]['messages'][-1]
        self.assertEqual(message['text'], '[图片识别] 一张合成绿色图片')
        self.assertEqual(result['description'], '一张合成绿色图片')
        kind, payload = self.ctrl.models.submit.call_args.args
        self.assertEqual(kind, 'analyze_image')
        self.assertEqual(payload['data'], path.read_bytes())
        self.assertEqual(payload['source'], 'user_selected')
        self.assertTrue(payload['config']['media_allow_cloud'])
        self.assertFalse(self.ctrl.store.config['media_allow_cloud'])
        snapshot = json.dumps(self.ctrl.snapshot(), ensure_ascii=False)
        self.assertNotIn(self.ctrl.store.secrets['vision_api_key'], snapshot)
        self.assertNotIn(str(path), snapshot)
        self.assertNotIn('data:image/', snapshot)
        self.ctrl.capture.fill.assert_not_called()

    def test_choose_voice_adapts_audio_result_to_voice_message_and_transcript(self):
        path = self.audio_file()
        self.ctrl.models.submit.return_value = completed({'success': True, 'status': 'ok', 'kind': 'audio',
            'text': '合成语音：周五再确认', 'model': 'synthetic-stt', 'warning': 'Check synthetic transcript'})
        with patch('desk.controller.QFileDialog.getOpenFileName', return_value=(str(path), '')):
            result = self.ctrl.handle('choose_media', {'session_id': self.sid, 'kind': 'voice'}).result(timeout=5)
        self.drain(lambda: self.ctrl.sessions[self.sid]['messages'][-1].get('transcript'))
        self.assertEqual(self.ctrl.models.submit.call_args.args[0], 'transcribe_audio')
        self.assertEqual(result['kind'], 'voice')
        self.assertEqual(result['transcript'], '合成语音：周五再确认')
        self.assertEqual(self.ctrl.sessions[self.sid]['messages'][-1]['text'], '[语音转写] 合成语音：周五再确认')

    def test_cancelled_picker_has_no_model_call_or_placeholder(self):
        before = len(self.ctrl.sessions[self.sid]['messages'])
        with patch('desk.controller.QFileDialog.getOpenFileName', return_value=('', '')):
            result = self.ctrl.handle('choose_media', {'session_id': self.sid, 'kind': 'image'})
        self.assertTrue(result['cancelled'])
        self.assertEqual(len(self.ctrl.sessions[self.sid]['messages']), before)
        self.assertEqual(self.ctrl.media_inputs, {})
        self.ctrl.models.submit.assert_not_called()

    def test_unsupported_and_empty_media_never_invent_transcript(self):
        path = self.audio_file()
        results = ({'success': False, 'status': 'unsupported', 'kind': 'audio', 'text': '', 'warning': 'Unsupported synthetic format'},
                   {'success': True, 'status': 'ok', 'kind': 'audio', 'text': '', 'warning': 'No speech found'})
        for value in results:
            self.ctrl.models.submit.return_value = completed(value)
            with patch('desk.controller.QFileDialog.getOpenFileName', return_value=(str(path), '')):
                result = self.ctrl.handle('choose_media', {'session_id': self.sid, 'kind': 'voice'}).result(timeout=5)
            APP.processEvents()
            message = self.ctrl.sessions[self.sid]['messages'][-1]
            self.assertFalse(result['success'])
            self.assertNotIn('transcript', message)
            self.assertEqual(message['text'], '[语音]')

    def test_preview_is_local_and_does_not_start_a_model(self):
        raw = self.image_file().read_bytes()
        self.ctrl.media_inputs['synthetic-media'] = (raw, 'image/png', 'image')
        result = self.ctrl.handle('load_media', {'session_id': self.sid, 'message_id': 'synthetic-media'}).result(timeout=5)
        self.assertTrue(result['available'])
        self.assertTrue(result['data_url'].startswith('data:image/jpeg;base64,'))
        self.ctrl.models.submit.assert_not_called()

    def test_media_completion_after_history_cleared_cannot_recreate_session(self):
        pending = Future()
        self.ctrl.models.submit.return_value = pending
        path = self.image_file()
        with patch('desk.controller.QFileDialog.getOpenFileName', return_value=(str(path), '')):
            future = self.ctrl.handle('choose_media', {'session_id': self.sid, 'kind': 'image'})
        self.drain(lambda: self.ctrl.models.submit.called)
        self.ctrl.handle('clear_history', {})
        pending.set_result({'success': True, 'status': 'ok', 'kind': 'image', 'text': 'Late synthetic description'})
        future.result(timeout=5)
        APP.processEvents()
        self.assertEqual(self.ctrl.sessions, {})
        self.assertEqual(self.ctrl.media_inputs, {})

    def test_media_keys_absent_from_snapshot_and_safe_error_redacts_every_key(self):
        for name in ('api_key', 'reply_api_key', 'weflow_token', 'vision_api_key', 'stt_api_key'):
            self.ctrl.store.secrets[name] = 'synthetic-controller-' + name + '-never-issued'
        public = json.dumps(self.ctrl.snapshot())
        safe = self.ctrl.safe_error(RuntimeError(' '.join(self.ctrl.store.secrets.values())))
        for name, value in self.ctrl.store.secrets.items():
            self.assertNotIn(value, public)
            self.assertNotIn(value, safe)
            self.assertNotIn(name, self.ctrl.snapshot()['config'])

    def test_redaction_precedes_error_length_limit(self):
        sample = 'synthetic-fragment-protection-' + 'A' * 160
        self.ctrl.store.secrets['vision_api_key'] = sample
        text = self.ctrl.safe_error(RuntimeError('x' * 1140 + sample))
        self.assertNotIn(sample[:40], text)
        self.assertLessEqual(len(text), 1200)

    def test_manual_moment_uses_linked_history_and_contact_relation_but_never_publishes(self):
        self.ctrl.store.save_contact({'name': 'Synthetic friend', 'relationship': '普通同事'})
        post = self.ctrl.handle('manual_moment', {'session_id': self.sid, 'author': 'Synthetic friend',
                              'text': 'Synthetic project milestone'})['item']
        self.ctrl.models.submit.return_value = completed({'like_recommendation': 'wait', 'comment_label': '先核实语境',
            'topic': '合成动态', 'candidates': [], 'automatic_actions': False, 'warning': 'Only supplied context'})
        result = self.ctrl.handle('analyze_moment', {'moment_id': post['id']}).result(timeout=5)
        kind, payload = self.ctrl.models.submit.call_args.args
        self.assertEqual(kind, 'analyze_moment')
        self.assertEqual(payload['relationship'], '普通同事')
        self.assertEqual(len(payload['history']), 2)
        self.assertEqual(payload['post']['id'], post['id'])
        self.assertIsNone(result['like'])
        self.assertEqual(result['comments'], [])
        self.assertFalse(result['automatic_actions'])
        self.ctrl.capture.fill.assert_not_called()

    def test_expired_moment_does_not_invoke_any_model(self):
        with self.assertRaises(ValueError):
            self.ctrl.handle('analyze_moment', {'moment_id': 'unknown-synthetic-post'})
        self.ctrl.models.submit.assert_not_called()

    def test_media_task_error_signal_does_not_expose_new_secret_fields(self):
        sample = 'synthetic-media-error-never-issued'
        self.ctrl.store.secrets['stt_api_key'] = sample
        errors = []
        self.ctrl.mediaFinished.connect(lambda sid, mid, result, error: errors.append(error))
        failure = Future()
        failure.set_exception(RuntimeError('Rejected ' + sample))
        self.ctrl._emit_media_done(self.sid, 'synthetic-id', failure)
        self.assertTrue(errors)
        self.assertNotIn(sample, errors[0])


if __name__ == '__main__':
    unittest.main()
