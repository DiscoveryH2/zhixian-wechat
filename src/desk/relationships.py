"""Identity-scoped history, durable Moments and source-backed simulations."""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from pathlib import Path
from .imports import stable_identity


def identity(platform, owner, username):
    return 'contact:' + hashlib.sha256(json.dumps([platform, owner, username], ensure_ascii=False).encode()).hexdigest()[:24]


def _limit(value, maximum=100):
    return max(1, min(int(value or 24), maximum))


def _timestamp(value):
    try:
        number = float(value or 0)
        return number if math.isfinite(number) and 0 < number < 32503680000 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


class RelationshipStore:
    def __init__(self, archives):
        self.archives = archives
        self.lock = archives.lock
        with self.lock, archives._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS relationship_contacts (
                  id TEXT PRIMARY KEY, platform TEXT NOT NULL, owner TEXT NOT NULL,
                  username TEXT NOT NULL, name TEXT NOT NULL,
                  UNIQUE(platform,owner,username)
                );
                CREATE TABLE IF NOT EXISTS relationship_moments (
                  id TEXT PRIMARY KEY, platform TEXT NOT NULL, owner TEXT NOT NULL,
                  username TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
                  author TEXT NOT NULL, text TEXT NOT NULL, timestamp REAL NOT NULL,
                  payload TEXT NOT NULL, origin TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS moments_author ON relationship_moments(platform,owner,username,timestamp DESC,id);
                CREATE TABLE IF NOT EXISTS personas (
                  id TEXT PRIMARY KEY, contact_id TEXT NOT NULL, name TEXT NOT NULL,
                  mode TEXT NOT NULL, created REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                  coverage TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS persona_evidence (
                  persona_id TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                  active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(persona_id,id)
                );
                CREATE TABLE IF NOT EXISTS persona_turns (
                  id INTEGER PRIMARY KEY, persona_id TEXT NOT NULL, user_text TEXT NOT NULL,
                  reply TEXT NOT NULL, evidence TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS persona_history ON persona_turns(persona_id,id DESC);
                CREATE TABLE IF NOT EXISTS archive_sync (
                  source_id TEXT PRIMARY KEY, archive_id TEXT NOT NULL, cursor TEXT, complete INTEGER NOT NULL DEFAULT 0
                );
            ''')
        self.refresh_contacts()

    def refresh_contacts(self):
        with self.lock, self.archives._connect() as db:
            rows = db.execute('''SELECT s.platform,s.owner_id,m.platform_id,
                COALESCE(MAX(CASE WHEN s.type='private' AND m.name!=m.platform_id THEN m.name END),
                         MAX(CASE WHEN s.type='private' AND s.talker=m.platform_id THEN s.title END),
                         MAX(CASE WHEN m.name!=m.platform_id THEN m.name END),m.platform_id) AS name
                FROM members m JOIN sessions s ON s.id=m.session_id
                WHERE m.platform_id NOT IN ('','unknown') AND m.platform_id!=s.owner_id
                GROUP BY s.platform,s.owner_id,m.platform_id''').fetchall()
            db.executemany('''INSERT INTO relationship_contacts VALUES(?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name''',
                [(identity(r['platform'], r['owner_id'], r['platform_id']), r['platform'], r['owner_id'], r['platform_id'], r['name']) for r in rows])

    def contacts(self, query='', cursor=0, limit=24):
        self.refresh_contacts()
        limit, offset = _limit(limit), max(0, int(cursor or 0))
        escaped = str(query or '').strip()[:100].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        with self.lock, self.archives._connect() as db:
            rows = db.execute('''SELECT * FROM relationship_contacts WHERE name LIKE ? ESCAPE '\\'
                OR username LIKE ? ESCAPE '\\' ORDER BY name,id LIMIT ? OFFSET ?''',
                (f'%{escaped}%', f'%{escaped}%', limit+1, offset)).fetchall()
        return {'items': [dict(r) for r in rows[:limit]], 'has_more': len(rows)>limit,
                'next_cursor': offset+limit if len(rows)>limit else None, 'scope': 'indexed_archives'}

    def contact(self, cid):
        with self.lock, self.archives._connect() as db:
            row = db.execute('SELECT * FROM relationship_contacts WHERE id=?', (str(cid),)).fetchone()
        if not row:
            raise ValueError('请选择已经索引的联系人。')
        return dict(row)

    def contact_for_session(self, sid):
        session = self.archives.get_session(sid)
        if not session or session['type'] != 'private' or '|' in session['talker']:
            raise ValueError('请选择身份明确的单聊；群成员请从联系人目录选择。')
        self.refresh_contacts()
        return self.contact(identity(session['platform'], session['owner_id'], session['talker']))

    def _scope(self, cid=None, sid=None, target_only=False):
        if cid:
            contact = self.contact(cid)
            suffix = 'm.sender=?' if target_only else "((s.type='private' AND s.talker=?) OR (s.type='group' AND m.sender=?))"
            return 's.platform=? AND s.owner_id=? AND ' + suffix, [contact['platform'], contact['owner'], contact['username']] + ([] if target_only else [contact['username']])
        session = self.archives.get_session(sid)
        if not session:
            raise ValueError('请先索引需要分析的聊天记录。')
        return 's.id=?', [sid]

    def statistics(self, cid=None, sid=None):
        where, args = self._scope(cid, sid)
        with self.lock, self.archives._connect() as db:
            row = db.execute(f'''SELECT COUNT(*) AS messages,
                MIN(CASE WHEN m.timestamp>0 THEN m.timestamp END) AS first_at,
                MAX(m.timestamp) AS last_at,
                COUNT(DISTINCT CASE WHEN m.sender NOT IN ('','unknown') THEN m.sender END) AS speaker_count,
                SUM(CASE WHEN m.side='me' THEN 1 ELSE 0 END) AS mine,
                SUM(CASE WHEN m.side='unknown' THEN 1 ELSE 0 END) AS unknown_direction,
                SUM(CASE WHEN m.kind IN ('image','voice') THEN 1 ELSE 0 END) AS media_messages,
                SUM(CASE WHEN m.kind IN ('image','voice') AND length(trim(u.text))>0 THEN 1 ELSE 0 END) AS understood_media,
                SUM(CASE WHEN m.kind='text' AND length(m.text)>0 THEN 1 ELSE 0 END) AS text_messages
                FROM messages m JOIN sessions s ON s.id=m.session_id
                LEFT JOIN message_understanding u ON u.session_id=m.session_id AND u.message_id=m.id
                  AND u.original_text=m.text AND u.sender=m.sender AND u.kind=m.kind
                WHERE {where}''', args).fetchone()
            sessions = [dict(r) for r in db.execute(f'''SELECT s.id,s.title,s.type,s.origin,COUNT(*) AS messages
                FROM messages m JOIN sessions s ON s.id=m.session_id WHERE {where}
                GROUP BY s.id ORDER BY messages DESC,s.id''', args)]
            speakers = [dict(r) for r in db.execute(f'''SELECT m.sender AS username,
                COALESCE(MAX(NULLIF(member.name,'')),m.sender) AS name,COUNT(*) AS messages
                FROM messages m JOIN sessions s ON s.id=m.session_id
                LEFT JOIN members member ON member.session_id=m.session_id AND member.platform_id=m.sender
                WHERE {where} GROUP BY m.sender ORDER BY messages DESC,m.sender LIMIT 30''', args)]
            activity = [dict(r) for r in db.execute(f'''SELECT strftime('%Y-%m',m.timestamp,'unixepoch') AS month,COUNT(*) AS messages
                FROM messages m JOIN sessions s ON s.id=m.session_id WHERE {where} AND m.timestamp>0
                GROUP BY month ORDER BY month DESC LIMIT 36''', args)]
            kinds = {r['kind']: r['count'] for r in db.execute(f'''SELECT m.kind,COUNT(*) AS count
                FROM messages m JOIN sessions s ON s.id=m.session_id WHERE {where} GROUP BY m.kind''', args)}
        counts = dict(row)
        for key in ('mine', 'unknown_direction', 'media_messages', 'text_messages', 'understood_media'):
            counts[key] = counts[key] or 0
        counts['uninterpreted_media'] = counts['media_messages'] - counts['understood_media']
        moments = self.moments(cid=cid, limit=1)['total'] if cid else 0
        return {**counts, 'kinds': kinds, 'sessions': sessions, 'speakers': speakers, 'activity': activity,
                'moments': moments, 'scope': 'all_indexed_records', 'timezone': 'UTC',
                'warning': '统计覆盖已索引记录，不代表微信完整账户；群聊按稳定发送者 ID 关联，图片/语音未识别时只计类型。'}

    @staticmethod
    def _evidence(row):
        return {'id': row['id'], 'session_id': row['session_id'], 'source_kind': row['session_type'],
                'source_title': row['session_title'], 'sender': row['sender'], 'side': row['side'],
                'timestamp': row['timestamp'], 'text': row['analysis_text'][:1600],
                'interpretation': row['interpretation']}

    def corpus(self, cid=None, sid=None, target_only=False, maximum=180, include_moments=True, include_groups=True):
        where, args = self._scope(cid, sid, target_only)
        where += " AND ((m.kind='text' AND length(trim(m.text))>0) OR length(trim(u.text))>0) AND m.side!='unknown'"
        if not include_groups:
            where += " AND s.type='private'"
        maximum = _limit(maximum, 300)
        joins = '''FROM messages m JOIN sessions s ON s.id=m.session_id
            LEFT JOIN message_understanding u ON u.session_id=m.session_id AND u.message_id=m.id
              AND u.original_text=m.text AND u.sender=m.sender AND u.kind=m.kind'''
        fields = '''m.*,s.title AS session_title,s.type AS session_type,
            COALESCE(u.text,m.text) AS analysis_text,
            CASE WHEN u.text IS NOT NULL THEN 'model_media_interpretation' ELSE 'original_text' END AS interpretation'''
        select = f'SELECT {fields} {joins} WHERE {where} ORDER BY m.timestamp,m.id'
        with self.lock, self.archives._connect() as db:
            count = db.execute(f'SELECT COUNT(*) {joins} WHERE {where}', args).fetchone()[0]
            rows = []
            if count <= maximum:
                rows = db.execute(select, args).fetchall()
            else:
                positions = [index*(count-1)//(maximum-1)+1 for index in range(maximum)] if maximum > 1 else [count]
                ranked = f'''SELECT {fields},
                    ROW_NUMBER() OVER (ORDER BY m.timestamp,m.id) AS position
                    {joins} WHERE {where}'''
                rows = db.execute('SELECT * FROM ('+ranked+') WHERE position IN ('+
                                  ','.join('?' for _ in positions)+') ORDER BY position', (*args,*positions)).fetchall()
        records = [self._evidence(r) for r in rows]
        if cid and include_moments:
            page = self.moments(cid=cid, limit=100)
            for post in page['items']:
                if post['text'].strip():
                    records.append({'id': post['id'], 'session_id': post.get('session_id') or '',
                        'source_kind': 'moment', 'source_title': post['author']+'的朋友圈',
                        'sender': post['username'], 'side': 'other', 'timestamp': post['timestamp'], 'text': post['text'][:1600]})
        return {'records': records, 'eligible_text_messages': count, 'sampled_messages': len(rows),
                'sampling': 'temporal_strata', 'moment_sampling': 'latest_100_text_posts', 'target_only': target_only}

    def save_moment(self, post, owner='', platform='wechat', origin='manual'):
        username = stable_identity(post.get('username') or '', 'username', allow_empty=True)
        raw_id = str(post.get('id') or '')[:256] or uuid.uuid4().hex
        ident = 'moment:' + hashlib.sha256(json.dumps([platform,owner,raw_id]).encode()).hexdigest()[:24]
        item = {'id': ident, 'author': str(post.get('author') or post.get('nickname') or username or '未标记作者')[:200],
                'username': username, 'session_id': str(post.get('session_id') or '')[:256],
                'text': str(post.get('text') or post.get('contentDesc') or '')[:16000],
                'timestamp': _timestamp(post.get('timestamp') or post.get('createTime')),
                'source': origin, 'media': [], 'image_descriptions': []}
        with self.lock, self.archives._connect() as db:
            db.execute('''INSERT INTO relationship_moments VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET author=excluded.author,text=excluded.text,
                timestamp=excluded.timestamp,payload=excluded.payload''',
                (ident, platform, owner, username, item['session_id'], item['author'], item['text'], item['timestamp'], json.dumps(item,ensure_ascii=False), origin))
            if owner and username and username not in (owner,'unknown'):
                db.execute('''INSERT INTO relationship_contacts VALUES(?,?,?,?,?) ON CONFLICT(id) DO NOTHING''',
                    (identity(platform,owner,username),platform,owner,username,item['author']))
        return item

    def import_moments(self, files, owner, platform='wechat'):
        if not isinstance(files,list) or not 1 <= len(files) <= 100:
            raise ValueError('请一次选择 1–100 个朋友圈 JSON 文件。')
        if not owner or len(owner)>240:
            raise ValueError('请填写数据所属的本人帐号 ID，避免混合多个帐号。')
        count = 0
        for filename in files:
            path = Path(filename)
            if path.suffix.lower() != '.json' or path.stat().st_size > 32*1024*1024:
                raise ValueError('朋友圈仅支持不超过 32 MB 的 JSON；请分批导出。')
            payload = json.loads(path.read_text(encoding='utf-8-sig'))
            if isinstance(payload,dict):
                declared = payload.get('owner_id') or payload.get('ownerId')
                if declared and declared != owner:
                    raise ValueError('文件所属帐号与选定帐号不一致。')
                declared_platform = payload.get('platform')
                if declared_platform and declared_platform != platform:
                    raise ValueError('朋友圈平台与选定聊天档案不一致。')
                posts = payload.get('items',payload.get('timeline',payload.get('posts')))
            else:
                posts = payload
            if not isinstance(posts,list) or len(posts)>100000:
                raise ValueError('JSON 应包含 items、timeline、posts 列表或直接使用帖子数组。')
            for post in posts:
                if not isinstance(post,dict) or not post.get('username') or not (post.get('id') or post.get('snsId')):
                    raise ValueError('每条动态必须有稳定 id 和作者 username；不能根据昵称推断作者。')
                stable_identity(post['username'], 'username')
                stable_identity(str(post.get('id') or post.get('snsId')), '朋友圈 id', 256)
            # A file commits atomically, including contact updates.
            with self.lock, self.archives._connect() as db:
                for post in posts:
                    raw_id = str(post.get('id') or post['snsId'])[:256]
                    uid = str(post['username'])[:240]
                    ident = 'moment:'+hashlib.sha256(json.dumps([platform,owner,raw_id]).encode()).hexdigest()[:24]
                    item = {'id':ident,'username':uid,'author':str(post.get('author') or post.get('nickname') or uid)[:200],
                            'text':str(post.get('text') or post.get('contentDesc') or '')[:16000],
                            'timestamp':_timestamp(post.get('timestamp') or post.get('createTime')),
                            'session_id':'','source':'import','media':[], 'image_descriptions':[]}
                    db.execute('''INSERT INTO relationship_moments VALUES(?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET author=excluded.author,text=excluded.text,timestamp=excluded.timestamp,payload=excluded.payload''',
                        (ident,platform,owner,uid,'',item['author'],item['text'],item['timestamp'],json.dumps(item,ensure_ascii=False),'import'))
                    if uid != owner:
                        db.execute('INSERT INTO relationship_contacts VALUES(?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                            (identity(platform,owner,uid),platform,owner,uid,item['author']))
                    count += 1
        return {'success':True,'posts':count,'warning':'已索引导出文字；媒体需原文件和显式识别，不自动下载远程图片。'}

    def moments(self, cid=None, sid=None, cursor=0, limit=24):
        limit, offset = _limit(limit), max(0,int(cursor or 0))
        if cid:
            contact = self.contact(cid)
            where, args = 'platform=? AND owner=? AND username=?', [contact['platform'],contact['owner'],contact['username']]
        elif sid:
            try:
                contact = self.contact_for_session(sid)
                where, args = '(platform=? AND owner=? AND username=?) OR session_id=?', [contact['platform'],contact['owner'],contact['username'],sid]
            except ValueError:
                where, args = 'session_id=?', [sid]
        else:
            where, args = '1=1', []
        with self.lock, self.archives._connect() as db:
            total = db.execute('SELECT COUNT(*) FROM relationship_moments WHERE '+where,args).fetchone()[0]
            rows = db.execute('SELECT payload FROM relationship_moments WHERE '+where+' ORDER BY timestamp DESC,id DESC LIMIT ? OFFSET ?',(*args,limit,offset)).fetchall()
        return {'items':[json.loads(r['payload']) for r in rows],'total':total,'has_more':offset+len(rows)<total,
                'next_cursor':offset+len(rows) if offset+len(rows)<total else None,'source':'local','available':bool(total),
                'scope':'indexed_moments','warning':'已保存的朋友圈文字；来源未提供原媒体时无法识别配图。'}

    def get_moment(self, mid):
        with self.lock,self.archives._connect() as db:
            row=db.execute('SELECT payload FROM relationship_moments WHERE id=?',(mid,)).fetchone()
        return json.loads(row['payload']) if row else None

    def create_persona(self, cid, name='', mode='companion', include_groups=True, include_moments=True):
        contact=self.contact(cid)
        if mode not in ('companion','memorial','rehearsal'):
            raise ValueError('分身模式无效。')
        corpus=self.corpus(cid=cid,target_only=True,maximum=240,include_moments=include_moments,include_groups=include_groups)
        records=[r for r in corpus['records'] if include_groups or r['source_kind']!='group']
        if not records:
            raise ValueError('没有这位联系人可用的本人文字记录；请先导入私聊、群聊或朋友圈。')
        coverage=self.statistics(cid=cid)
        coverage.update(sampled_memories=len(records),include_groups=include_groups,include_moments=include_moments)
        pid='persona:'+uuid.uuid4().hex
        with self.lock,self.archives._connect() as db:
            coverage['message_rowid_max'] = db.execute('SELECT COALESCE(MAX(rowid),0) FROM messages').fetchone()[0]
            coverage['moment_rowid_max'] = db.execute('SELECT COALESCE(MAX(rowid),0) FROM relationship_moments').fetchone()[0]
            db.execute('INSERT INTO personas VALUES(?,?,?,?,?,?,?)',(pid,cid,str(name or contact['name']).strip()[:100],mode,time.time(),1,json.dumps(coverage,ensure_ascii=False)))
            db.executemany('INSERT INTO persona_evidence VALUES(?,?,?,1)',[(pid,r['id'],json.dumps(r,ensure_ascii=False)) for r in records])
        return self.persona(pid)

    def personas(self, cursor=0, limit=24):
        limit,offset=_limit(limit),max(0,int(cursor or 0))
        with self.lock,self.archives._connect() as db:
            rows=db.execute('SELECT id FROM personas ORDER BY created DESC,id LIMIT ? OFFSET ?',(limit+1,offset)).fetchall()
        return {'items':[self.persona(r['id'],history=False) for r in rows[:limit]],'has_more':len(rows)>limit,
                'next_cursor':offset+limit if len(rows)>limit else None}

    def persona(self,pid,history=True):
        with self.lock,self.archives._connect() as db:
            row=db.execute('SELECT * FROM personas WHERE id=?',(str(pid),)).fetchone()
            if not row:
                raise ValueError('数字分身已删除或不存在。')
            item=dict(row)
            item['coverage']=json.loads(item['coverage'])
            item['active_memories']=db.execute('SELECT COUNT(*) FROM persona_evidence WHERE persona_id=? AND active=1',(pid,)).fetchone()[0]
            turns=db.execute('SELECT * FROM persona_turns WHERE persona_id=? ORDER BY id DESC LIMIT 20',(pid,)).fetchall() if history else []
            item['turns']=[{**dict(t),'evidence':json.loads(t['evidence'])} for t in reversed(turns)]
        item['simulation']=True
        return item

    def memories(self,pid,cursor=0,limit=24):
        self.persona(pid,history=False)
        limit,offset=_limit(limit),max(0,int(cursor or 0))
        with self.lock,self.archives._connect() as db:
            rows=db.execute('SELECT payload,active FROM persona_evidence WHERE persona_id=? ORDER BY id LIMIT ? OFFSET ?',(pid,limit+1,offset)).fetchall()
        return {'items':[{**json.loads(r['payload']),'active':bool(r['active'])} for r in rows[:limit]],
                'has_more':len(rows)>limit,'next_cursor':offset+limit if len(rows)>limit else None}

    def set_memory(self,pid,mid,active):
        with self.lock,self.archives._connect() as db:
            if not db.execute('SELECT 1 FROM personas WHERE id=?',(pid,)).fetchone():
                raise ValueError('数字分身不存在。')
            row=db.execute('UPDATE persona_evidence SET active=? WHERE persona_id=? AND id=?',(int(active is True),pid,mid))
            if row.rowcount!=1:
                raise ValueError('分身记忆不存在。')
            db.execute('UPDATE personas SET revision=revision+1 WHERE id=?',(pid,))
            # Removed memories must not leak back through earlier simulated turns.
            db.execute('DELETE FROM persona_turns WHERE persona_id=?',(pid,))
        return {'success':True}

    def delete_persona(self,pid):
        with self.lock,self.archives._connect() as db:
            for table in ('persona_evidence','persona_turns'):
                db.execute(f'DELETE FROM {table} WHERE persona_id=?',(pid,))
            db.execute('DELETE FROM personas WHERE id=?',(pid,))
        return {'success':True}

    def chat_context(self,pid,text):
        text=str(text or '').strip()
        if not 1 <= len(text) <= 2000:
            raise ValueError('请输入不超过 2000 字的对话。')
        persona=self.persona(pid)
        if not persona['active_memories']:
            raise ValueError('所有来源记忆均已停用；请先恢复需要使用的记忆。')
        # Retrieve relevant originals across the complete creation-time index;
        # the initial temporal sample is a fallback for style, not the search boundary.
        where,args=self._scope(cid=persona['contact_id'],target_only=True)
        coverage=persona['coverage']
        if not coverage.get('include_groups',True):
            where += " AND s.type='private'"
        where += ' AND m.rowid<=?'
        args.append(coverage.get('message_rowid_max',0))
        tokens=re.findall(r'[a-zA-Z0-9]{3,}|[\u4e00-\u9fff]{3,}',text)
        phrases=[]
        for token in tokens:
            candidates=[token] if re.match(r'[a-zA-Z0-9]',token) else [token[i:i+3] for i in range(len(token)-2)]
            for phrase in candidates:
                if phrase not in phrases:
                    phrases.append(phrase)
        retrieved=[]
        if phrases and (coverage.get('message_rowid_max') or coverage.get('moment_rowid_max')):
            self.archives.warm_search()
            expression=' OR '.join('"'+p.replace('"','""')+'"' for p in phrases[:24])
            with self.lock,self.archives._connect() as db:
                live=db.execute('SELECT revision FROM personas WHERE id=?',(pid,)).fetchone()
                if not live or live['revision']!=persona['revision']:
                    raise ValueError('分身记忆或对话已改变，请重试。')
                deadline=time.monotonic()+3
                db.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
                try:
                    rows=db.execute(f'''SELECT m.*,s.title AS session_title,s.type AS session_type,
                        m.text AS analysis_text,'original_text' AS interpretation
                        FROM messages_fts JOIN messages m ON m.rowid=messages_fts.rowid
                        JOIN sessions s ON s.id=m.session_id WHERE messages_fts MATCH ?
                        AND {where} AND m.kind='text' AND m.side='other'
                        AND NOT EXISTS(SELECT 1 FROM persona_evidence e WHERE e.persona_id=? AND e.id=m.id AND e.active=0)
                        ORDER BY bm25(messages_fts),m.timestamp DESC,m.id LIMIT 16''',
                        (expression,*args,pid)).fetchall()
                    retrieved=[self._evidence(r) for r in rows]
                    if coverage.get('include_moments',True):
                        contact=self.contact(persona['contact_id'])
                        matches=' OR '.join('instr(text,?)>0' for _ in phrases[:24])
                        posts=db.execute(f'''SELECT payload FROM relationship_moments WHERE platform=? AND owner=? AND username=?
                            AND rowid<=? AND ({matches})
                            AND NOT EXISTS(SELECT 1 FROM persona_evidence e WHERE e.persona_id=? AND e.id=relationship_moments.id AND e.active=0)
                            ORDER BY timestamp DESC,id LIMIT 8''',
                            (contact['platform'],contact['owner'],contact['username'],coverage.get('moment_rowid_max',0),*phrases[:24],pid)).fetchall()
                        for row in posts:
                            post=json.loads(row['payload'])
                            retrieved.append({'id':post['id'],'session_id':post.get('session_id') or '',
                                'source_kind':'moment','source_title':post['author']+'的朋友圈','sender':post['username'],
                                'side':'other','timestamp':post['timestamp'],'text':post['text'][:1600], 'interpretation':'original_text'})
                except sqlite3.OperationalError:
                    raise ValueError('全历史记忆检索未能及时完成，请用更具体的词重试。') from None
                finally:
                    db.set_progress_handler(None,0)
                db.executemany('''INSERT INTO persona_evidence VALUES(?,?,?,1)
                    ON CONFLICT(persona_id,id) DO UPDATE SET payload=excluded.payload''',
                    [(pid,r['id'],json.dumps(r,ensure_ascii=False)) for r in retrieved])
        with self.lock,self.archives._connect() as db:
            live=db.execute('SELECT revision FROM personas WHERE id=?',(pid,)).fetchone()
            if not live or live['revision']!=persona['revision']:
                raise ValueError('分身记忆或对话已改变，请重试。')
            rows=db.execute('''SELECT payload FROM persona_evidence WHERE persona_id=? AND active=1
                ORDER BY rowid DESC LIMIT 240''',(pid,)).fetchall()
        records=[json.loads(r['payload']) for r in rows]
        if not records:
            raise ValueError('所有来源记忆均已停用；请先恢复需要使用的记忆。')
        terms=set(re.findall(r'[A-Za-z0-9]{2,}|[\u4e00-\u9fff]{2,}',text.lower()))
        terms.update(text[i:i+2].lower() for i in range(len(text)-1) if '\u4e00'<=text[i]<='\u9fff')
        records.sort(key=lambda r:(sum(term in r['text'].lower() for term in terms),r['timestamp'],r['id']),reverse=True)
        chosen=list({r['id']:r for r in [*retrieved,*records]}.values())[:24]
        while sum(len(r['text']) for r in chosen)>24000:
            chosen.pop()
        turns=persona['turns'][-6:]
        return {'persona':{'id':pid,'name':persona['name'],'mode':persona['mode'],'simulation':True},
                'records':chosen,'turns':[{'user':t['user_text'][:1000],'reply':t['reply'][:1000],'simulation':True} for t in turns],
                'message':text,'revision':persona['revision']}

    def append_turn(self,pid,revision,text,result):
        with self.lock,self.archives._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT revision FROM personas WHERE id=?',(pid,)).fetchone()
            if not row or row['revision']!=revision:
                raise ValueError('分身记忆或对话已改变；本次回复未保存，请重试。')
            db.execute('INSERT INTO persona_turns(persona_id,user_text,reply,evidence,created) VALUES(?,?,?,?,?)',
                (pid,text,result['reply'],json.dumps(result['evidence'],ensure_ascii=False),time.time()))
            db.execute('UPDATE personas SET revision=revision+1 WHERE id=?',(pid,))
        return {**result,'saved':True,'simulation':True}

    def sync_state(self,source_id):
        with self.lock,self.archives._connect() as db:
            row=db.execute('SELECT * FROM archive_sync WHERE source_id=?',(source_id,)).fetchone()
        return dict(row) if row else None

    def index_native_page(self,source_id,meta,owner,messages,cursor=None,complete=False):
        if not owner or meta.get('type') not in ('private','group'):
            raise ValueError('数据源未提供可靠的本人帐号或会话类型，不能跨来源关联。')
        talker=str(meta.get('talker') or source_id)
        if talker.startswith('hash:'):
            raise ValueError('会话的稳定帐号尚未解析，不能按昵称关联。')
        sid='import:'+hashlib.sha256(f'wechat|{owner}|{talker}'.encode()).hexdigest()[:24]
        with self.lock,self.archives._connect() as db:
            db.execute('''INSERT INTO sessions(id,title,talker,type,source_dir,owner_id,platform,origin,imported_at)
                VALUES(?,?,?,?,?,?,?,'wechat_db',?) ON CONFLICT(id) DO NOTHING''',
                (sid,meta.get('title') or talker,talker,meta['type'],'',owner,'wechat',time.time()))
            if meta['type']=='private':
                db.execute('INSERT INTO members VALUES(?,?,?) ON CONFLICT(session_id,platform_id) DO UPDATE SET name=excluded.name',(sid,talker,meta.get('title') or talker))
            message_ids={str(raw.get('id') or '') for raw in messages}
            if len(message_ids)>200 or '' in message_ids:
                raise ValueError('每批索引需要不超过 200 条具有稳定 ID 的消息。')
            existing={r['id'] for r in db.execute('SELECT id FROM messages WHERE session_id=? AND id IN ('+
                ','.join('?' for _ in message_ids)+')',(sid,*message_ids))} if message_ids else set()
            for raw in messages:
                mid=str(raw.get('id') or '')
                if not mid:
                    raise ValueError('数据源消息缺少稳定 ID。')
                sender=str(raw.get('sender') or '')[:240]
                if not sender and meta['type']=='private':
                    sender=owner if raw.get('side')=='me' else talker if raw.get('side')=='other' else ''
                db.execute('''INSERT INTO messages VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id,id) DO UPDATE SET
                    sender=excluded.sender,side=excluded.side,text=excluded.text,kind=excluded.kind,timestamp=excluded.timestamp''',
                    (sid,mid,sender,raw.get('side') if raw.get('side') in ('me','other') else 'unknown',str(raw.get('text') or '')[:16000],str(raw.get('kind') or 'other'),_timestamp(raw.get('timestamp')),''))
                if sender and sender!='unknown':
                    db.execute('INSERT OR IGNORE INTO members VALUES(?,?,?)',(sid,sender,str(raw.get('sender_name') or sender)[:200]))
            last=db.execute('SELECT text,kind,timestamp FROM messages WHERE session_id=? ORDER BY timestamp DESC,id DESC LIMIT 1',(sid,)).fetchone()
            if last:
                db.execute('UPDATE sessions SET count=count+?,latest=?,latest_kind=?,updated=? WHERE id=?',
                    (len(message_ids-existing),last['text'][:140],last['kind'],last['timestamp'],sid))
            db.execute('''INSERT INTO archive_sync VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                cursor=excluded.cursor,complete=excluded.complete''',(source_id,sid,json.dumps(cursor) if cursor else None,int(complete)))
        return {'archive_id':sid,'indexed':len(messages),'complete':complete}
