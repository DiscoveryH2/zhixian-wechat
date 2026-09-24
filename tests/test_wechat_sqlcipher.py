import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from desk.wechat_sqlcipher import ReadOnlyWechatCipher, WeChatCipherError


class ReadOnlyWechatCipherTests(unittest.TestCase):
    def test_readonly_connection_sees_committed_wal_updates(self):
        try:
            import sqlcipher3
        except ImportError:
            self.skipTest("sqlcipher3 is not installed")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "live.db"
            key = "cd" * 32
            with closing(sqlcipher3.connect(path)) as writer:
                writer.set_key(bytes.fromhex(key))
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE synthetic(n INTEGER)")
                writer.execute("INSERT INTO synthetic VALUES (1)")
                writer.commit()
                source = ReadOnlyWechatCipher(root, key)
                with closing(source(path)) as reader:
                    self.assertEqual(reader.execute("SELECT count(*) FROM synthetic").fetchone()[0], 1)
                    writer.execute("INSERT INTO synthetic VALUES (2)")
                    writer.commit()
                    self.assertEqual(reader.execute("SELECT count(*) FROM synthetic").fetchone()[0], 2)

    def test_encrypted_database_reads_and_denies_writes(self):
        try:
            import sqlcipher3
        except ImportError:
            self.skipTest("sqlcipher3 is not installed")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "message.db"
            key = "ab" * 32
            with closing(sqlcipher3.connect(path)) as writer:
                writer.set_key(bytes.fromhex(key))
                writer.execute("CREATE TABLE synthetic(id INTEGER PRIMARY KEY, text TEXT)")
                writer.execute("INSERT INTO synthetic(text) VALUES ('fixture')")
                writer.commit()
            source = ReadOnlyWechatCipher(root, key)
            with closing(source(path)) as reader:
                self.assertEqual(reader.execute("SELECT count(*) FROM synthetic").fetchone()[0], 1)
                with self.assertRaises(sqlcipher3.OperationalError):
                    reader.execute("INSERT INTO synthetic(text) VALUES ('blocked')")
            with self.assertRaises(WeChatCipherError):
                source(root.parent / "elsewhere.db")

    def test_rejects_bad_key_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(WeChatCipherError) as caught:
                ReadOnlyWechatCipher(Path(temp), "x" * 64)
            self.assertNotIn("x" * 64, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
