"""Read-only discovery of CipherTalk's locally configured WeChat account.

This module only reads CipherTalk's small SQLite config database. It never
opens a WeChat database or invokes CipherTalk's native components.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


class CipherTalkConfigError(RuntimeError):
    """Configuration is missing, unsafe, or does not contain a usable account."""


@dataclass(frozen=True, repr=False)
class CipherTalkAccount:
    """Minimal credentials needed by a separate database adapter.

    ``db_key`` is deliberately excluded from repr to reduce accidental leaks.
    Callers must keep this object short-lived and must not serialize it.
    """

    db_root: Path
    wxid: str
    db_key: str = field(repr=False)
    display_name: str = field(default="", repr=False)


_KEY_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_WXID_RE = re.compile(r"^[A-Za-z0-9_@.\-]{1,256}$")
_MAX_CONFIG_BYTES = 16 * 1024 * 1024
_MAX_VALUE_BYTES = 8 * 1024 * 1024


def _config_path(environ: Mapping[str, str], explicit: str | os.PathLike[str] | None) -> Path:
    if explicit is not None:
        path = Path(explicit).expanduser()
    else:
        appdata = environ.get("APPDATA")
        if not appdata:
            raise CipherTalkConfigError("APPDATA is unavailable; CipherTalk config discovery is Windows-only by default")
        path = Path(appdata) / "ciphertalk" / "ciphertalk-config.db"
    if not path.is_absolute():
        raise CipherTalkConfigError("CipherTalk config path must be absolute")
    if ".." in path.parts:
        raise CipherTalkConfigError("CipherTalk config path may not contain parent traversal")
    _reject_symlink_components(path)
    return path


def _reject_symlink_components(path: Path) -> None:
    """Reject existing symlink components to avoid redirecting discovery."""
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise CipherTalkConfigError("CipherTalk paths may not traverse symbolic links")
        except FileNotFoundError:
            # Later components cannot exist if an ancestor is missing.
            break
        except OSError as exc:
            raise CipherTalkConfigError("CipherTalk path components cannot be inspected") from exc


def _read_config(path: Path) -> dict[str, str]:
    try:
        st = path.lstat()
    except FileNotFoundError as exc:
        raise CipherTalkConfigError("CipherTalk config database was not found") from exc
    except OSError as exc:
        raise CipherTalkConfigError("CipherTalk config database cannot be inspected") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise CipherTalkConfigError("CipherTalk config path must be a regular, non-symlink file")
    if st.st_size <= 0 or st.st_size > _MAX_CONFIG_BYTES:
        raise CipherTalkConfigError("CipherTalk config database has an invalid size")

    # Read-only mode keeps current committed WAL configuration visible. Using
    # immutable=1 here could silently select an old active account or key.
    uri = path.resolve(strict=True).as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only = ON")
            schema = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='config'").fetchone()
            if not schema:
                raise CipherTalkConfigError("CipherTalk config database has no config table")
            columns = {row[1] for row in db.execute('PRAGMA table_info("config")')}
            if not {"key", "value"}.issubset(columns):
                raise CipherTalkConfigError("CipherTalk config table has an unsupported schema")
            rows = db.execute("SELECT key, value FROM config WHERE key IN (?, ?, ?, ?, ?)",
                              ("accounts", "activeAccountId", "dbPath", "decryptKey", "myWxid")).fetchall()
    except CipherTalkConfigError:
        raise
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise CipherTalkConfigError("CipherTalk config database could not be read safely") from exc

    result: dict[str, str] = {}
    for key, value in rows:
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if len(value.encode("utf-8", errors="replace")) > _MAX_VALUE_BYTES:
            raise CipherTalkConfigError("CipherTalk config contains an oversized value")
        result[key] = value
    return result


def _json_value(raw: str | None, label: str):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CipherTalkConfigError(f"CipherTalk {label} config is malformed") from exc


def _usable_account(record: object) -> tuple[Path, str, str, str] | None:
    if not isinstance(record, dict):
        return None
    root_raw = record.get("dbPath")
    wxid = record.get("wxid")
    key = record.get("decryptKey")
    if not all(isinstance(v, str) and v.strip() for v in (root_raw, wxid, key)):
        return None
    key = key.strip()
    if not _KEY_RE.fullmatch(key):
        return None
    root = Path(root_raw.strip()).expanduser()
    if not root.is_absolute() or ".." in root.parts:
        return None
    try:
        _reject_symlink_components(root)
        resolved = root.resolve(strict=True)
        if not resolved.is_dir():
            return None
        # CipherTalk's dbPath can be the account container: its WCDB resolver
        # checks db_storage, <wxid>/db_storage, and a wxid-prefixed child.
        # The local account currently uses the second layout.
        wxid = wxid.strip()
        candidates = [resolved] if resolved.name.casefold() == "db_storage" else [resolved / "db_storage"]
        if resolved.name.casefold() != "db_storage":
            candidates.append(resolved / wxid / "db_storage")
            try:
                for entry in resolved.iterdir():
                    if (entry.is_dir() and entry.name.casefold().startswith(wxid.casefold() + "_")
                            and len(entry.name) <= 512):
                        candidates.append(entry / "db_storage")
            except OSError:
                return None
        storage = next((candidate for candidate in candidates if candidate.is_dir()), None)
        if storage is None:
            return None
        _reject_symlink_components(storage)
        if storage.is_symlink():
            return None
    except OSError:
        return None
    if not _WXID_RE.fullmatch(wxid):
        return None
    label = record.get("displayName")
    display_name = label.strip()[:80] if isinstance(label, str) and not any(ord(ch) < 32 for ch in label) else ""
    return resolved, wxid, key, display_name


def discover_ciphertalk_account(
    config_path: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> CipherTalkAccount:
    """Return the active usable CipherTalk account using read-only local config.

    By default only ``%APPDATA%/ciphertalk/ciphertalk-config.db`` is inspected.
    ``config_path`` exists for controlled tests and explicit local diagnostics.
    The returned key is held only in this in-memory object.
    """
    env = os.environ if environ is None else environ
    path = _config_path(env, config_path)
    values = _read_config(path)

    accounts = _json_value(values.get("accounts"), "accounts")
    active_id = _json_value(values.get("activeAccountId"), "active account id")
    if isinstance(accounts, list):
        active = next((item for item in accounts if isinstance(item, dict) and item.get("id") == active_id), None)
        ordered = ([active] if active is not None else []) + [item for item in accounts if item is not active]
        for item in ordered:
            usable = _usable_account(item)
            if usable:
                root, wxid, key, display_name = usable
                return CipherTalkAccount(root, wxid, key, display_name)

    # Older CipherTalk builds stored a single account in top-level config keys.
    legacy = {
        "dbPath": _json_value(values.get("dbPath"), "database path"),
        "decryptKey": _json_value(values.get("decryptKey"), "database key"),
        "wxid": _json_value(values.get("myWxid"), "account id"),
    }
    usable = _usable_account(legacy)
    if usable:
        root, wxid, key, display_name = usable
        return CipherTalkAccount(root, wxid, key, display_name)
    raise CipherTalkConfigError("No active CipherTalk account has a valid key and existing database root")
