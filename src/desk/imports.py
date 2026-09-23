"""User-initiated, local-only ChatLab 0.0.2 archive indexing.

CipherTalk exports one conversation per JSON/JSONL file. Indexes contain user
data and live only in the application's excluded data directory. No exporter
code, database key extraction, or media from another app is bundled here.
"""
from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath

MAX_JSON_BYTES = 128 * 1024 * 1024
MAX_JSONL_BYTES = 1024 * 1024 * 1024
MAX_LINE_BYTES = 2 * 1024 * 1024
MEDIA_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.wav', '.mp3', '.m4a', '.ogg', '.opus', '.amr'}
KINDS = {0: 'text', 1: 'image', 2: 'voice', 3: 'video', 5: 'emoji', 7: 'link', 8: 'location', 27: 'contact', 23: 'call', 80: 'system'}
LABELS = {'image': '[图片]', 'voice': '[语音]', 'video': '[视频]', 'emoji': '[表情]', 'link': '[链接]', 'location': '[位置]', 'contact': '[名片]', 'call': '[通话]', 'system': '[系统消息]'}


def _safe_media_path(export_dir: Path, content: str, kind: str) -> str:
    if kind not in ('image', 'voice', 'video'):
        return ''
    # CipherTalk's media export writes `[图片] relative/path.jpg` and
    # `[语音消息] relative/path.wav` into the message content when enabled.
    match = re.match(r'^\[(?:图片|语音消息|视频)\]\s+(.+?\.(?:jpe?g|png|webp|gif|wav|mp3|m4a|ogg|opus|amr))(?:\s|$)', content, re.I)
    if not match:
        return ''
    candidate = match.group(1).strip().replace('\\', '/')
    if candidate.startswith('/') or PureWindowsPath(candidate).is_absolute() or '://' in candidate:
        return ''
    resolved = (export_dir / candidate).resolve()
    if not resolved.is_relative_to(export_dir.resolve()) or resolved.suffix.lower() not in MEDIA_EXT:
        return ''
    return candidate if resolved.is_file() else ''


def _summary(message):
    try:
        kind = KINDS.get(int(message.get('type') or 0), 'other')
    except (TypeError, ValueError):
        kind = 'other'
    text = str(message.get('content') or '').strip()
    if kind == 'text':
        return text[:140], kind
    return (LABELS.get(kind, '[其他消息]') + (' ' + text[:110] if text and not text.startswith('[') else '')), kind


