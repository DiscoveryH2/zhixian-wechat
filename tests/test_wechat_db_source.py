import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from desk.wechat_db_source import WeChatDBSource, WeChatDBError, _kind


def _msg_table(session):
    return "msg_" + hashlib.md5(session.encode("utf-8")).hexdigest()


def _create_shard(root, index, conversations):
    directory = root / "message"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"message_{index}.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE Name2Id (user_name TEXT)")
    con.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (1, 'wxid_self')")
    con.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (2, 'wxid_friend')")
    con.execute("INSERT INTO Name2Id(rowid, user_name) VALUES (3, 'wxid_member')")
    for session, messages in conversations.items():
        table = _msg_table(session)
        con.execute(f'CREATE TABLE "{table}" (local_id INTEGER, server_id INTEGER, sort_seq INTEGER, '
                    "create_time INTEGER, local_type INTEGER, real_sender_id INTEGER, message_content TEXT, is_send INTEGER)")
        con.executemany(f'INSERT INTO "{table}" VALUES (?, ?, ?, ?, ?, ?, ?, ?)', messages)
    con.commit()
    con.close()


def _snapshot(root):
    root = Path(root) / "db_storage"
    contact = root / "contact"
    contact.mkdir(parents=True)
    con = sqlite3.connect(contact / "contact.db")
    con.execute("CREATE TABLE contact (user_name TEXT, nick_name TEXT, remark TEXT)")
    con.executemany("INSERT INTO contact VALUES (?, ?, ?)", [
        ("wxid_friend", "羽", "王松羽"), ("room@chatroom", "龙虎豹", "")])
    con.execute("CREATE TABLE name2id(user_name TEXT)")
    con.execute("INSERT INTO name2id(user_name) VALUES ('wxid_friend')")
    con.commit()
    con.close()
    _create_shard(root, 0, {
        "wxid_friend": [
            (1, 101, 100, 1000, 1, 2, "你好", 0),
            (2, 102, 200, 2000, 3, 2, "<opaque image data>", 0),
        ],
        "room@chatroom": [(1, 201, 150, 1500, 34, 3, "voice payload", 0)],
    })
    _create_shard(root, 2, {
        # is_send wins over the deliberately misleading real_sender_id=2.
        "wxid_friend": [(3, 103, 300, 3000, 1, 2, "回头见", 1)],
        "room@chatroom": [(2, 202, 250, 2500, 1, 2, "群里问题", 0)],
    })
    return root


def _add_session_index(root):
    directory = Path(root) / "session"
    directory.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(directory / "session.db")
    con.execute("CREATE TABLE SessionTable (username TEXT, type INTEGER, summary TEXT, "
                "last_timestamp INTEGER, sort_timestamp INTEGER, last_msg_locald_id INTEGER, last_msg_type INTEGER)")
    con.executemany("INSERT INTO SessionTable VALUES (?, ?, ?, ?, ?, ?, ?)", [
        ("wxid_friend", 1, "合成私聊预览", 1700, 300, 12, 1),
        ("room@chatroom", 2, "合成群聊预览", 1800, 500, 42, 34),
        ("wxid_other", 1, "另一条合成预览", 1600, 400, 21, 3),
    ])
    con.commit()
    con.close()


