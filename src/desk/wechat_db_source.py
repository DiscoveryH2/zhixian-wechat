"""Read-only snapshot adapter for already-decrypted WeChat 4 SQLite files.

Decryption and live-change monitoring deliberately belong to other components.
This module only discovers known database files and performs bounded reads.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from core.backlog import _explicit_mention


_SHARD = re.compile(r"^message_[0-9]+\.db$", re.IGNORECASE)
_MSG_TABLE = re.compile(r"^msg_[0-9a-f]{16,64}$", re.IGNORECASE)
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_LIMIT = 500
_MAX_SESSION_LIMIT = 500
_PLACEHOLDERS = {3: ("image", "[图片]"), 34: ("voice", "[语音]"),
                 43: ("video", "[视频]"), 47: ("emoji", "[表情]"),
                 42: ("card", "[名片]"), 48: ("location", "[位置]"),
                 49: ("app", "[链接或应用消息]")}


class WeChatDBError(RuntimeError):
    """A safe, user-displayable database adapter error."""


def _quote_identifier(value: str) -> str:
    if not isinstance(value, str) or not _IDENT.fullmatch(value):
        raise WeChatDBError("拒绝使用无效的 SQLite 标识符")
    return '"' + value.replace('"', '""') + '"'


def _stable_id(*parts: Any) -> str:
    raw = "\0".join(str(part) for part in parts).encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()[:24]


def _as_text(value: Any, cap: int = 4000) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    # Avoid leaking opaque/binary media payloads into the UI or model context.
    if not isinstance(value, str):
        value = str(value)
    return "".join(ch for ch in value if ch >= " " or ch in "\n\t")[:cap]


def _decode_text_payload(value: Any) -> str | None:
    if value is None or value == "" or value == b"":
        return None
    if isinstance(value, bytes):
        if len(value) > 1024 * 1024:
            return None
        if value.startswith(b"\x28\xb5\x2f\xfd"):
            try:
                import zstandard
                value = zstandard.ZstdDecompressor().decompress(value, max_output_size=1024 * 1024)
            except Exception:
                return None
        try:
            value = value.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str) or "\ufffd" in value:
        return None
    return _as_text(value)


def _kind(local_type: Any, content: Any, compressed: Any = None) -> tuple[str, str]:
    try:
        code = int(local_type)
    except (TypeError, ValueError, OverflowError):
        code = 0
    if code in _PLACEHOLDERS:
        return _PLACEHOLDERS[code]
    if code == 1:
        text = _decode_text_payload(compressed) or _decode_text_payload(content)
        if not text:
            return "unknown", "[无法解码的文字]"
        # XML payloads (often app cards/quotes) are not presented as raw markup.
        if text.lstrip().startswith("<"):
            return "app", "[应用消息]"
        return "text", text
    return "unknown", "[非文本消息]"


class WeChatDBSource:
    """Read already-decrypted WeChat4 ``db_storage`` snapshots.

    ``self_wxid`` is optional. Direction uses an explicit ``is_send``/``isSend``
    field when present, then falls back to comparing the sender row with this ID.
    Snapshot messages are always marked historical and must never trigger sends.
    """

    def __init__(self, db_storage: str | Path, self_wxid: str | None = None,
                 self_display_name: str | None = None,
                 max_limit: int = _MAX_LIMIT,
                 connect_factory: Callable[[Path], sqlite3.Connection] | None = None):
        self.root = Path(db_storage).expanduser().resolve()
        self.self_wxid = str(self_wxid) if self_wxid else None
        self.self_display_name = str(self_display_name or "").strip()[:80]
        self.max_limit = max(1, min(int(max_limit), _MAX_LIMIT))
        self.connect_factory = connect_factory
        self.message_dbs = self._discover_message_dbs()
        self.contact_db = self._discover_contact_db()
        self.session_db = self._discover_session_db()
        self._session_cache: list[dict[str, Any]] | None = None
        self._cache_fingerprint: tuple[Any, ...] | None = None

    def _discover_message_dbs(self) -> list[Path]:
        directory = self.root / "message"
        if not directory.is_dir():
            return []
        return sorted((p for p in directory.iterdir()
                       if p.is_file() and _SHARD.fullmatch(p.name)),
                      key=lambda p: (int(p.stem.split("_")[-1]), p.name.lower()))

    def _discover_contact_db(self) -> Path | None:
        path = self.root / "contact" / "contact.db"
        return path if path.is_file() else None

    def _discover_session_db(self) -> Path | None:
        path = self.root / "session" / "session.db"
        return path if path.is_file() else None

    def _connect(self, path: Path) -> sqlite3.Connection:
        # URI quoting protects Windows paths containing spaces, #, ?, or Unicode.
        con = None
        try:
            if self.connect_factory is None:
                uri = "file:" + quote(path.resolve().as_posix(), safe="/:\\") + "?mode=ro"
                con = sqlite3.connect(uri, uri=True, timeout=1.5)
            else:
                con = self.connect_factory(path)
            con.execute("PRAGMA query_only=ON")
            return con
        except Exception:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass
            raise WeChatDBError("无法只读打开微信数据库") from None

    @staticmethod
    def _tables(con: sqlite3.Connection) -> list[str]:
        rows = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return [str(row[0]) for row in rows if _IDENT.fullmatch(str(row[0]))]

    @staticmethod
    def _columns(con: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in con.execute(
            f"PRAGMA table_info({_quote_identifier(table)})").fetchall()}

    def _contact_map(self, usernames: list[str] | None = None) -> dict[str, dict[str, str]]:
        if not self.contact_db:
            return {}
        result: dict[str, dict[str, str]] = {}
        try:
            with closing(self._connect(self.contact_db)) as con:
                for table in self._tables(con):
                    cols = self._columns(con, table)
                    user_col = next((c for c in ("user_name", "username", "wxid") if c in cols), None)
                    if not user_col:
                        continue
                    name_cols = [c for c in ("remark", "nick_name", "nickname", "display_name") if c in cols]
                    if not name_cols:
                        continue  # Name2Id is identity metadata, not a display-name source.
                    selected = [_quote_identifier(user_col)]
                    selected.append("COALESCE(" + ", ".join(
                        f"NULLIF({_quote_identifier(c)}, '')" for c in name_cols) + ", '')" if name_cols else "''")
                    where = ""
                    params: tuple[str, ...] = ()
                    if usernames is not None:
                        if not usernames:
                            continue
                        where = f" WHERE {_quote_identifier(user_col)} IN ({','.join('?' for _ in usernames)})"
                        params = tuple(usernames)
                    sql = f"SELECT {', '.join(selected)} FROM {_quote_identifier(table)}{where} LIMIT 100000"
                    for row in con.execute(sql, params):
                        uid = _as_text(row[0], 256)
                        if uid:
                            name = _as_text(row[1], 256) or uid
                            previous = result.get(uid, {}).get("name")
                            if not previous or (previous == uid and name != uid):
                                result[uid] = {"id": uid, "name": name}
        except Exception:
            return result
        return result

    def _session_tables(self) -> list[dict[str, Any]]:
        """Build and cache shard/table metadata, invalidating on DB/WAL changes."""
        self.message_dbs = self._discover_message_dbs()
        self.contact_db = self._discover_contact_db()
        fingerprint = self._storage_fingerprint()
        if self._session_cache is not None and fingerprint == self._cache_fingerprint:
            return self._session_cache

        contacts = self._contact_map()
        scanned: list[dict[str, Any]] = []
        known_wxids = set(contacts)
        # One lightweight schema pass reads Name2Id once per shard. It also
        # collects all session candidates before any message table is matched.
        for path in self.message_dbs:
            try:
                with closing(self._connect(path)) as con:
                    names = self._tables(con)
                    name2id = self._name2id(con, names)
                    known_wxids.update(value for value in name2id.values() if value)
                    valid_tables = []
                    for table in names:
                        if not _MSG_TABLE.fullmatch(table):
                            continue
                        cols = self._columns(con, table)
                        if not {"local_id", "create_time", "local_type", "message_content"}.issubset(cols):
                            continue
                        valid_tables.append((table, cols))
                    scanned.append({"db": path, "name2id": name2id, "tables": valid_tables})
            except Exception:
                continue
        if self.message_dbs and not scanned:
            raise WeChatDBError("未能读取任何微信消息分库")

        # Hash each unique candidate exactly once, then do constant-time lookups
        # for every table instead of repeated tables × contacts MD5 scans.
        hash_to_wxid: dict[str, str] = {}
        for wxid in known_wxids:
            try:
                digest = hashlib.md5(wxid.encode("utf-8")).hexdigest()
            except (AttributeError, UnicodeError):
                continue
            hash_to_wxid.setdefault(digest, wxid)
        tables: list[dict[str, Any]] = []
        for shard in scanned:
            for table, cols in shard["tables"]:
                suffix = table[4:].lower()
                session_id = hash_to_wxid.get(suffix)
                # Keep unresolved hash identifiers opaque. Name2Id is sender lookup,
                # not a guaranteed session-name map.
                sid = session_id or "wxdb:" + suffix
                tables.append({"db": shard["db"], "table": table, "session_id": sid,
                               "contact": contacts.get(session_id or "", {}),
                               "columns": cols, "name2id": shard["name2id"]})
        self._cache_fingerprint = self._storage_fingerprint()
        self._session_cache = tables
        return tables

    def _storage_fingerprint(self) -> tuple[Any, ...]:
        """Return path + mtime/size for each main DB and adjacent WAL file."""
        paths = list(self.message_dbs)
        if self.contact_db:
            paths.append(self.contact_db)
        self.session_db = self._discover_session_db()
        if self.session_db:
            paths.append(self.session_db)
        stats: list[tuple[str, int, int] | tuple[str, None, None]] = []
        for path in paths:
            for candidate in (path, Path(str(path) + "-wal")):
                try:
                    stat = candidate.stat()
                    stats.append((str(candidate), stat.st_mtime_ns, stat.st_size))
                except OSError:
                    stats.append((str(candidate), None, None))
        return tuple(stats)

    @staticmethod
    def _name2id(con: sqlite3.Connection, tables: list[str]) -> dict[int, str]:
        table = next((t for t in tables if t.lower() == "name2id"), None)
        if not table:
            return {}
        cols = WeChatDBSource._columns(con, table)
        user_col = next((c for c in ("user_name", "username", "wxid") if c in cols), None)
        if not user_col:
            return {}
        mapping: dict[int, str] = {}
        try:
            for row in con.execute(f"SELECT rowid, {_quote_identifier(user_col)} FROM {_quote_identifier(table)} LIMIT 100000"):
                try:
                    mapping[int(row[0])] = _as_text(row[1], 256)
                except (TypeError, ValueError, OverflowError):
                    continue
        except sqlite3.Error:
            return {}
        return mapping

    @staticmethod
    def _bounded_page(limit: int, offset: int) -> tuple[int, int]:
        try:
            page_size = max(1, min(int(limit), _MAX_SESSION_LIMIT))
            page_offset = max(0, int(offset))
        except (TypeError, ValueError, OverflowError):
            page_size, page_offset = 100, 0
        return page_size, page_offset

    def _session_table_rows(self, limit: int, offset: int,
                            query: str | None = None) -> list[dict[str, Any]] | None:
        """Page SessionTable metadata; None means schema unavailable, [] is valid empty."""
        self.session_db = self._discover_session_db()
        if not self.session_db:
            return None
        try:
            with closing(self._connect(self.session_db)) as con:
                table = next((t for t in self._tables(con) if t.lower() == "sessiontable"), None)
                if not table:
                    return None
                cols = self._columns(con, table)
                by_lower = {c.lower(): c for c in cols}
                user_col = next((by_lower[k] for k in ("username", "user_name", "wxid") if k in by_lower), None)
                if not user_col:
                    return None
                sort_col = next((by_lower[k] for k in ("sort_timestamp", "sort_time") if k in by_lower), None)
                stamp_col = next((by_lower[k] for k in ("last_timestamp", "timestamp", "create_time") if k in by_lower), None)
                summary_col = next((by_lower[k] for k in ("summary", "last_message", "preview") if k in by_lower), None)
                type_col = next((by_lower[k] for k in ("type", "session_type") if k in by_lower), None)
                local_id_col = next((by_lower[k] for k in ("last_msg_locald_id", "last_msg_local_id", "last_local_id") if k in by_lower), None)
                msg_type_col = next((by_lower[k] for k in ("last_msg_type", "last_type") if k in by_lower), None)
                selected = [f"{_quote_identifier(user_col)} AS username"]
                for alias, col in (("sort_timestamp", sort_col), ("last_timestamp", stamp_col),
                                   ("summary", summary_col), ("session_type", type_col),
                                   ("last_msg_local_id", local_id_col), ("last_msg_type", msg_type_col)):
                    selected.append(f"{_quote_identifier(col)} AS {_quote_identifier(alias)}" if col
                                    else f"NULL AS {_quote_identifier(alias)}")
                where = ""
                params: list[Any] = []
                if query:
                    where = f"WHERE CAST({_quote_identifier(user_col)} AS TEXT) LIKE ? "
                    params.append("%" + str(query)[:128] + "%")
                order = _quote_identifier(sort_col) if sort_col else _quote_identifier(user_col)
                sql = (f"SELECT {', '.join(selected)} FROM {_quote_identifier(table)} {where}"
                       f"ORDER BY {order} DESC, {_quote_identifier(user_col)} ASC LIMIT ? OFFSET ?")
                params.extend((limit, offset))
                aliases = ("username", "sort_timestamp", "last_timestamp", "summary",
                           "session_type", "last_msg_local_id", "last_msg_type")
                return [dict(zip(aliases, row)) for row in con.execute(sql, tuple(params))]
        except WeChatDBError:
            raise
        except Exception:
            return None

    @staticmethod
    def _session_kind(username: str, raw_type: Any) -> str:
        value = _as_text(raw_type, 32).lower()
        if value in {"group", "chatroom", "2"} or username.endswith("@chatroom"):
            return "group"
        return "private"

    def _page_from_session_table(self, limit: int, offset: int,
                                 query: str | None = None) -> list[dict[str, Any]] | None:
        rows = self._session_table_rows(limit, offset, query)
        if rows is None:
            return None
        usernames = [_as_text(row.get("username"), 256) for row in rows]
        labels = self._contact_map(usernames)
        result = []
        for row in rows:
            username = _as_text(row.get("username"), 256)
            if not username:
                continue
            contact = labels.get(username, {})
            summary = _as_text(row.get("summary"), 240)
            result.append({"id": username, "name": contact.get("name", username),
                           "type": self._session_kind(username, row.get("session_type")),
                           "preview": summary, "timestamp": _safe_int(row.get("last_timestamp")),
                           "sort_timestamp": _safe_int(row.get("sort_timestamp")),
                           "last_msg_local_id": _safe_int(row.get("last_msg_local_id")),
                           "last_msg_type": _safe_int(row.get("last_msg_type")),
                           "source": "wechat_db", "historical": True})
        return result

    def sessions(self, limit: int = 100, offset: int = 0,
                 query: str | None = None) -> list[dict[str, Any]]:
        """Return a stable page of recent sessions, preferring SessionTable metadata.

        Search currently matches session username. Contact names and summary text
        are returned as page previews but are not scanned to implement search.
        """
        limit, offset = self._bounded_page(limit, offset)
        page = self._page_from_session_table(limit, offset, query)
        if page is not None:
            if query or len(page) >= limit:
                return page
            # SessionTable omits some old/hidden conversations whose message
            # tables still exist. Append those after the indexed directory.
            indexed_ids = set()
            indexed_count = 0
            walk = 0
            while True:
                indexed = self._page_from_session_table(_MAX_SESSION_LIMIT, walk)
                if not indexed:
                    break
                indexed_ids.update(row["id"] for row in indexed)
                indexed_count += len(indexed)
                walk += len(indexed)
                if len(indexed) < _MAX_SESSION_LIMIT:
                    break
            fallback = self._fallback_sessions(exclude=indexed_ids)
            extra_offset = max(0, offset - indexed_count)
            return page + fallback[extra_offset:extra_offset + limit - len(page)]
        # Compatibility fallback for layouts without a usable session database.
        fallback = self._fallback_sessions()
        if query:
            needle = str(query)[:128].casefold()
            fallback = [row for row in fallback if needle in row["id"].casefold() or needle in row["name"].casefold()]
        return fallback[offset:offset + limit]

    def _fallback_sessions(self, exclude: set[str] | None = None) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for item in self._session_tables():
            sid = item["session_id"]
            if exclude and sid in exclude:
                continue
            row = merged.setdefault(sid, {"id": sid, "name": item["contact"].get("name", sid),
                                           "type": "group" if sid.endswith("@chatroom") else "private",
                                           "source": "wechat_db", "historical": True,
                                           "shards": []})
            row["shards"].append(item["db"].name)
        return sorted(merged.values(), key=lambda item: item["name"].casefold())

    def latest_session_heads(self, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        """Return current session head markers for polling without declaring liveness.

        This is a snapshot API. Every returned row remains ``historical=True``;
        callers must establish their own baseline before identifying new heads.
        """
        limit, offset = self._bounded_page(limit, offset)
        rows = self._session_table_rows(limit, offset)
        if rows is not None:
            heads = []
            for row in rows:
                username = _as_text(row.get("username"), 256)
                if not username:
                    continue
                heads.append({"id": username,
                              "sort_timestamp": _safe_int(row.get("sort_timestamp")),
                              "timestamp": _safe_int(row.get("last_timestamp")),
                              "last_msg_local_id": _safe_int(row.get("last_msg_local_id")),
                              "last_msg_type": _safe_int(row.get("last_msg_type")),
                              "source": "wechat_db", "historical": True})
            return heads
        # Fallback still supplies metadata only, and never claims a live event.
        return [{"id": row["id"], "sort_timestamp": 0, "timestamp": 0,
                 "last_msg_local_id": 0, "last_msg_type": 0,
                 "source": "wechat_db", "historical": True}
                for row in self.sessions(limit=limit, offset=offset)]

    def messages(self, session_id: str, limit: int = 100,
                 before: tuple[int, int, int] | None = None) -> list[dict[str, Any]]:
        """Read latest messages for a session, newest first, with stable cursor.

        ``before`` is the exclusive ``(sort_seq, create_time, local_id)`` cursor
        from the last returned message. Result rows include ``_cursor`` for paging.
        """
        if not isinstance(session_id, str) or not session_id or len(session_id) > 300:
            raise WeChatDBError("会话标识无效")
        limit = max(1, min(int(limit), self.max_limit))
        candidates = [item for item in self._session_tables() if item["session_id"] == session_id]
        rows: list[tuple[tuple[int, int, int], dict[str, Any]]] = []
        for item in candidates:
            cols = item["columns"]
            sort_col = "sort_seq" if "sort_seq" in cols else "create_time"
            fields = ["local_id", "create_time", "local_type", "message_content"]
            if "real_sender_id" in cols:
                fields.append("real_sender_id")
            if "sort_seq" in cols:
                fields.append("sort_seq")
            if "compress_content" in cols:
                fields.append("compress_content")
            send_col = next((col for col in cols if col.lower() in ("is_send", "issend")), None)
            if send_col:
                fields.append(send_col)
            where = ""
            params: tuple[int, ...] = (limit + 1,)
            if before is not None:
                sort_value, stamp_value, local_value = tuple(map(int, before))
                sort_ident = _quote_identifier(sort_col)
                time_ident = _quote_identifier("create_time")
                id_ident = _quote_identifier("local_id")
                where = (f"WHERE ({sort_ident} < ? OR ({sort_ident} = ? AND {time_ident} < ?) "
                         f"OR ({sort_ident} = ? AND {time_ident} = ? AND {id_ident} < ?)) ")
                params = (sort_value, sort_value, stamp_value, sort_value, stamp_value, local_value, limit + 1)
            sql = (f"SELECT {', '.join(_quote_identifier(c) for c in fields)} "
                   f"FROM {_quote_identifier(item['table'])} {where}"
                   f"ORDER BY {_quote_identifier(sort_col)} DESC, {_quote_identifier('create_time')} DESC, "
                   f"{_quote_identifier('local_id')} DESC LIMIT ?")
            try:
                with closing(self._connect(item["db"])) as con:
                    selected_rows = con.execute(sql, params)
                    for row in selected_rows:
                        raw = dict(zip(fields, row))
                        local_id = _safe_int(raw["local_id"])
                        stamp = _safe_int(raw["create_time"])
                        sort_seq = _safe_int(raw["sort_seq"]) if "sort_seq" in cols else stamp
                        cursor = (sort_seq, stamp, local_id)
                        sender_id = item["name2id"].get(_safe_int(raw["real_sender_id"]), "") if "real_sender_id" in cols else ""
                        kind, text = _kind(raw["local_type"], raw["message_content"],
                                           raw.get("compress_content"))
                        sent = _is_send(raw[send_col]) if send_col else None
                        if sent is None:
                            sent = True if _same_account_identity(sender_id, self.self_wxid) else (False if sender_id else None)
                        side = "me" if sent is True else "other" if sent is False else "unknown"
                        msg = {"id": _stable_id(item["db"].name, item["table"], local_id, stamp),
                               "side": side, "sender": sender_id, "text": text, "kind": kind,
                               "timestamp": stamp, "source": "wechat_db", "historical": True,
                               "_cursor": cursor}
                        if session_id.endswith("@chatroom") and kind == "text":
                            msg["directed_to_me"] = _explicit_mention(text, self.self_display_name)
                        rows.append((cursor, msg))
            except Exception as exc:
                raise WeChatDBError("读取微信消息快照失败") from exc
        rows.sort(key=lambda pair: pair[0], reverse=True)
        return [row for _, row in rows[:limit]]


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def _is_send(value: Any) -> bool | None:
    """Parse WeChat's explicit direction flag, returning None when ambiguous."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if value == 0:
            return False
        if value == 1:
            return True
        return None
    text = str(value or "").strip().lower()
    if text in {"0", "false", "no"}:
        return False
    if text in {"1", "true", "yes"}:
        return True
    return None


def _same_account_identity(sender: str, account: str | None) -> bool:
    """An account directory may append a device suffix to the login wxid."""
    if not sender or not account:
        return False
    left, right = sender.casefold(), account.casefold()
    return left == right or left.startswith(right + "_") or right.startswith(left + "_")
