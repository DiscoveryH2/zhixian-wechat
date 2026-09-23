import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from desk.imports import ChatLabImporter


def chatlab(name='合成群', group_id='group-001', count=3):
    return {'chatlab': {'version': '0.0.2'}, 'meta': {'name': name, 'platform': 'wechat',
            'type': 'group', 'groupId': group_id, 'ownerId': 'wxid-self'},
            'members': [{'platformId': 'wxid-self'}, {'platformId': 'wxid-friend'}],
            'messages': [{'sender': 'wxid-friend' if i % 2 else 'wxid-self', 'accountName': '合成朋友',
                          'timestamp': 10000 + i, 'type': 0, 'content': f'合成文字 {i}',
                          'platformMessageId': f'message-{i}'} for i in range(count)]}


class ChatLabImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.index = ChatLabImporter(self.root / 'app-data')

    def tearDown(self):
        self.temp.cleanup()

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        return str(path)

    def test_imports_paged_history_with_identity_and_reimport_is_idempotent(self):
        source = self.write_json('chatlab.json', chatlab(count=111))
        result = self.index.import_files([source])
        self.assertEqual(result['messages'], 111)
        catalog = self.index.list_sessions('合成', limit=1)
        self.assertEqual(catalog['total'], 1)
        sid = catalog['items'][0]['id']
        recent = self.index.messages(sid, limit=20)
        self.assertTrue(recent['has_more'])
        self.assertEqual(len(recent['items']), 20)
        self.assertEqual(recent['items'][-1]['text'], '合成文字 110')
        self.assertEqual(recent['items'][-1]['side'], 'me')
        older = self.index.messages(sid, cursor=recent['next_cursor'], limit=20)
        self.assertEqual(len(older['items']), 20)
        self.index.import_files([source])
        self.assertEqual(self.index.get_session(sid)['count'], 111)

    def test_streaming_jsonl_and_same_names_remain_distinct(self):
        a = chatlab(name='同名群', group_id='g1', count=2)
        b = chatlab(name='同名群', group_id='g2', count=1)
        first = self.write_json('one.json', a)
        rows = [json.dumps({'_type': 'header', 'chatlab': b['chatlab'], 'meta': b['meta']}, ensure_ascii=False)]
        rows += [json.dumps({'_type': 'member', **m}, ensure_ascii=False) for m in b['members']]
        rows += [json.dumps({'_type': 'message', **m}, ensure_ascii=False) for m in b['messages']]
        second = self.root / 'two.jsonl'
        second.write_text('\n'.join(rows), encoding='utf-8')
        self.index.import_files([first, str(second)])
        page = self.index.list_sessions(query='同名', limit=1)
        self.assertEqual(len(page['items']), 1)
        self.assertTrue(page['has_more'])
        other = self.index.list_sessions(query='同名', cursor=page['next_cursor'])
        self.assertNotEqual(page['items'][0]['id'], other['items'][0]['id'])

    def test_media_kind_and_path_boundary(self):
        media = self.root / 'media'
        media.mkdir()
        (media / 'photo.png').write_bytes(b'not-a-real-image')
        payload = chatlab(count=0)
        payload['messages'] = [
            {'sender': 'wxid-friend', 'timestamp': 10000, 'type': 1, 'content': '[图片] media/photo.png'},
            {'sender': 'wxid-friend', 'timestamp': 10001, 'type': 2, 'content': '[语音消息] ../outside.wav'},
        ]
        self.index.import_files([self.write_json('media.json', payload)])
        sid = self.index.list_sessions()['items'][0]['id']
        messages = self.index.messages(sid)['items']
        self.assertEqual([m['kind'] for m in messages], ['image', 'voice'])
        self.assertEqual(len(messages[0]['media']), 1)
        self.assertEqual(messages[1]['media'], [])
        self.assertEqual(self.index.resolve_media(sid, messages[0]['id'])['status'], 'ok')
        self.assertEqual(self.index.resolve_media(sid, messages[1]['id'])['status'], 'unavailable')

    def test_rejects_unrelated_file_and_no_partial_rows(self):
        bad = self.write_json('unrelated.json', {'messages': [{'content': 'not chatlab'}]})
        with self.assertRaises(ValueError):
            self.index.import_files([bad])
        self.assertEqual(self.index.count(), 0)


if __name__ == '__main__':
    unittest.main()
