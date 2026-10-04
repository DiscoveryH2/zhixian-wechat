"""User-confirmed follow-ups with evidence and optimistic concurrency."""
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import time
import uuid

STATES = ('open', 'done', 'dismissed')


class ActionStore:
    def __init__(self, directory):
        self.path = Path(directory) / 'actions.sqlite3'
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, title TEXT NOT NULL,
                    session_title TEXT NOT NULL, detail TEXT NOT NULL, evidence TEXT NOT NULL,
                    due_at TEXT, state TEXT NOT NULL DEFAULT 'open', revision INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS actions_state_due ON actions(state,due_at,updated_at);
            ''')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _due(value):
        if value in (None, ''):
            return None
        try:
            stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError
            return stamp.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, OverflowError):
            raise ValueError('跟进时间需要包含时区；可以暂不设置。') from None

    def create(self, item, session):
        if not isinstance(item, dict):
            raise ValueError('行动内容格式无效。')
        title = str(item.get('title') or '').strip()
        if not title or len(title) > 200:
            raise ValueError('请填写 1–200 字的行动标题。')
        known = {str(m.get('id')): m for m in session.get('messages', []) if isinstance(m, dict) and m.get('id')}
        ids = item.get('message_ids') or []
        if not isinstance(ids, list) or len(ids) > 5 or any(str(mid) not in known for mid in ids):
            raise ValueError('消息证据已过期，请重新选择。')
        evidence = [{'message_id': str(mid), 'text': str(known[str(mid)].get('text') or '')[:500],
                     'side': known[str(mid)].get('side')} for mid in dict.fromkeys(ids)]
        now, ident = time.time(), uuid.uuid4().hex
        values = (ident, session['id'], str(session.get('title') or '')[:200], title,
                  str(item.get('detail') or '')[:4000], json.dumps(evidence, ensure_ascii=False), self._due(item.get('due_at')), now, now)
        with self._connect() as db:
            db.execute('INSERT INTO actions(id,session_id,session_title,title,detail,evidence,due_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)', values)
            return self._item(db.execute('SELECT * FROM actions WHERE id=?', (ident,)).fetchone())

    @staticmethod
    def _item(row):
        result = dict(row)
        result['evidence'] = json.loads(result['evidence'])
        return result

    def list(self, state='open', offset=0, limit=30):
        if state not in (*STATES, 'all'):
            raise ValueError('行动状态无效。')
        limit, offset = max(1, min(int(limit), 100)), max(0, int(offset))
        where, params = ('', ()) if state == 'all' else ('WHERE state=?', (state,))
        with self._connect() as db:
            rows = db.execute(f'SELECT * FROM actions {where} ORDER BY due_at IS NULL,due_at,updated_at DESC,id LIMIT ? OFFSET ?', (*params, limit + 1, offset)).fetchall()
        return {'items': [self._item(row) for row in rows[:limit]], 'has_more': len(rows) > limit,
                'next_cursor': offset + limit if len(rows) > limit else None}

    def transition(self, ident, state, revision):
        if state not in STATES or isinstance(revision, bool) or not isinstance(revision, int):
            raise ValueError('行动状态或版本无效。')
        with self._connect() as db:
            changed = db.execute('UPDATE actions SET state=?,revision=revision+1,updated_at=? WHERE id=? AND revision=?',
                                 (state, time.time(), str(ident), revision)).rowcount
            if not changed:
                raise ValueError('行动已被更新，请刷新后重试。')
            return self._item(db.execute('SELECT * FROM actions WHERE id=?', (str(ident),)).fetchone())
