"""Pure policy and rate-limit guard for opt-in, reply-only automation.

This module deliberately has no WeChat, network, filesystem, or sender I/O.
Callers provide normalized event dictionaries (or objects with equivalent
attributes) and persist ``snapshot()`` themselves if desired.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Mapping


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str
    ready_at: float | None = None


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _epoch(value: Any, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _now_epoch(value: Any = None) -> float:
    return _epoch(value, datetime.now(timezone.utc).timestamp())


def _truthy_flag(obj: Any, *names: str) -> bool:
    return any(_get(obj, name, default=False) is True for name in names)


class AutoReplyGuard:
    """Evaluate eligibility and atomically claim permitted reply sends."""

    STATE_VERSION = 1
    MAX_DEDUPE = 4096

    def __init__(self, state: Mapping[str, Any] | None = None) -> None:
        self._seen: deque[str] = deque(maxlen=self.MAX_DEDUPE)
        self._seen_set: set[str] = set()
        self._pending: dict[str, dict[str, Any]] = {}
        self._sent: deque[dict[str, Any]] = deque(maxlen=10000)
        self._last_by_session: dict[str, float] = {}
        if state:
            self.restore(state)

    @staticmethod
    def _session_id(session: Any) -> str:
        value = _get(session, "id", "session_id", "username", "wxid", default=None)
        return str(value) if value is not None else ""

    @staticmethod
    def _session_kind(session: Any, message: Any, event: Any) -> str:
        raw = _get(session, "kind", "type", "session_type", default=None)
        if raw is None:
            raw = _get(message, "session_type", "chat_type", default=None)
        if raw is None:
            raw = _get(event, "session_type", "chat_type", default=None)
        if not isinstance(raw, str):
            return "unknown"
        normalized = raw.strip().lower()
        if normalized in {"group", "群", "群聊", "chatroom"}:
            return "group"
        if normalized in {"contact", "friend", "private", "direct", "联系人", "私聊"}:
            return "contact"
        return "unknown"

    @staticmethod
    def _message_key(session_id: str, message: Any, event: Any) -> str:
        mid = _get(message, "id", "message_id", "msg_id", default=None)
        eid = _get(event, "id", "event_id", "sequence", default=None)
        # At least one stable identifier is required; content is never used as identity.
        identity = f"m:{mid}" if mid is not None else (f"e:{eid}" if eid is not None else "")
        return f"{session_id}|{identity}" if identity else ""

    def _remember(self, key: str) -> None:
        if key in self._seen_set:
            return
        if len(self._seen) == self._seen.maxlen:
            self._seen_set.discard(self._seen[0])
        self._seen.append(key)
        self._seen_set.add(key)

    def _eligibility(self, session: Any, message: Any, event: Any,
                     config: Any, policy: Mapping[str, Any], now: float) -> tuple[Decision | None, str, str]:
        if not bool(policy.get("enabled", False)) or _get(config, "auto_reply_enabled", default=True) is False:
            return Decision(False, "disabled"), "", ""
        sid = self._session_id(session)
        allowed_sessions = policy.get("session_ids", ())
        if not sid or not isinstance(allowed_sessions, (list, tuple, set, frozenset)) or sid not in {str(x) for x in allowed_sessions}:
            return Decision(False, "session_not_opted_in"), sid, ""
        kind = self._session_kind(session, message, event)
        if kind == "unknown":
            return Decision(False, "unknown_session_type"), sid, ""
        if _truthy_flag(event, "historical", "is_historical", "history", "imported", "from_import", "ocr_history") or _truthy_flag(message, "historical", "is_historical", "history", "imported", "from_import", "ocr_history"):
            return Decision(False, "historical_event"), sid, ""
        source = str(_get(event, "source", default="") or _get(message, "source", default="")).lower()
        if source in {"import", "imported", "ocr_history", "history", "backfill"}:
            return Decision(False, "non_live_source"), sid, ""
        if source == "ocr":
            # OCR is live only when capture explicitly identifies a visible,
            # fresh turn and the parser supplied a durable message identity.
            if (_get(event, "live_visible", default=None) is not True
                    or _get(message, "historical", default=None) is not False
                    or _get(event, "historical", default=None) is not False
                    or _get(message, "id", default=None) is None):
                return Decision(False, "non_live_source"), sid, ""
        if _truthy_flag(event, "self", "is_self", "from_self", "outgoing") or _truthy_flag(message, "self", "is_self", "from_self", "outgoing"):
            return Decision(False, "self_message"), sid, ""
        if _truthy_flag(event, "old", "is_old", "stale") or _truthy_flag(message, "old", "is_old", "stale"):
            return Decision(False, "old_event"), sid, ""
        age = _get(event, "age_seconds", default=_get(message, "age_seconds", default=None))
        if age is not None and _epoch(age, 0) > 300:
            return Decision(False, "old_event"), sid, ""
        ts = _get(event, "timestamp", "created_at", default=_get(message, "timestamp", "created_at", default=None))
        if ts is not None and now - _epoch(ts, now) > 300:
            return Decision(False, "old_event"), sid, ""
        if not (_truthy_flag(event, "incoming", "is_incoming") or _truthy_flag(message, "incoming", "is_incoming") or
                (_get(event, "direction", default=None) == "incoming") or (_get(message, "direction", default=None) == "incoming")):
            return Decision(False, "not_incoming"), sid, ""
        if kind == "group":
            mode = policy.get("group_mode", "mention_only")
            if mode not in {"mention_only", "all"}:
                return Decision(False, "invalid_group_mode"), sid, ""
            if mode == "mention_only" and _get(message, "directed_to_me", default=_get(event, "directed_to_me", default=None)) is not True:
                return Decision(False, "group_not_directed_to_me"), sid, ""
        key = self._message_key(sid, message, event)
        if not key:
            return Decision(False, "missing_event_identity"), sid, ""
        if key in self._seen_set:
            return Decision(False, "duplicate_event"), sid, key
        return None, sid, key

    def observe(self, session: Any, message: Any, event: Any, config: Any,
                policy: Mapping[str, Any], now: Any = None) -> Decision:
        """Register one live incoming event and return its debounce deadline."""
        timestamp = _now_epoch(now)
        denied, sid, key = self._eligibility(session, message, event, config, policy, timestamp)
        if denied:
            return denied
        self._remember(key)
        ready_at = timestamp + max(0.0, float(policy.get("debounce_seconds", 3) or 0))
        self._pending[key] = {"session_id": sid, "ready_at": ready_at, "observed_at": timestamp}
        return Decision(True, "eligible", ready_at)

    def assess_candidate(self, text: Any, incoming_text: str = "") -> Decision:
        """Reject empty, oversized, or obviously sensitive outgoing candidates."""
        if not isinstance(text, str) or not text.strip():
            return Decision(False, "empty_candidate")
        if len(text) > 180:
            return Decision(False, "candidate_too_long")
        if re.search(r"(?i)\bhttps?://[^\s]+", text):
            return Decision(False, "external_link")
        patterns = (
            r"(?i)\b(?:password|passwd|secret|api[_ -]?key|access[_ -]?token)\s*[:=]\s*\S+",
            r"\bsk-[A-Za-z0-9_-]{16,}\b",
            r"(?<!\d)\d{16,19}(?!\d)",
            r"(?<!\d)1[3-9]\d{9}(?!\d)",
        )
        if any(re.search(pattern, text) for pattern in patterns):
            return Decision(False, "sensitive_output")
        # Avoid parroting a long incoming secret verbatim as an automated reply.
        if incoming_text and len(incoming_text) >= 24 and text.strip() == incoming_text.strip():
            return Decision(False, "echoes_incoming_message")
        return Decision(True, "candidate_safe")

    def claim(self, session: Any, message: Any, event: Any, config: Any,
              policy: Mapping[str, Any], candidate: str, now: Any = None,
              incoming_text: str = "") -> Decision:
        """Consume a pending event if debounce, cooldown, and daily/hourly caps pass."""
        timestamp = _now_epoch(now)
        safe = self.assess_candidate(candidate, incoming_text)
        if not safe.allow:
            return safe
        # Re-evaluate all opt-in and event constraints at the moment of sending.
        denied, sid, key = self._eligibility(session, message, event, config, policy, timestamp)
        if denied and denied.reason != "duplicate_event":
            return denied
        if not key:
            return Decision(False, "missing_event_identity")
        pending = self._pending.get(key)
        if pending is None:
            return Decision(False, "event_not_pending")
        if timestamp < pending["ready_at"]:
            return Decision(False, "debounce_pending", pending["ready_at"])
        cooldown = max(0.0, float(policy.get("cooldown_seconds", 60) or 0))
        last = self._last_by_session.get(sid)
        if last is not None and timestamp - last < cooldown:
            return Decision(False, "cooldown", last + cooldown)
        hour_count = sum(1 for item in self._sent if timestamp - item["at"] < 3600)
        day_count = sum(1 for item in self._sent if timestamp - item["at"] < 86400)
        if hour_count >= max(0, int(policy.get("hourly_limit", 10))):
            return Decision(False, "hourly_limit")
        if day_count >= max(0, int(policy.get("daily_limit", 30))):
            return Decision(False, "daily_limit")
        self._sent.append({"at": timestamp, "session_id": sid})
        self._last_by_session[sid] = timestamp
        del self._pending[key]
        return Decision(True, "claimed")

    def snapshot(self) -> dict[str, Any]:
        """Return JSON-serializable guard state; callers choose where to store it."""
        return {"version": self.STATE_VERSION, "seen": list(self._seen),
                "pending": dict(self._pending), "sent": list(self._sent),
                "last_by_session": dict(self._last_by_session)}

    def restore(self, data: Mapping[str, Any]) -> None:
        """Restore bounded state from a snapshot, ignoring malformed entries."""
        self._seen.clear()
        self._seen_set.clear()
        self._pending.clear()
        self._sent.clear()
        self._last_by_session.clear()
        if not isinstance(data, Mapping) or int(data.get("version", self.STATE_VERSION)) != self.STATE_VERSION:
            return
        for key in list(data.get("seen", []))[-self.MAX_DEDUPE:]:
            if isinstance(key, str):
                self._remember(key)
        pending = data.get("pending", {})
        if isinstance(pending, Mapping):
            for key, item in list(pending.items())[-self.MAX_DEDUPE:]:
                if isinstance(key, str) and isinstance(item, Mapping):
                    self._pending[key] = {"session_id": str(item.get("session_id", "")),
                                          "ready_at": _epoch(item.get("ready_at"), 0),
                                          "observed_at": _epoch(item.get("observed_at"), 0)}
        for item in list(data.get("sent", []))[-10000:]:
            if isinstance(item, Mapping) and item.get("session_id") is not None:
                self._sent.append({"at": _epoch(item.get("at"), 0), "session_id": str(item["session_id"])})
        last = data.get("last_by_session", {})
        if isinstance(last, Mapping):
            self._last_by_session.update({str(k): _epoch(v, 0) for k, v in list(last.items())[-self.MAX_DEDUPE:]})
