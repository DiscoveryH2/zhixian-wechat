import json
import io
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from desk.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_credentials_roundtrip_never_plaintext_or_in_public_snapshot(self):
        sample = 'synthetic-unit-test-credential-349237'
        self.store.save_config({'api_key': sample})
        for file in Path(self.temp.name).glob('*'):
            self.assertNotIn(sample, file.read_text())
        self.assertNotIn('api_key', self.store.public_config())
        self.assertTrue(self.store.public_config()['has_api_key'])
        self.assertEqual(Store(Path(self.temp.name)).secrets['api_key'], sample)
        self.store.save_config({'api_key': ''})
        self.assertEqual(self.store.secrets['api_key'], sample)
        self.store.save_config({'clear_api_key': True})
        self.assertFalse(self.store.public_config()['has_api_key'])

    def test_history_disabled_by_default(self):
        self.store.save_history([{'id': 'sample'}])
        self.assertFalse((Path(self.temp.name) / 'history.json').exists())
        self.assertEqual(self.store.load_history(), [])

    def test_context_matches_alias_and_relevant_notes(self):
        self.store.save_contact({'name': '项目讨论组', 'aliases': ['开发小组'], 'relationship': '同事', 'notes': '演示备注'})
        self.store.save_note({'title': '发布安排', 'content': '周四交付', 'tags': ['发布']})
        self.store.save_note({'title': '偏好', 'content': '回复简短', 'always': True})
        self.store.save_note({'title': '无关内容', 'content': '不应进入上下文'})
        context, contact = self.store.background('开发小组 (12)', [{'text': '发布安排确认了吗'}])
        self.assertEqual(contact['relationship'], '同事')
        self.assertIn('周四交付', context)
        self.assertIn('回复简短', context)
        self.assertNotIn('不应进入上下文', context)

    def test_contact_matching_is_exact_not_fuzzy(self):
        self.store.save_contact({'name': '小明', 'notes': 'private'})
        context, contact = self.store.background('小明同事', [])
        self.assertIsNone(contact)
        self.assertEqual(context, '')

    def test_note_update_keeps_identity_and_delete_persists(self):
        item = self.store.save_note({'title': 'A', 'content': 'B'})
        self.store.save_note({**item, 'content': 'C'})
        self.assertEqual(len(self.store.notes), 1)
        self.store.delete('notes', item['id'])
        self.assertEqual(Store(Path(self.temp.name)).notes, [])

    def test_url_credentials_are_rejected_before_persistence(self):
        for url in ('https://example.com/api?key=sample', 'https://user:password@example.com/api'):
            with self.assertRaises(Exception):
                self.store.save_config({'base_url': url})
        self.assertFalse((Path(self.temp.name) / 'config.json').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_media_credentials_dpapi_roundtrip_keep_and_independent_clear(self):
        values = {name: 'synthetic-media-' + name + '-never-issued'
                  for name in ('vision_api_key', 'stt_api_key')}
        public = self.store.save_config(values)
        for name, value in values.items():
            self.assertNotIn(name, public)
            self.assertTrue(public['has_' + name])
            self.assertEqual(Store(Path(self.temp.name)).secrets[name], value)
            for path in Path(self.temp.name).glob('*'):
                self.assertNotIn(value.encode(), path.read_bytes())
        self.store.save_config({'vision_api_key': '', 'stt_api_key': ''})
        self.assertEqual(self.store.secrets['vision_api_key'], values['vision_api_key'])
        self.assertEqual(self.store.secrets['stt_api_key'], values['stt_api_key'])
        self.store.save_config({'clear_vision_api_key': True})
        restored = Store(Path(self.temp.name))
        self.assertEqual(restored.secrets['vision_api_key'], '')
        self.assertEqual(restored.secrets['stt_api_key'], values['stt_api_key'])
        self.assertFalse(restored.public_config()['has_vision_api_key'])
        self.assertTrue(restored.public_config()['has_stt_api_key'])

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_media_settings_never_log_credentials_or_return_them(self):
        values = {name: 'synthetic-no-log-' + name + '-never-issued'
                  for name in ('vision_api_key', 'stt_api_key')}
        stdout, stderr, logs = io.StringIO(), io.StringIO(), io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = self.store.save_config({**values, 'vision_base_url': 'https://vision.example.invalid/v1',
                                                 'stt_base_url': 'https://speech.example.invalid/v1'})
                restored = Store(Path(self.temp.name))
            exposed = stdout.getvalue() + stderr.getvalue() + logs.getvalue() + json.dumps(result) + restored.error
            for value in values.values():
                self.assertNotIn(value, exposed)
        finally:
            logging.getLogger().removeHandler(handler)

    def test_media_credential_urls_rejected_without_creating_state(self):
        for field in ('vision_base_url', 'stt_base_url'):
            for bad in ('https://user:' + 'synthetic-password@media.example.invalid/v1',
                        'https://media.example.invalid/v1?' + 'token=synthetic-query-credential'):
                with self.subTest(field=field), self.assertRaises(Exception) as error:
                    self.store.save_config({field: bad})
                self.assertNotIn('synthetic-password', str(error.exception))
                self.assertNotIn('synthetic-query-credential', str(error.exception))
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])

    def test_dpapi_failure_never_falls_back_to_plaintext_media_keys(self):
        sample = 'synthetic-encryption-failure-never-issued'
        with patch('desk.store._crypt', side_effect=RuntimeError('Synthetic encryption failure')):
            with self.assertRaises(RuntimeError):
                self.store.save_config({'vision_api_key': sample, 'stt_api_key': sample})
        self.assertEqual(list(Path(self.temp.name).iterdir()), [])
        self.assertFalse(self.store.public_config()['has_vision_api_key'])
        self.assertFalse(self.store.public_config()['has_stt_api_key'])


if __name__ == '__main__':
    unittest.main()