class WeChatDBSourceTests(unittest.TestCase):
    def test_compressed_text_is_decoded_and_invalid_bytes_are_not_replyable(self):
        import zstandard
        compressed = zstandard.ZstdCompressor().compress("合成可回复文字".encode("utf-8"))
        self.assertEqual(_kind(1, b"\xff\xfe", compressed), ("text", "合成可回复文字"))
        self.assertEqual(_kind(1, b"\xff\xfe"), ("unknown", "[无法解码的文字]"))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.snapshot = _snapshot(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_discovery_and_sessions_cover_private_and_group_across_shards(self):
        source = WeChatDBSource(self.snapshot, self_wxid="wxid_self")
        self.assertEqual([p.name for p in source.message_dbs], ["message_0.db", "message_2.db"])
        sessions = {row["id"]: row for row in source.sessions()}
        self.assertEqual(sessions["wxid_friend"]["name"], "王松羽")
        self.assertEqual(sessions["wxid_friend"]["type"], "private")
        self.assertEqual(sessions["room@chatroom"]["name"], "龙虎豹")
        self.assertEqual(sessions["room@chatroom"]["type"], "group")
        self.assertTrue(sessions["room@chatroom"]["historical"])

    def test_messages_merge_shards_stably_and_normalize_media(self):
        source = WeChatDBSource(self.snapshot, self_wxid="wxid_self")
        messages = source.messages("wxid_friend", limit=10)
        self.assertEqual([m["text"] for m in messages], ["回头见", "[图片]", "你好"])
        self.assertEqual([m["kind"] for m in messages], ["text", "image", "text"])
        self.assertEqual(messages[0]["side"], "me")
        self.assertEqual(messages[1]["side"], "other")
        self.assertTrue(all(m["source"] == "wechat_db" and m["historical"] for m in messages))
        group = source.messages("room@chatroom")
        self.assertEqual(group[0]["text"], "群里问题")
        self.assertEqual((group[1]["kind"], group[1]["text"]), ("voice", "[语音]"))

    def test_sender_suffix_identifies_self_and_missing_sender_fails_closed(self):
        path = self.snapshot / "message" / "message_4.db"
        with closing(sqlite3.connect(path)) as con:
            con.execute("CREATE TABLE Name2Id(user_name TEXT)")
            con.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (1,'wxid_self_device')")
            con.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (2,'wxid_friend')")
            table = _msg_table("wxid_friend")
            con.execute(f'CREATE TABLE "{table}"(local_id INTEGER,sort_seq INTEGER,create_time INTEGER,'
                        'local_type INTEGER,real_sender_id INTEGER,message_content TEXT)')
            con.execute(f'INSERT INTO "{table}" VALUES (10,400,4000,1,1,\'合成发出\')')
            con.execute(f'INSERT INTO "{table}" VALUES (11,500,5000,1,99,\'无法确认方向\')')
            con.commit()
        rows = WeChatDBSource(self.snapshot, self_wxid="wxid_self").messages("wxid_friend", limit=10)
        sides = {row["text"]: row["side"] for row in rows}
        self.assertEqual(sides["合成发出"], "me")
        self.assertEqual(sides["无法确认方向"], "unknown")

    def test_paging_cursor_is_exclusive_and_limit_is_bounded(self):
        source = WeChatDBSource(self.snapshot, max_limit=2)
        first = source.messages("wxid_friend", limit=999)
        self.assertEqual(len(first), 2)
        second = source.messages("wxid_friend", limit=2, before=first[-1]["_cursor"])
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["text"], "你好")

    def test_database_is_query_only_and_session_id_is_not_sql(self):
        source = WeChatDBSource(self.snapshot)
        self.assertEqual(source.messages("wxid_friend'; DROP TABLE Name2Id;--"), [])
        with closing(source._connect(source.message_dbs[0])) as con:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("DELETE FROM Name2Id")
        with closing(sqlite3.connect(source.message_dbs[0])) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM Name2Id").fetchone()[0], 3)

    def test_custom_connect_factory_is_used_for_each_database(self):
        opened = []

        class NoRowFactoryConnection:
            """Mimic SQLCipher DB-API where stdlib sqlite3.Row is incompatible."""
            def __init__(self, connection):
                object.__setattr__(self, "_connection", connection)

            @property
            def row_factory(self):
                raise AttributeError("provider does not expose sqlite3 row_factory")

            @row_factory.setter
            def row_factory(self, _value):
                raise TypeError("incompatible row_factory")

            def __getattr__(self, name):
                if name == "row_factory":
                    raise AttributeError(name)
                return getattr(self._connection, name)

        def factory(path):
            opened.append(path.name)
            uri = "file:" + path.as_posix() + "?mode=ro"
            return NoRowFactoryConnection(sqlite3.connect(uri, uri=True))

        source = WeChatDBSource(self.snapshot, connect_factory=factory)
        source.sessions()
        self.assertIn("contact.db", opened)
        self.assertIn("message_0.db", opened)
        self.assertIn("message_2.db", opened)

    def test_broken_cipher_source_is_reported_unavailable(self):
        _add_session_index(self.snapshot)
        def broken(_path):
            raise RuntimeError("private path and key details must not escape")
        source = WeChatDBSource(self.snapshot, connect_factory=broken)
        with self.assertRaises(WeChatDBError) as caught:
            source.sessions(limit=5)
        self.assertNotIn("private path", str(caught.exception))

    def test_session_discovery_cache_and_database_invalidation(self):
        source = WeChatDBSource(self.snapshot)
        first = source._session_tables()
        self.assertIs(source._session_tables(), first)
        _create_shard(self.snapshot, 3, {
            "wxid_new_contact": [(1, 303, 400, 4000, 1, 2, "合成", 0)],
        })
        third = source._session_tables()
        self.assertIsNot(third, first)
        self.assertIn("message_3.db", {item["db"].name for item in third})

    def test_session_table_directory_is_paged_sorted_and_labeled(self):
        _add_session_index(self.snapshot)
        _create_shard(self.snapshot, 4, {"orphan_contact": [(1, 501, 900, 9000, 1, 2, "归档合成消息", 0)]})
        source = WeChatDBSource(self.snapshot)
        first = source.sessions(limit=1)
        second = source.sessions(limit=1, offset=1)
        self.assertEqual(first[0]["id"], "room@chatroom")
        self.assertEqual(first[0]["name"], "龙虎豹")
        self.assertEqual(first[0]["preview"], "合成群聊预览")
        self.assertEqual(first[0]["type"], "group")
        self.assertEqual(second[0]["id"], "wxid_other")
        self.assertEqual(source.sessions(limit=10, query="friend")[0]["id"], "wxid_friend")
        historical_only = source.sessions(limit=10, offset=3)
        self.assertEqual(len(historical_only), 1)
        self.assertTrue(historical_only[0]["id"].startswith("wxdb:"))

    def test_session_heads_are_snapshot_baseline_not_live_events(self):
        _add_session_index(self.snapshot)
        source = WeChatDBSource(self.snapshot)
        heads = source.latest_session_heads(limit=2)
        self.assertEqual([row["id"] for row in heads], ["room@chatroom", "wxid_other"])
        self.assertEqual(heads[0]["last_msg_local_id"], 42)
        self.assertTrue(all(row["historical"] for row in heads))
        self.assertTrue(all("preview" not in row for row in heads))


if __name__ == "__main__":
    unittest.main()
