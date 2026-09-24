import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.desk.cipher_config import (
    CipherTalkConfigError,
    discover_ciphertalk_account,
)


class CipherTalkConfigDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.account_root = self.base / "wechat-data"
        (self.account_root / "db_storage").mkdir(parents=True)
        self.key = "ab" * 32
        self.config = self.base / "ciphertalk-config.db"

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, values):
        with closing(sqlite3.connect(self.config)) as db:
            with db:
                db.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
                db.executemany("INSERT INTO config(key, value) VALUES (?, ?)",
                               [(key, json.dumps(value)) for key, value in values.items()])

    def account(self, ident, root=None, key=None, wxid=None):
        return {
            "id": ident,
            "dbPath": str(root or self.account_root),
            "decryptKey": key or self.key,
            "wxid": wxid or "wxid_test",
        }

    def test_finds_active_account_and_redacts_key_from_repr(self):
        active = self.account("active")
        active["displayName"] = "合成本人"
        self.write_config({
            "accounts": [self.account("older", wxid="wxid_old"), active],
            "activeAccountId": "active",
        })
        result = discover_ciphertalk_account(self.config)
        self.assertEqual(result.db_root, self.account_root.resolve())
        self.assertEqual(result.wxid, "wxid_test")
        self.assertEqual(result.db_key, self.key)
        self.assertEqual(result.display_name, "合成本人")
        self.assertNotIn(self.key, repr(result))
        self.assertNotIn("合成本人", repr(result))

    def test_accepts_ciphertalk_account_container_with_wxid_child_storage(self):
        (self.account_root / "wxid_test" / "db_storage").mkdir(parents=True)
        (self.account_root / "db_storage").rmdir()
        self.write_config({"accounts": [self.account("nested")], "activeAccountId": "nested"})
        result = discover_ciphertalk_account(self.config)
        self.assertEqual(result.db_root, self.account_root.resolve())

    def test_accepts_ciphertalk_wxid_prefixed_account_child_storage(self):
        (self.account_root / "wxid_test_suffix" / "db_storage").mkdir(parents=True)
        (self.account_root / "db_storage").rmdir()
        self.write_config({"accounts": [self.account("prefixed")], "activeAccountId": "prefixed"})
        self.assertEqual(discover_ciphertalk_account(self.config).db_root, self.account_root.resolve())

    def test_falls_back_to_first_valid_account_when_active_missing(self):
        self.write_config({"accounts": [self.account("one"), self.account("two")], "activeAccountId": "missing"})
        self.assertEqual(discover_ciphertalk_account(self.config).wxid, "wxid_test")

    def test_supports_legacy_single_account_config(self):
        self.write_config({"dbPath": str(self.account_root), "decryptKey": self.key, "myWxid": "wxid_legacy"})
        result = discover_ciphertalk_account(self.config)
        self.assertEqual(result.wxid, "wxid_legacy")
        self.assertEqual(result.db_key, self.key)

    def test_rejects_invalid_key_and_missing_database_root(self):
        self.write_config({"accounts": [self.account("bad", key="not-a-key", wxid="wxid_bad")], "activeAccountId": "bad"})
        with self.assertRaisesRegex(CipherTalkConfigError, "No active"):
            discover_ciphertalk_account(self.config)

    def test_missing_config_has_explicit_error(self):
        with self.assertRaisesRegex(CipherTalkConfigError, "not found"):
            discover_ciphertalk_account(self.base / "absent.db")

    def test_default_location_is_limited_to_appdata_cipher_talk_directory(self):
        expected = self.base / "ciphertalk" / "ciphertalk-config.db"
        expected.parent.mkdir()
        with closing(sqlite3.connect(expected)) as db:
            with db:
                db.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT)")
                db.executemany("INSERT INTO config(key, value) VALUES (?, ?)", [
                    ("accounts", json.dumps([self.account("one")])),
                    ("activeAccountId", json.dumps("one")),
                ])
        result = discover_ciphertalk_account(environ={"APPDATA": str(self.base)})
        self.assertEqual(result.db_root, self.account_root.resolve())

    def test_real_machine_config_is_discoverable_without_revealing_credentials(self):
        import os

        config = Path(os.environ.get("APPDATA", "")) / "ciphertalk" / "ciphertalk-config.db"
        if not config.is_file():
            self.skipTest("CipherTalk config is not installed on this machine")
        account = discover_ciphertalk_account()
        self.assertTrue(account.db_root.is_dir())
        self.assertEqual(len(account.db_key), 64)
        self.assertTrue(account.wxid)


if __name__ == "__main__":
    unittest.main()
