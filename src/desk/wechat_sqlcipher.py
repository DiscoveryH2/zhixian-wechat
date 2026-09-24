"""Read Windows WeChat databases through a local, open-source SQLCipher driver.

The raw key is borrowed from the current user's CipherTalk configuration in
memory. We never copy it into Zhixian settings or write decrypted databases.
Connections use SQLite's read-only URI mode, keep WAL visible, and reject
write queries. No CipherTalk native binaries are loaded here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .cipher_config import CipherTalkAccount, discover_ciphertalk_account


class WeChatCipherError(RuntimeError):
    """Safe diagnostic without database paths, key material, or SQL text."""


_RAW_KEY = re.compile(r"[0-9a-fA-F]{64}\Z")


def _storage_root(account: CipherTalkAccount) -> Path:
    root = account.db_root
    candidates = [root] if root.name.casefold() == "db_storage" else [root / "db_storage"]
    if root.name.casefold() != "db_storage":
        candidates.append(root / account.wxid / "db_storage")
        try:
            candidates.extend(child / "db_storage" for child in root.iterdir()
                              if child.is_dir() and child.name.casefold().startswith(account.wxid.casefold() + "_"))
        except OSError as exc:
            raise WeChatCipherError("无法检查微信数据库目录") from None
    for candidate in candidates:
        try:
            if candidate.is_dir() and not candidate.is_symlink():
                resolved = candidate.resolve(strict=True)
                if resolved.is_relative_to(root.resolve(strict=True)):
                    return resolved
        except OSError:
            continue
    raise WeChatCipherError("未找到已配置账号的微信数据库目录")


@dataclass(repr=False)
class ReadOnlyWechatCipher:
    """Factory for sqlcipher3 connections scoped to one local db_storage root."""

    root: Path
    raw_key_hex: str = field(repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root).resolve(strict=True)
        if not self.root.is_dir() or not _RAW_KEY.fullmatch(self.raw_key_hex):
            raise WeChatCipherError("微信数据库来源配置无效")

    def __call__(self, path: Path):
        try:
            import sqlcipher3
        except ImportError:
            raise WeChatCipherError("缺少 SQLCipher 数据库驱动，请重新安装知弦") from None
        try:
            target = Path(path).resolve(strict=True)
            if (not target.is_file() or target.suffix.casefold() != ".db"
                    or not target.is_relative_to(self.root)):
                raise WeChatCipherError("拒绝读取微信数据目录之外的文件")
            # Do not use immutable=1: it would hide committed WAL messages.
            con = sqlcipher3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                con.set_key(bytes.fromhex(self.raw_key_hex))
                con.row_factory = sqlcipher3.Row
                con.execute("PRAGMA query_only=ON")
                con.execute("PRAGMA temp_store=MEMORY")
                # SQLCipher reports wrong keys on first real read, not set_key.
                con.execute("SELECT count(*) FROM sqlite_master").fetchone()
                return con
            except Exception:
                con.close()
                raise
        except WeChatCipherError:
            raise
        except Exception:
            raise WeChatCipherError("无法只读打开微信数据库；请检查账号和密钥") from None


def discover_cipher_source() -> tuple[CipherTalkAccount, Path, ReadOnlyWechatCipher]:
    """Discover the already configured account; return no public key metadata."""
    account = discover_ciphertalk_account()
    root = _storage_root(account)
    return account, root, ReadOnlyWechatCipher(root, account.db_key)
