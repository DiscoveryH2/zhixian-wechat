import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == '__main__':
    unittest.main()