class ChatLabImporter:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / 'chatlab-index.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                  id TEXT PRIMARY KEY, title TEXT NOT NULL, talker TEXT, type TEXT,
                  source_dir TEXT NOT NULL, latest TEXT, latest_kind TEXT,
                  updated REAL NOT NULL DEFAULT 0, count INTEGER NOT NULL DEFAULT 0,
                  owner_id TEXT NOT NULL DEFAULT '', imported_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                  session_id TEXT NOT NULL, id TEXT NOT NULL, sender TEXT,
                  side TEXT NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL,
                  timestamp REAL, media_rel TEXT NOT NULL DEFAULT '',
                  PRIMARY KEY(session_id,id)
                );
                CREATE INDEX IF NOT EXISTS messages_page ON messages(session_id,timestamp DESC,id DESC);
                CREATE INDEX IF NOT EXISTS sessions_updated ON sessions(updated DESC,id);
            ''')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _parse(self, path: Path):
        if path.suffix.lower() == '.jsonl':
            if path.stat().st_size > MAX_JSONL_BYTES:
                raise ValueError('JSONL 聊天文件过大，请分批导出。')
            with path.open('r', encoding='utf-8-sig') as stream:
                header_line = stream.readline(MAX_LINE_BYTES + 1)
                if len(header_line.encode('utf-8')) > MAX_LINE_BYTES:
                    raise ValueError('ChatLab 头部过长。')
                header = json.loads(header_line)
                if not isinstance(header, dict) or header.get('_type') != 'header':
                    raise ValueError('JSONL 首行不是 ChatLab header。')
                # ChatLab JSONL stores participants as rows after the header.
                # Read only that small prefix before deriving a private-chat ID;
                # the messages themselves remain streaming.
                members = list(header.get('members') or [])
                first_message = None
                while line := stream.readline(MAX_LINE_BYTES + 1):
                    if len(line.encode('utf-8')) > MAX_LINE_BYTES:
                        raise ValueError('聊天记录单行过长。')
                    item = json.loads(line)
                    if not isinstance(item, dict):
                        continue
                    if item.get('_type') == 'member':
                        if len(members) >= 10000:
                            raise ValueError('ChatLab 成员列表过长。')
                        members.append(item)
                    elif item.get('_type') == 'message':
                        first_message = item
                        break
                header['members'] = members
                def rows():
                    if first_message is not None:
                        yield first_message
                    while line := stream.readline(MAX_LINE_BYTES + 1):
                        if len(line.encode('utf-8')) > MAX_LINE_BYTES:
                            raise ValueError('聊天记录单行过长。')
                        item = json.loads(line)
                        if isinstance(item, dict) and item.get('_type') == 'message':
                            yield item
                yield header, rows()
        else:
            if path.stat().st_size > MAX_JSON_BYTES:
                raise ValueError('JSON 文件过大；请使用 ChatLab JSONL 流式导出。')
            payload = json.loads(path.read_text(encoding='utf-8-sig'))
            if not isinstance(payload, dict):
                raise ValueError('无法识别 ChatLab 文件。')
            yield payload, iter(payload.get('messages') or [])

    def import_files(self, files: list[str], progress=None) -> dict:
        if not files or len(files) > 5000:
            raise ValueError('请一次选择 1–5000 个 ChatLab 文件。')
        total, imported = 0, []
        with self.lock:
            for file_index, filename in enumerate(files, 1):
                path = Path(filename)
                if path.suffix.lower() not in ('.json', '.jsonl') or not path.is_file():
                    raise ValueError('只支持 ChatLab JSON 或 JSONL 文件。')
                with self._connect() as db:
                    for header, messages in self._parse(path):
                        lab = header.get('chatlab') or {}
                        meta = header.get('meta') or {}
                        if not isinstance(lab, dict) or not str(lab.get('version') or '').startswith('0.0.'):
                            raise ValueError('文件不是受支持的 ChatLab 导出格式。')
                        if not isinstance(meta, dict) or not meta.get('name') or meta.get('type') not in ('private', 'group'):
                            raise ValueError('ChatLab 缺少会话名称或类型。')
                        owner = str(meta.get('ownerId') or '')
                        if not owner:
                            raise ValueError('ChatLab 缺少 ownerId，无法可靠区分你的消息。')
                        if meta['type'] == 'group':
                            identity = str(meta.get('groupId') or '')
                            if not identity:
                                raise ValueError('群聊导出缺少稳定的 groupId。')
                        else:
                            # Include known participant IDs so equal display names remain separate.
                            ids = sorted(str(m.get('platformId')) for m in header.get('members', []) if isinstance(m, dict) and m.get('platformId') and m.get('platformId') != owner)
                            if not ids:
                                raise ValueError('单聊导出缺少稳定的好友帐号 ID，不能仅凭同名昵称合并。')
                            identity = '|'.join(ids)
                        sid = 'import:' + hashlib.sha256(f"{meta.get('platform', 'wechat')}|{owner}|{identity}".encode()).hexdigest()[:24]
                        count, latest, latest_kind, updated = 0, '', 'text', 0.0
                        occurrences = {}
                        for raw in messages:
                            if not isinstance(raw, dict):
                                continue
                            text = str(raw.get('content') or '')[:16000]
                            try:
                                kind_number = int(raw.get('type') or 0)
                            except (ValueError, TypeError):
                                kind_number = -1
                            kind = KINDS.get(kind_number, 'other')
                            sender = str(raw.get('sender') or '')[:240]
                            side = 'me' if sender == owner else 'other'
                            try:
                                timestamp = float(raw.get('timestamp') or 0)
                            except (ValueError, TypeError):
                                timestamp = 0.0
                            timestamp = timestamp if 0 < timestamp < 10**13 else 0.0
                            original_id = str(raw.get('platformMessageId') or '')
                            if not original_id:
                                signature = hashlib.sha256(f'{sender}|{timestamp}|{kind_number}|{text}'.encode()).hexdigest()
                                occurrences[signature] = occurrences.get(signature, 0) + 1
                                original_id = hashlib.sha256(repr((signature, occurrences[signature])).encode()).hexdigest()
                            message_id = 'impmsg:' + hashlib.sha256(f'{sid}|{original_id}'.encode()).hexdigest()[:24]
                            media_rel = _safe_media_path(path.parent, text, kind)
                            db.execute('''INSERT OR REPLACE INTO messages(session_id,id,sender,side,text,kind,timestamp,media_rel)
                                VALUES(?,?,?,?,?,?,?,?)''', (sid, message_id, sender, side, text, kind, timestamp, media_rel))
                            count += 1
                            if progress and count % 3000 == 0:
                                progress({'files_done': file_index-1, 'files_total': len(files),
                                          'messages': total+count, 'sessions': len(imported)})
                            if timestamp >= updated:
                                latest, latest_kind, updated = *_summary(raw), timestamp
                        title = str(meta['name']).strip()[:200]
                        db.execute('''INSERT INTO sessions(id,title,talker,type,source_dir,latest,latest_kind,updated,count,owner_id,imported_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?)
                            ON CONFLICT(id) DO UPDATE SET title=excluded.title,source_dir=excluded.source_dir,
                              latest=excluded.latest,latest_kind=excluded.latest_kind,updated=excluded.updated,
                              count=(SELECT COUNT(*) FROM messages WHERE session_id=excluded.id),imported_at=excluded.imported_at''',
                                   (sid, title, identity, meta['type'], str(path.parent.resolve()), latest, latest_kind, updated, count, owner, time.time()))
                        imported.append({'id': sid, 'title': title, 'count': count})
                        total += count
                if progress:
                    progress({'files_done': file_index, 'files_total': len(files),
                              'messages': total, 'sessions': len(imported)})
        return {'success': True, 'sessions': len(imported), 'messages': total, 'items': imported}

    def list_sessions(self, query='', cursor=None, limit=20):
        limit = min(100, max(1, int(limit)))
        offset = max(0, int(cursor or 0))
        query = str(query or '').strip()[:100]
        escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        where, args = ("WHERE title LIKE ? ESCAPE '\\'", [f'%{escaped}%']) if query else ('', [])
        with self.lock, self._connect() as db:
            total = db.execute(f'SELECT COUNT(*) FROM sessions {where}', args).fetchone()[0]
            rows = db.execute(f'SELECT * FROM sessions {where} ORDER BY updated DESC,id LIMIT ? OFFSET ?', (*args, limit, offset)).fetchall()
        items = [self._session(row) for row in rows]
        next_cursor = str(offset + len(items)) if offset + len(items) < total else None
        return {'items': items, 'next_cursor': next_cursor, 'has_more': bool(next_cursor), 'total': total,
                'source': 'import', 'scope': 'imported_archive', 'available': True}

    def get_session(self, sid):
        with self.lock, self._connect() as db:
            row = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
        return self._session(row) if row else None

    def messages(self, sid, cursor=None, limit=50):
        limit = min(200, max(1, int(limit)))
        offset = max(0, int(cursor or 0))
        with self.lock, self._connect() as db:
            rows = db.execute('SELECT * FROM messages WHERE session_id=? ORDER BY timestamp DESC,id DESC LIMIT ? OFFSET ?', (sid, limit+1, offset)).fetchall()
        has_more = len(rows) > limit
        result = [self._message(row) for row in reversed(rows[:limit])]
        return {'items': result, 'next_cursor': str(offset+limit) if has_more else None,
                'has_more': has_more, 'source': 'import', 'scope': 'imported_archive', 'available': True}

    def resolve_media(self, sid, message_id):
        with self.lock, self._connect() as db:
            row = db.execute('SELECT s.source_dir,m.media_rel,m.kind FROM messages m JOIN sessions s ON s.id=m.session_id WHERE m.session_id=? AND m.id=?', (sid, message_id)).fetchone()
        if not row or not row['media_rel']:
            return {'status': 'unavailable', 'reason': '该消息没有可用的本地媒体文件。'}
        root = Path(row['source_dir']).resolve()
        candidate = (root / row['media_rel']).resolve()
        if not candidate.is_relative_to(root) or candidate.suffix.lower() not in MEDIA_EXT or not candidate.is_file():
            return {'status': 'unavailable', 'reason': '导出媒体已移动或路径无效。'}
        if candidate.stat().st_size > 12 * 1024 * 1024:
            return {'status': 'unavailable', 'reason': '媒体文件超过 12 MB，请先压缩或转写。'}
        mime = mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream'
        return {'status': 'ok', 'kind': row['kind'], 'mime_type': mime, 'data': candidate.read_bytes()}

    def count(self):
        with self.lock, self._connect() as db:
            return db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0]

    @staticmethod
    def _session(row):
        return {'id': row['id'], 'title': row['title'], 'source': 'import', 'talker': row['talker'],
                'type': row['type'], 'preview': row['latest'] or LABELS.get(row['latest_kind'], ''),
                'preview_status': row['latest_kind'], 'updated': row['updated'], 'count': row['count'], 'unread_count': 0}

    @staticmethod
    def _message(row):
        kind = row['kind']
        media = [{'id': 'impasset:' + hashlib.sha256(f"{row['session_id']}|{row['id']}".encode()).hexdigest()[:24],
                  'kind': kind, 'status': 'ready'}] if row['media_rel'] else []
        return {'id': row['id'], 'side': row['side'], 'sender': row['sender'], 'text': row['text'] or LABELS.get(kind, ''),
                'kind': kind, 'timestamp': row['timestamp'], 'source': 'import', 'media': media}
