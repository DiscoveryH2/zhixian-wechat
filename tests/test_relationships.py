import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from concurrent.futures import Future
from unittest.mock import patch

from desk.imports import ChatLabImporter
from desk.relationships import RelationshipStore, identity
from desk.wechat_db_source import WeChatDBSource, WeChatDBError
from core.relationships import analyze_history, chat_persona
from core.client import ProviderError

CONFIG = {'base_url': 'http://localhost:9123', 'model_name': 'synthetic',
          'reply_base_url': 'http://localhost:9123', 'reply_model': 'synthetic-chat'}


class RelationshipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.archives = ChatLabImporter(self.root/'data')
        self.store = RelationshipStore(self.archives)
        self.private = self.archive('private', 'Synthetic same name', [('me', '我的表达'), ('friend', '旧日合成记忆：喜欢茶')]*110)
        self.group = self.archive('group', 'Synthetic group', [('friend','群里本人表达'), ('bystander','别人的经历'), ('me','群里我的话')]*60)
        self.other_owner = self.archive('private', 'Synthetic same name', [('friend','另一个帐号的记录')], owner='second-self')
        self.other_friend = self.archive('private', 'Synthetic same name', [('other-friend','同名另一个人')], friend='other-friend')
        self.store.refresh_contacts()
        self.cid = identity('wechat','me','friend')

    def tearDown(self):
        self.temp.cleanup()

    def archive(self,kind,name,messages,owner='me',friend='friend'):
        path=self.root/(str(len(list(self.root.glob('*.json'))))+'.json')
        members=[{'platformId':owner,'accountName':'Synthetic self'}, {'platformId':friend,'accountName':name}]
        if kind=='group': members.append({'platformId':'bystander','accountName':'Synthetic bystander'})
        payload={'chatlab':{'version':'0.0.2'},'meta':{'name':name,'type':kind,'platform':'wechat','ownerId':owner,'groupId':'synthetic@chatroom'},
                 'members':members,'messages':[{'sender':s,'content':text,'type':0,'timestamp':1700000000+i,'platformMessageId':str(i)} for i,(s,text) in enumerate(messages)]}
        path.write_text(json.dumps(payload),encoding='utf-8')
        return self.archives.import_files([str(path)])['items'][0]['id']

    def test_identity_links_private_and_group_without_same_name_or_account_leaks(self):
        stats=self.store.statistics(cid=self.cid)
        self.assertEqual(stats['messages'],280)
        self.assertEqual(stats['mine'],110)
        self.assertEqual({s['id'] for s in stats['sessions']},{self.private,self.group})
        corpus=self.store.corpus(cid=self.cid,maximum=300)['records']
        self.assertFalse(any('别人的经历' in r['text'] or '另一个' in r['text'] for r in corpus))
        self.assertEqual(self.store.contact_for_session(self.private)['id'],self.cid)
        contacts=self.store.contacts(query='Synthetic same name')['items']
        self.assertEqual(len(contacts),3)

    def test_full_group_statistics_and_time_strata_include_old_and_new(self):
        stats=self.store.statistics(sid=self.group)
        self.assertEqual(stats['messages'],180)
        self.assertEqual(stats['speaker_count'],3)
        sample=self.store.corpus(sid=self.private,maximum=8)
        self.assertEqual(sample['eligible_text_messages'],220)
        self.assertEqual(sample['sampled_messages'],8)
        self.assertEqual(sample['records'][0]['timestamp'],1700000000)
        self.assertEqual(sample['records'][-1]['timestamp'],1700000219)
        self.assertEqual(len(self.store.corpus(sid=self.private,maximum=1)['records']),1)

    def test_persona_only_contains_contact_authored_material_and_opt_out(self):
        self.store.save_moment({'id':'synthetic-post','username':'friend','author':'Synthetic friend','text':'朋友圈合成原文'},owner='me')
        twin=self.store.create_persona(self.cid,mode='memorial')
        memories=self.store.memories(twin['id'],limit=100)['items']
        self.assertTrue(all(r['sender']=='friend' and r['side']=='other' for r in memories))
        self.assertNotIn('我的表达',str(memories))
        self.assertNotIn('别人的经历',str(memories))
        without=self.store.create_persona(self.cid,include_groups=False,include_moments=False)
        records=self.store.memories(without['id'],limit=100)['items']
        self.assertTrue(all(r['source_kind']=='private' for r in records))
        self.assertEqual(without['active_memories'],110)
        resumed=RelationshipStore(ChatLabImporter(self.root/'data'))
        self.assertEqual(resumed.persona(twin['id'])['name'],twin['name'])
        self.assertEqual(resumed.moments(cid=self.cid)['total'],1)

    def test_removed_memory_cannot_return_from_simulated_history(self):
        twin=self.store.create_persona(self.cid)
        context=self.store.chat_context(twin['id'],'喜欢什么？')
        source=context['records'][0]
        result={'reply':'模拟回应','evidence':[source]}
        self.store.append_turn(twin['id'],context['revision'],context['message'],result)
        self.assertEqual(len(self.store.persona(twin['id'])['turns']),1)
        self.store.set_memory(twin['id'],source['id'],False)
        self.assertEqual(self.store.persona(twin['id'])['turns'],[])
        self.assertNotIn(source['id'],[r['id'] for r in self.store.chat_context(twin['id'],'喜欢什么？')['records']])
        with self.assertRaises(ValueError): self.store.append_turn(twin['id'],context['revision'],'旧请求',result)

    def test_concurrent_turn_and_deleted_persona_cannot_save_late_response(self):
        twin=self.store.create_persona(self.cid)
        context=self.store.chat_context(twin['id'],'你好')
        result={'reply':'模拟回应','evidence':context['records'][:1]}
        self.store.append_turn(twin['id'],context['revision'],'你好',result)
        with self.assertRaises(ValueError): self.store.append_turn(twin['id'],context['revision'],'并发',result)
        self.store.delete_persona(twin['id'])
        with self.assertRaises(ValueError): self.store.append_turn(twin['id'],context['revision']+1,'晚到',result)
        with self.archives._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM persona_evidence WHERE persona_id=?',(twin['id'],)).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM persona_turns WHERE persona_id=?',(twin['id'],)).fetchone()[0],0)

    def test_all_memories_disabled_blocks_generation(self):
        twin=self.store.create_persona(self.cid,include_groups=False)
        with self.archives._connect() as db: db.execute('UPDATE persona_evidence SET active=0 WHERE persona_id=?',(twin['id'],))
        with self.assertRaises(ValueError): self.store.chat_context(twin['id'],'你好')

    def test_unknown_sender_is_not_assigned_to_other_or_avatar(self):
        sid=self.archive('private','Unknown source',[('', '不确定方向'),('unknown','未知身份')],friend='unknown-friend')
        self.assertTrue(all(r['side']=='unknown' for r in self.archives.messages(sid)['items']))
        self.store.refresh_contacts()
        with self.assertRaises(ValueError): self.store.create_persona(identity('wechat','me','unknown-friend'))

    def test_moments_import_is_idempotent_scoped_and_rejects_invalid_file_atomically(self):
        path=self.root/'moments.json'
        posts=[{'id':'synthetic-sns-1','username':'friend','nickname':'Synthetic friend','contentDesc':'合成动态','createTime':1700000000}]
        path.write_text(json.dumps({'owner_id':'me','timeline':posts}),encoding='utf-8')
        self.store.import_moments([str(path)],'me')
        self.store.import_moments([str(path)],'me')
        self.assertEqual(self.store.moments(cid=self.cid)['total'],1)
        self.assertEqual(self.store.moments(sid=self.private)['total'],1)
        with self.assertRaises(ValueError): self.store.import_moments([str(path)],'second-self')
        posts.append({'id':'missing-author','contentDesc':'invalid'})
        path.write_text(json.dumps(posts),encoding='utf-8')
        with self.assertRaises(ValueError): self.store.import_moments([str(path)],'me')
        self.assertEqual(self.store.moments(cid=self.cid)['total'],1)

    def test_native_index_is_durable_resumable_and_does_not_claim_media_understanding(self):
        meta={'type':'group','title':'Synthetic native group','talker':'native@chatroom'}
        rows=[{'id':'native-1','sender':'friend','side':'other','kind':'voice','text':'[语音]','timestamp':1700000000,'_cursor':(1,2,3)}]
        first=self.store.index_native_page('source-me',meta,'me',rows,[1,2,3],False)
        self.store.index_native_page('source-me',meta,'me',rows,[1,2,3],True)
        self.assertEqual(self.archives.get_session(first['archive_id'])['count'],1)
        self.assertTrue(self.store.sync_state('source-me')['complete'])
        self.assertEqual(self.store.statistics(sid=first['archive_id'])['media_messages'],1)
        self.assertEqual(self.store.corpus(sid=first['archive_id'])['records'],[])
        self.assertFalse(self.store.sync_state('source-another-account'))
        self.assertEqual(self.store.statistics(sid=self.private)['messages'],220)

    def test_paginated_memories_and_contacts_have_no_duplicates(self):
        twin=self.store.create_persona(self.cid)
        all_ids=[]; cursor=0
        while True:
            page=self.store.memories(twin['id'],cursor,limit=31)
            all_ids.extend(r['id'] for r in page['items'])
            if not page['has_more']: break
            cursor=page['next_cursor']
        self.assertEqual(len(all_ids),twin['active_memories'])
        self.assertEqual(len(all_ids),len(set(all_ids)))
        first=self.store.contacts(limit=1); second=self.store.contacts(cursor=first['next_cursor'],limit=1)
        self.assertNotEqual(first['items'][0]['id'],second['items'][0]['id'])

    def test_media_interpretations_are_durable_labelled_and_invalidated_when_original_changes(self):
        sid=self.archive('private','Synthetic media',[('friend','[语音]')],friend='media-friend')
        with self.archives._connect() as db:
            db.execute("UPDATE messages SET kind='voice' WHERE session_id=?",(sid,))
        mid=self.archives.messages(sid)['items'][0]['id']
        self.archives.save_understanding(sid,mid,'合成转写：今天讨论方案','voice','synthetic-stt')
        resumed=ChatLabImporter(self.root/'data')
        self.assertEqual(resumed.messages(sid)['items'][0]['transcript'],'合成转写：今天讨论方案')
        stats=self.store.statistics(sid=sid)
        self.assertEqual(stats['understood_media'],1)
        self.assertEqual(stats['uninterpreted_media'],0)
        records=self.store.corpus(sid=sid)['records']
        self.assertEqual(records[0]['interpretation'],'model_media_interpretation')
        with self.archives._connect() as db:
            db.execute("UPDATE messages SET text='[语音] another-file' WHERE session_id=?",(sid,))
        self.assertEqual(self.store.corpus(sid=sid)['records'],[])
        self.assertEqual(self.store.statistics(sid=sid)['understood_media'],0)
        self.assertFalse(resumed.messages(sid)['items'][0]['transcript'])

    def test_chat_route_supports_separate_reply_key_and_does_not_forward_key_cross_origin(self):
        twin=self.store.create_persona(self.cid)
        context=self.store.chat_context(twin['id'],'你好')
        cfg={'base_url':'https://api.typesafe.ai/v1','model_name':'synthetic-judge',
             'reply_base_url':'https://reply.example.invalid/v1','reply_model':'synthetic-reply','reply_api_key':'separate-test-key'}
        def model(route,payload,**kwargs):
            self.assertEqual(route.key,'separate-test-key')
            r=context['records'][0]
            return {'choices':[{'message':{'content':json.dumps({'reply':'合成回复','evidence':[{'id':r['id'],'quote':r['text']}]})}}]}
        self.assertTrue(chat_persona(context,cfg,post_json_fn=model)['simulation'])
        cfg.pop('reply_api_key'); cfg['api_key']='synthetic-only'
        with self.assertRaises(ProviderError): chat_persona(context,cfg,post_json_fn=model)

    def test_persona_retrieves_old_unsampled_record_from_complete_identity_scoped_index(self):
        sid=self.archive('private','Synthetic deep archive',[('deep-friend','合成普通记忆') for _ in range(800)],friend='deep-friend')
        message=self.archives.messages(sid,cursor=400,limit=1)['items'][0]
        with self.archives._connect() as db:
            db.execute('UPDATE messages SET text=? WHERE session_id=? AND id=?',('合成灯塔纪念册：那次我们选了蓝色的封面',sid,message['id']))
        self.store.refresh_contacts()
        twin=self.store.create_persona(identity('wechat','me','deep-friend'))
        memories=[]; cursor=0
        while True:
            page=self.store.memories(twin['id'],cursor,100); memories.extend(page['items'])
            if not page['has_more']: break
            cursor=page['next_cursor']
        self.assertNotIn(message['id'],[r['id'] for r in memories])
        context=self.store.chat_context(twin['id'],'那本合成灯塔纪念册呢？')
        self.assertIn(message['id'],[r['id'] for r in context['records']])
        self.store.set_memory(twin['id'],message['id'],False)
        again=self.store.chat_context(twin['id'],'合成灯塔纪念册')
        self.assertNotIn(message['id'],[r['id'] for r in again['records']])

    def test_deletion_during_index_warmup_cannot_leave_orphaned_memories(self):
        twin=self.store.create_persona(self.cid)
        with patch.object(self.archives,'warm_search',side_effect=lambda:self.store.delete_persona(twin['id'])):
            with self.assertRaises(ValueError): self.store.chat_context(twin['id'],'合成记忆')
        with self.archives._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM persona_evidence WHERE persona_id=?',(twin['id'],)).fetchone()[0],0)

    def test_schema_upgrade_keeps_v17_history_and_backfills_stable_senders(self):
        legacy=self.root/'legacy'; legacy.mkdir()
        with sqlite3.connect(legacy/'chatlab-index.sqlite3') as db:
            db.executescript('''CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT NOT NULL,talker TEXT,type TEXT,source_dir TEXT NOT NULL,latest TEXT,latest_kind TEXT,updated REAL NOT NULL DEFAULT 0,count INTEGER NOT NULL DEFAULT 0,owner_id TEXT NOT NULL DEFAULT '',imported_at REAL NOT NULL DEFAULT 0);
                CREATE TABLE messages(session_id TEXT NOT NULL,id TEXT NOT NULL,sender TEXT,side TEXT NOT NULL,text TEXT NOT NULL,kind TEXT NOT NULL,timestamp REAL,media_rel TEXT NOT NULL DEFAULT '',PRIMARY KEY(session_id,id));''')
            db.execute("INSERT INTO sessions VALUES('legacy','Synthetic legacy','friend','private','','old','text',1,1,'me',1)")
            db.execute("INSERT INTO messages VALUES('legacy','legacy-msg','friend','other','old','text',1,'')")
        migrated=ChatLabImporter(legacy); store=RelationshipStore(migrated)
        self.assertEqual(store.contact_for_session('legacy')['username'],'friend')
        self.assertEqual(store.statistics(sid='legacy')['messages'],1)

    def test_overlong_or_structured_sender_ids_are_rejected_without_identity_truncation(self):
        count=self.archives.count()
        with self.assertRaises(ValueError): self.archive('private','Invalid',[('x'*241+'a','invalid')],friend='x'*241+'a')
        with self.assertRaises(ValueError): self.archive('private','Invalid',[({'username':'friend'},'invalid')])
        self.assertEqual(self.archives.count(),count)

    def test_model_citations_are_verified_and_payload_is_bounded(self):
        stats=self.store.statistics(cid=self.cid); corpus=self.store.corpus(cid=self.cid)
        def model(route,payload,**kwargs):
            state=json.loads(payload['messages'][-1]['content'])
            record=state['records'][0]
            return {'choices':[{'message':{'content':json.dumps({'insights':[{'title':'合成观察','observation':'原文待核对','action':'询问下一步','evidence':[{'id':record['id'],'quote':record['text']}]}]})}}]}
        result=analyze_history(stats,corpus,CONFIG,post_json_fn=model)
        self.assertFalse(result['automatic_actions'])
        self.assertEqual(len(result['insights']),1)
        def forged(*args,**kwargs): return {'choices':[{'message':{'content':json.dumps({'insights':[{'title':'伪造','observation':'伪造','action':'伪造','evidence':[{'id':corpus['records'][0]['id'],'quote':'没有这条原文'}]}]})}}]}
        with self.assertRaises(ProviderError): analyze_history(stats,corpus,CONFIG,post_json_fn=forged)

    def test_chat_is_labelled_and_generated_turns_are_not_source_records(self):
        twin=self.store.create_persona(self.cid)
        context=self.store.chat_context(twin['id'],'你好')
        def model(route,payload,**kwargs):
            state=json.loads(payload['messages'][-1]['content']); r=state['records'][0]
            self.assertTrue(state['persona']['simulation'])
            self.assertLess(len(payload['messages'][-1]['content']),70000)
            return {'choices':[{'message':{'content':json.dumps({'reply':'合成模拟回应','evidence':[{'id':r['id'],'quote':r['text']} ]})}}]}
        result=chat_persona(context,CONFIG,post_json_fn=model)
        self.assertTrue(result['simulation'])
        self.store.append_turn(twin['id'],context['revision'],'你好',result)
        second=self.store.chat_context(twin['id'],'再聊聊')
        self.assertEqual(second['turns'][0]['reply'],'合成模拟回应')
        self.assertNotIn('合成模拟回应',[r['text'] for r in second['records']])
        def forged(*args,**kwargs): return {'choices':[{'message':{'content':'{"reply":"伪造","evidence":[{"id":"unknown","quote":"伪造"}]}'}}]}
        with self.assertRaises(ProviderError): chat_persona(context,CONFIG,post_json_fn=forged)


