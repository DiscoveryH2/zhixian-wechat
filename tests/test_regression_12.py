"""Synthetic catalog pagination and large streaming-import regressions."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtWidgets import QApplication
from desk.controller import Controller
from desk.imports import ChatLabImporter, MAX_LINE_BYTES

APP = QApplication.instance() or QApplication([])


def write_jsonl(path, count, *, private=False, peer='synthetic-peer', include_member=True, suffix=''):
    meta = {'name': 'Synthetic same-name chat' if private else 'Synthetic bulk group', 'platform': 'wechat',
            'type': 'private' if private else 'group', 'ownerId': 'synthetic-self'}
    if not private:
        meta['groupId'] = 'synthetic-bulk-group' + suffix
    with path.open('w', encoding='utf-8') as stream:
        stream.write(json.dumps({'_type': 'header', 'chatlab': {'version': '0.0.2'}, 'meta': meta}) + '\n')
        if include_member:
            for member in ('synthetic-self', peer):
                stream.write(json.dumps({'_type': 'member', 'platformId': member}) + '\n')
        for i in range(count):
            stream.write(json.dumps({'_type': 'message', 'platformMessageId': 'synthetic-' + str(i),
                'sender': 'synthetic-self' if i % 2 == 0 else peer, 'timestamp': 1000000 + i, 'type': 0,
                'content': f'Synthetic row {i}: ' + ('合成内容，不是真实对话。' * 35 if count > 100 else '')}, ensure_ascii=False) + '\n')


class LargeImportRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.index = ChatLabImporter(self.root / 'data')

    def tearDown(self):
        self.temp.cleanup()

    def test_large_jsonl_is_read_incrementally_and_reports_progress_before_eof(self):
        source = self.root / 'synthetic-large.jsonl'
        count = 12005
        write_jsonl(source, count)
        size = source.stat().st_size
        self.assertGreater(size, 8 * 1024 * 1024)
        real_open = Path.open
        observed = {'stream': None, 'read_calls': 0}
        progress = []
        class BoundedStream:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                observed['stream'] = self.stream
                return self
            def __exit__(self, *args):
                self.stream.close()
            def readline(self, maximum=-1):
                if maximum < 1 or maximum > MAX_LINE_BYTES + 1:
                    raise AssertionError('JSONL reads must be line bounded')
                observed['read_calls'] += 1
                return self.stream.readline(maximum)
            def read(self, *args):
                raise AssertionError('Large JSONL must not be materialized through read()')
        def tracked_open(path, *args, **kwargs):
            stream = real_open(path, *args, **kwargs)
            return BoundedStream(stream) if path.resolve() == source and args and args[0] == 'r' else stream
        def on_progress(value):
            stream = observed['stream']
            progress.append((dict(value), stream.tell() if stream and not stream.closed else size))
        with patch.object(Path, 'open', tracked_open), patch.object(Path, 'read_text', side_effect=AssertionError('JSONL cannot use read_text')):
            result = self.index.import_files([str(source)], progress=on_progress)
        self.assertEqual(result['messages'], count)
        self.assertGreater(observed['read_calls'], count)
        self.assertTrue(any(0 < item['messages'] < count and position < size for item, position in progress))
        self.assertEqual([item['messages'] for item, _ in progress], sorted(item['messages'] for item, _ in progress))
        self.assertEqual(progress[-1][0]['messages'], count)
        self.assertEqual(progress[-1][0]['files_done'], 1)
        sid = self.index.list_sessions()['items'][0]['id']
        latest = self.index.messages(sid, limit=17)
        self.assertEqual(len(latest['items']), 17)
        self.assertTrue(latest['has_more'])
        self.assertTrue(latest['items'][-1]['text'].startswith(f'Synthetic row {count - 1}:'))
        older = self.index.messages(sid, cursor=latest['next_cursor'], limit=17)
        self.assertTrue(set(m['id'] for m in latest['items']).isdisjoint(m['id'] for m in older['items']))

    def test_private_jsonl_uses_member_identity_not_display_name(self):
        for index, peer in enumerate(('synthetic-friend-one', 'synthetic-friend-two')):
            source = self.root / f'private-{index}.jsonl'
            write_jsonl(source, 3, private=True, peer=peer)
            self.index.import_files([str(source)])
        catalog = self.index.list_sessions()
        self.assertEqual(catalog['total'], 2)
        self.assertEqual(len({item['id'] for item in catalog['items']}), 2)
        self.assertEqual({item['count'] for item in catalog['items']}, {3})
        for item in catalog['items']:
            senders = {m['sender'] for m in self.index.messages(item['id'])['items']}
            self.assertEqual(len(senders - {'synthetic-self'}), 1)

    def test_private_jsonl_missing_stable_identity_is_rejected_atomically(self):
        source = self.root / 'ambiguous-private.jsonl'
        write_jsonl(source, 1, private=True, peer='synthetic-self', include_member=False)
        with self.assertRaises(ValueError):
            self.index.import_files([str(source)])
        self.assertEqual(self.index.count(), 0)

    def test_invalid_tail_rolls_back_partial_bulk_import_without_harming_existing_data(self):
        original = self.root / 'initial.jsonl'
        write_jsonl(original, 3, suffix='-valid')
        self.index.import_files([str(original)])
        before = self.index.list_sessions()['items']
        broken = self.root / 'broken.jsonl'
        write_jsonl(broken, 3001, suffix='-broken')
        with broken.open('a', encoding='utf-8') as stream:
            stream.write('{invalid synthetic final record}\n')
        with self.assertRaises(ValueError):
            self.index.import_files([str(broken)])
        after = self.index.list_sessions()['items']
        self.assertEqual([(item['id'], item['count']) for item in after], [(item['id'], item['count']) for item in before])


class CatalogMergeRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.native = patch('desk.capture_service.CaptureService')
        self.native.start()
        self.addCleanup(self.native.stop)
        self.ctrl = Controller(Path(self.temp.name).resolve() / 'data')

    def tearDown(self):
        self.ctrl.close()
        APP.processEvents()
        self.temp.cleanup()

    def test_interleaved_sources_duplicate_boundaries_and_last_partial_page(self):
        def item(ident, updated, source):
            return {'id': ident, 'title': 'Synthetic ' + ident, 'updated': updated, 'source': source, 'type': 'private'}
        hub_pages = {None: {'items': [item('common', 20, 'weflow'), item('hub-old', 10, 'weflow')], 'next_cursor': 'h2', 'has_more': True},
                     'h2': {'items': [item('hub-tail', 4, 'weflow')], 'next_cursor': None, 'has_more': False}}
        imp_pages = {None: {'items': [item('imp-new', 30, 'import'), item('common', 20, 'import')], 'next_cursor': 'i2', 'has_more': True},
                     'i2': {'items': [item('imp-tail', 1, 'import')], 'next_cursor': None, 'has_more': False}}
        def hub_page(config, query, cursor, limit):
            return {**hub_pages[cursor], 'available': True, 'source': 'weflow'}
        def import_page(query, cursor, limit):
            return {**imp_pages[cursor], 'available': True, 'source': 'import'}
        config = {**self.ctrl.store.full_config(), 'weflow_token': 'synthetic-only'}
        with patch.object(self.ctrl.hub, 'list_sessions', side_effect=hub_page), patch.object(self.ctrl.imports, 'list_sessions', side_effect=import_page), patch.object(self.ctrl.imports, 'count', return_value=3):
            first = self.ctrl._list_sessions_task(config, '', None, 2, 'all')
            second = self.ctrl._list_sessions_task(config, '', first['next_cursor'], 2, 'all')
            third = self.ctrl._list_sessions_task(config, '', second['next_cursor'], 2, 'all')
        all_items = first['items'] + second['items'] + third['items']
        self.assertEqual([item['id'] for item in all_items], ['imp-new', 'common', 'hub-old', 'hub-tail', 'imp-tail'])
        self.assertFalse(third['has_more'])
        self.assertIsNone(third['next_cursor'])
        self.assertEqual(third['scope'], 'all_available')

    def test_catalog_cursor_cannot_be_reused_across_search_or_source(self):
        cursor = self.ctrl._catalog_cursor({'source': 'all', 'query': 'Synthetic', 'offset': 0})
        for source, query in (('all', 'Different'), ('collected', 'Synthetic')):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.ctrl._read_catalog_cursor(cursor, source, query)
        self.ctrl.catalog_cursors[cursor] = (time.monotonic() - 601, self.ctrl.catalog_cursors[cursor][1])
        with self.assertRaises(ValueError):
            self.ctrl._read_catalog_cursor(cursor, 'all', 'Synthetic')

    def test_overlapping_history_pages_preserve_newer_enriched_message_and_selection(self):
        self.ctrl.sessions['archive:a'] = {'id': 'archive:a', 'title': 'Synthetic archive', 'source': 'import',
            'messages': [{'id': 'm2', 'timestamp': 2, 'text': '[图片识别] 合成描述', 'image_description': '合成描述'},
                         {'id': 'm3', 'timestamp': 3, 'text': 'newest'}]}
        self.ctrl.current_id = 'archive:a'
        self.ctrl._on_session_loaded('archive:a', {'kind': 'older', 'result': {
            'items': [{'id': 'm1', 'timestamp': 1, 'text': 'oldest'}, {'id': 'm2', 'timestamp': 2, 'text': '[图片]'}],
            'has_more': False, 'next_cursor': None}}, None)
        session = self.ctrl.sessions['archive:a']
        self.assertEqual([m['id'] for m in session['messages']], ['m1', 'm2', 'm3'])
        self.assertEqual(session['messages'][1]['image_description'], '合成描述')
        self.assertEqual(self.ctrl.current_id, 'archive:a')
        self.assertFalse(session['has_more'])


if __name__ == '__main__':
    unittest.main()
