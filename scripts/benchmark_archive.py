"""Measure local synthetic archive import, prewarming and bounded queries."""
import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from desk.imports import ChatLabImporter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--messages', type=int, default=100000)
    parser.add_argument('--sessions', type=int, default=100)
    args = parser.parse_args()
    if not 100 <= args.messages <= 1000000:
        parser.error('--messages must be between 100 and 1000000')
    if not 1 <= args.sessions <= min(args.messages, 1000):
        parser.error('--sessions must be between 1 and 1000 and no greater than --messages')
    with tempfile.TemporaryDirectory(prefix='zhixian-benchmark-') as directory:
        root = Path(directory)
        files = []
        for session in range(args.sessions):
            source = root / f'synthetic-{session}.jsonl'
            files.append(str(source))
            header = {'_type': 'header', 'chatlab': {'version': '0.0.2'},
                      'meta': {'name': f'合成客户基准 {session}', 'type': 'private', 'ownerId': 'me'},
                      'members': [{'platformId': 'me'}, {'platformId': f'customer-{session}'}]}
            with source.open('w', encoding='utf-8') as output:
                output.write(json.dumps(header, ensure_ascii=False) + '\n')
                for index in range(session, args.messages, args.sessions):
                    row = {'_type': 'message', 'platformMessageId': str(index), 'sender': f'customer-{session}',
                           'timestamp': 1700000000 + index, 'type': 0,
                           'content': f'合成项目信息 {index}' + (' 报价待确认' if index % 997 == 0 else '')}
                    output.write(json.dumps(row, ensure_ascii=False) + '\n')
        archive = ChatLabImporter(root)
        start = time.perf_counter()
        archive.import_files(files)
        imported = time.perf_counter() - start
        start = time.perf_counter()
        archive.warm_search()
        warmed = time.perf_counter() - start
        timings = {}
        for query in ('报价待确认', '项目信息'):
            samples = []
            for _ in range(20):
                start = time.perf_counter()
                hits = archive.search(query, limit=20)
                samples.append((time.perf_counter() - start) * 1000)
                assert hits['items'] and len(hits['items']) <= 20
            timings[query] = {'p50_ms': round(statistics.median(samples), 2), 'p95_ms': round(sorted(samples)[18], 2)}
        first = hits['items'][0]
        page = archive.context(first['session_id'], first['id'])
        assert any(m['id'] == first['id'] for m in page['items'])
        start = time.perf_counter()
        catalog = archive.list_sessions(limit=20)
        catalog_ms = (time.perf_counter() - start) * 1000
        assert catalog['total'] == args.sessions and len(catalog['items']) <= 20
        assert (catalog['items'][0]['count'] - args.messages // args.sessions) in (0, 1)
        print(json.dumps({'messages': args.messages, 'sessions': catalog['total'],
                          'import_seconds': round(imported, 3), 'prewarm_seconds': round(warmed, 3),
                          'queries': timings, 'catalog_first_page_ms': round(catalog_ms, 2),
                          'returned_page_limit': 20, 'context_verified': True,
                          'scope': 'Synthetic local SQLite; no external model or real WeChat.'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