class TimelineTests(unittest.TestCase):
    def test_readonly_sns_parses_cdata_rejects_entities_and_pages(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'sns').mkdir()
            path=root/'sns/sns.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE SnsTimeLine(tid INTEGER,user_name TEXT,content TEXT)')
                db.executemany('INSERT INTO SnsTimeLine VALUES(?,?,?)',[(3,'friend','<TimelineObject><createTime>1700000000</createTime><contentDesc><![CDATA[合成动态 < & >]]></contentDesc></TimelineObject>'),(2,'friend','<!DOCTYPE x [<!ENTITY z "bad">]><x><contentDesc>&z;</contentDesc></x>'),(1,'other','<TimelineObject><contentDesc>另一条</contentDesc></TimelineObject>')])
            original=path.read_bytes()
            reader=WeChatDBSource(root,self_wxid='me')
            page=reader.moments(limit=2)
            self.assertEqual(page['items'][0]['text'],'合成动态 < & >')
            self.assertEqual(page['unreadable'],1)
            self.assertEqual(page['next_cursor'],2)
            self.assertEqual(reader.moments(limit=2,offset=2)['items'][0]['username'],'other')
            self.assertEqual(path.read_bytes(),original)

    def test_no_sns_database_is_an_explicit_error(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(WeChatDBError): WeChatDBSource(Path(temp),self_wxid='me').moments()


class ControllerRelationshipTests(unittest.TestCase):
    def test_rpc_model_jobs_persist_and_restart_and_do_not_dispatch_wechat(self):
        from PySide6.QtWidgets import QApplication
        from desk.controller import Controller
        app=QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as temp, patch('desk.capture_service.CaptureService') as capture:
            controller=Controller(Path(temp)/'data')
            path=Path(temp)/'archive.json'
            path.write_text(json.dumps({'chatlab':{'version':'0.0.2'},'meta':{'name':'Synthetic friend','type':'private','ownerId':'me'},'members':[{'platformId':'me'},{'platformId':'friend'}],'messages':[{'sender':'friend','type':0,'content':'合成原始话语','timestamp':1700000000,'platformMessageId':'1'}]}),encoding='utf-8')
            controller.imports.import_files([str(path)])
            cid=controller.handle('list_relationship_contacts',{}).result(timeout=3)['items'][0]['id']
            twin=controller.handle('create_persona',{'contact_id':cid}).result(timeout=3)
            source=controller.relationships.chat_context(twin['id'],'你好')['records'][0]
            future=Future(); future.set_result({'reply':'模拟回应','evidence':[source],'simulation':True})
            with patch.object(controller.models,'submit',return_value=future) as submit:
                reply=controller.handle('chat_persona',{'id':twin['id'],'text':'你好'}).result(timeout=3)
                self.assertEqual(submit.call_args.args[0],'chat_persona')
                self.assertTrue(reply['saved'])
            item=controller.handle('manual_moment',{'author':'Synthetic friend','text':'合成动态','session_id':controller.imports.list_sessions()['items'][0]['id']})['item']
            controller.close()
            next_controller=Controller(Path(temp)/'data')
            try:
                self.assertEqual(len(next_controller.handle('get_persona',{'id':twin['id']}).result(timeout=3)['turns']),1)
                self.assertEqual(next_controller.handle('list_moments',{}).result(timeout=3)['items'][0]['id'],item['id'])
                self.assertFalse(capture.return_value.send.called)
                self.assertFalse(capture.return_value.fill.called)
            finally: next_controller.close()
