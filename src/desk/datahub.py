"""Bounded on-demand browsing for WeFlow 4.3 and collected local sessions.

No disk persistence, implicit database access, arbitrary URL fetching, or logging
of conversation data. Public methods are synchronous except prewarm(); call the
former on the controller's worker pool. The callback also runs off the UI thread.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import copy
import threading
import time
import uuid

from desk.weflow import (WeFlowClient, WeFlowError, bounded_int, clipped_text,
                         media_kind, message_preview, normalize_message, stable_id)


class DataHub:
    def __init__(self, local_sessions=None, on_update=None, client_factory=None):
        self._lock = threading.RLock()
        self._local = {}
        self._remote = OrderedDict()
        self._indices = OrderedDict()
        self._cursors = OrderedDict()
        self._assets = OrderedDict()
        self._previews = OrderedDict()
        self._pending = set()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="zhixian-data")
        self._closed = False
        self._on_update = on_update
        self._client_factory = client_factory or WeFlowClient
        self.set_local_sessions(local_sessions or [])

    def set_local_sessions(self, sessions):
        """Accept a controller-provided snapshot; never query Qt state ourselves."""
        if isinstance(sessions, dict):
            sessions = list(sessions.values())
        if not isinstance(sessions, (list, tuple)):
            sessions = []
        normalized = {}
        for item in sessions[-2000:]:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            value = copy.deepcopy(item)
            messages = value.get("messages")
            value["messages"] = messages[-500:] if isinstance(messages, list) else []
            normalized[str(value["id"])[:256]] = value
        with self._lock:
            self._local = normalized

    @staticmethod
    def _scope_key(config):
        return stable_id(str(config.get("weflow_url") or "http://127.0.0.1:5031"),
                         str(config.get("weflow_token") or ""))

    @staticmethod
    def _enabled(config):
        return config.get("source") in ("weflow", "auto") and bool(config.get("weflow_token"))

    def _client(self, config):
        return self._client_factory(config.get("weflow_url"), config.get("weflow_token"), timeout=3)

    @staticmethod
    def _warning(exc):
        if isinstance(exc, WeFlowError):
            return str(exc)
        if isinstance(exc, ValueError):
            return "数据源配置或请求参数无效"
        return "数据源暂时不可用，请检查本机 WeFlow 服务"

    def _emit(self, kind, data):
        if self._on_update and not self._closed:
            try:
                self._on_update(kind, data)
            except Exception:
                pass

    def _cursor(self, scope, state):
        ident = uuid.uuid4().hex
        with self._lock:
            self._cursors[ident] = (scope, copy.deepcopy(state), time.monotonic())
            while len(self._cursors) > 500:
                self._cursors.popitem(last=False)
        return ident

    def _read_cursor(self, value, scope):
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError("分页游标无效，请重新加载")
        with self._lock:
            entry = self._cursors.get(value)
        if not entry or entry[0] != scope or time.monotonic() - entry[2] > 600:
            raise ValueError("分页游标已过期或不属于当前查询，请重新加载")
        return copy.deepcopy(entry[1])

    @staticmethod
    def _result(items, source, available, scope, next_cursor=None, warning=None, **extra):
        return {"items": items, "next_cursor": next_cursor, "has_more": bool(next_cursor),
                "source": source, "available": available, "scope": scope,
                "warning": warning, **extra}

    def _local_sessions(self, query, cursor, limit, warning=None):
        cursor_scope = ("local_sessions", query)
        offset = self._read_cursor(cursor, cursor_scope)["offset"] if cursor else 0
        with self._lock:
            values = copy.deepcopy(list(self._local.values()))
        values.sort(key=lambda s: self._number(s.get("updated")), reverse=True)
        items = []
        for item in values:
            title = clipped_text(item.get("title") or "未命名会话", 256)
            if query.casefold() not in title.casefold():
                continue
            messages = item.get("messages", [])
            preview = message_preview(messages[-1]) if messages and isinstance(messages[-1], dict) else clipped_text(item.get("preview"), 160)
            items.append({"id": item["id"], "title": title, "source": item.get("source", "ocr"),
                          "talker": "", "type": item.get("type", "unknown"), "preview": preview,
                          "preview_status": "ready" if preview else "empty", "updated": item.get("updated"),
                          "unread_count": 0, "count": bounded_int(item.get("count"), len(messages), 0, 100000000)})
        next_cursor = self._cursor(cursor_scope, {"offset": offset + limit}) if len(items) > offset + limit else None
        return self._result(items[offset:offset + limit], "local", False, "collected_only", next_cursor,
                            (warning + "；" if warning else "") + "仅显示本机已收集或导入的会话，不是微信全量会话。")

    def list_sessions(self, config, query="", cursor=None, limit=20):
        if self._closed:
            raise ValueError("数据浏览服务已关闭")
        query, limit = clipped_text(query), bounded_int(limit, 20, 1, 50)
        if not self._enabled(config):
            return self._local_sessions(query, cursor, limit, "尚未启用可用的 WeFlow 数据源")
        scope = ("sessions", self._scope_key(config), query)
        if cursor:
            # A fallback cursor remains usable even if the service comes back.
            with self._lock:
                entry = self._cursors.get(cursor)
            if entry and entry[0] == ("local_sessions", query):
                return self._local_sessions(query, cursor, limit)
        try:
            return self._list_remote(config, query, cursor, limit, scope, schedule=True)
        except ValueError:
            if cursor:
                raise
            return self._local_sessions(query, None, limit, "WeFlow 配置无效")
        except Exception as exc:
            return self._local_sessions(query, None, limit, self._warning(exc))

    def _list_remote(self, config, query, cursor, limit, scope, schedule):
        state = self._read_cursor(cursor, scope) if cursor else None
        with self._lock:
            index = self._indices.get(state["index"]) if state else None
        if state and not index:
            raise ValueError("会话索引已过期，请重新加载")
        offset = state["offset"] if state else 0
        if index is None:
            index = {"id": uuid.uuid4().hex, "order": [], "rows": {}, "requested": 0, "ended": False}
            with self._lock:
                self._indices[index["id"]] = index
                while len(self._indices) > 32:
                    self._indices.popitem(last=False)
        need = min(10000, offset + limit + 1)
        if index["requested"] < need and not index["ended"]:
            client = self._client(config)
            try:
                rows = client.sessions(limit=need, keyword=query)
            finally:
                client.close()
            with self._lock:
                if self._closed:
                    raise ValueError("数据浏览服务已关闭")
                for row in rows:
                    talker = clipped_text(row.get("username") or row.get("id"), 256)
                    if not talker:
                        continue
                    if talker not in index["rows"]:
                        index["order"].append(talker)
                    item = self._session_item(config, row, talker)
                    index["rows"][talker] = item
                    self._remote[(scope[1], item["id"])] = {**item, "talker": talker}
                    self._remote.move_to_end((scope[1], item["id"]))
                while len(self._remote) > 10000:
                    self._remote.popitem(last=False)
                index["requested"] = need
                index["ended"] = len(rows) < need or need >= 10000
        with self._lock:
            talkers = index["order"][offset:offset + limit]
            items = [copy.deepcopy(index["rows"][talker]) for talker in talkers]
            more = len(index["order"]) > offset + limit or not index["ended"]
        for item in items:
            preview = self._cached_preview(scope[1], item["id"])
            if preview:
                item.update(preview)
            elif schedule:
                self._schedule_preview(config, item)
        next_cursor = self._cursor(scope, {"index": index["id"], "offset": offset + len(items)}) if more and items else None
        warning = "上游会话索引最多返回 10000 条；可使用名称搜索缩小范围" if need >= 10000 else None
        return self._result(items, "weflow", True, "weflow_index", next_cursor, warning,
                            pagination="progressive_prefix", total_loaded=len(index["order"]))

    def _session_item(self, config, row, talker):
        preview = clipped_text(row.get("summary") or row.get("lastMessage"), 160)
        return {"id": "weflow:" + stable_id(talker), "title": clipped_text(row.get("displayName") or row.get("name") or talker, 256),
                "source": "weflow", "talker": talker,
                "type": row.get("sessionType") or ("group" if talker.endswith("@chatroom") else "private"),
                "preview": preview, "preview_status": "ready" if preview else "pending",
                "updated": row.get("lastTimestamp", row.get("lastMessageAt")),
                "unread_count": bounded_int(row.get("unreadCount"), 0, 0, 1000000),
                "count": row.get("messageCount")}

    def _cached_preview(self, scope, sid):
        with self._lock:
            value = self._previews.get((scope, sid))
            if value and time.monotonic() - value[0] < 60:
                return copy.deepcopy(value[1])
        return None

    def _load_preview(self, config, item):
        scope = self._scope_key(config)
        client = None
        try:
            client = self._client(config)
            rows = client.messages_page(item["talker"], limit=1)["messages"]
            preview = {"preview": message_preview(rows[0]) if rows else "", "preview_status": "ready" if rows else "empty"}
        except Exception:
            preview = {"preview": "", "preview_status": "unavailable"}
        finally:
            if client:
                client.close()
        with self._lock:
            if not self._closed:
                self._previews[(scope, item["id"])] = (time.monotonic(), preview)
                self._previews.move_to_end((scope, item["id"]))
                while len(self._previews) > 1000:
                    self._previews.popitem(last=False)
        if not self._closed:
            self._emit("sessions_updated", {"items": [{**item, **preview}], "source": "weflow"})
        return preview

    def _schedule_preview(self, config, item):
        key = (self._scope_key(config), item["id"])
        with self._lock:
            if self._closed or key in self._pending or len(self._pending) >= 50:
                return
            self._pending.add(key)
        def run():
            try:
                if not self._closed:
                    self._load_preview(dict(config), item)
            finally:
                with self._lock:
                    self._pending.discard(key)
        try:
            self._pool.submit(run)
        except RuntimeError:
            with self._lock:
                self._pending.discard(key)

    def prewarm(self, config, limit=5):
        config, limit = dict(config), bounded_int(limit, 5, 1, 10)
        def run():
            if self._closed or not self._enabled(config):
                return {"warmed": 0, "source": "local", "scope": "collected_only"}
            try:
                scope = ("sessions", self._scope_key(config), "")
                page = self._list_remote(config, "", None, limit, scope, schedule=False)
                warmed = 0
                for item in page["items"]:
                    if self._closed:
                        break
                    if not self._cached_preview(scope[1], item["id"]):
                        self._load_preview(config, item)
                    warmed += 1
                return {"warmed": warmed, "source": "weflow", "scope": "weflow_index"}
            except Exception as exc:
                return {"warmed": 0, "source": "local", "scope": "collected_only", "warning": self._warning(exc)}
        return self._pool.submit(run)

    @staticmethod
    def _number(value):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def messages(self, config, session_id, cursor=None, limit=50):
        if self._closed:
            raise ValueError("数据浏览服务已关闭")
        session_id, limit = clipped_text(session_id, 256), bounded_int(limit, 50, 1, 100)
        key = self._scope_key(config)
        with self._lock:
            remote = copy.deepcopy(self._remote.get((key, session_id)))
            local = copy.deepcopy(self._local.get(session_id))
        if not remote or not self._enabled(config):
            return self._local_messages(local, session_id, cursor, limit)
        scope = ("messages", key, session_id)
        state = self._read_cursor(cursor, scope) if cursor else {"offset": 0, "end": int(time.time())}
        client = None
        try:
            client = self._client(config)
            result = client.messages_page(remote["talker"], limit=limit, offset=state["offset"], end=state["end"])
            items = []
            for row in result["messages"]:
                message = normalize_message(row, session_id)
                if message:
                    message["historical"] = True
                    message["media"] = self._message_assets(config, remote, message, row)
                    items.append(message)
            items.sort(key=lambda m: self._number(m.get("timestamp")))
            more = result["hasMore"] and bool(result["messages"])
            next_cursor = self._cursor(scope, {"offset": state["offset"] + len(result["messages"]), "end": state["end"]}) if more else None
            return self._result(items, "weflow", True, "weflow_history", next_cursor,
                                session={"id": session_id, "title": remote["title"], "source": "weflow"},
                                order="chronological", direction="older", watermark=state["end"])
        except Exception as exc:
            return self._local_messages(local, session_id, None, limit, self._warning(exc))
        finally:
            if client:
                client.close()

    def _local_messages(self, local, sid, cursor, limit, warning=None):
        scope = ("local_messages", sid)
        offset = self._read_cursor(cursor, scope)["offset"] if cursor else 0
        if not local:
            return self._result([], "local", False, "collected_only", warning=warning or "此会话尚未收集；请刷新会话列表或主动导入历史",
                                session={"id": sid, "title": "", "source": "local"}, order="chronological", direction="older")
        rows = local.get("messages", [])
        end = max(0, len(rows) - offset)
        start = max(0, end - limit)
        items = copy.deepcopy(rows[start:end])
        for row in items:
            row["historical"] = True
            if media_kind(row) in ("image", "voice", "video", "emoji"):
                row["media"] = [{"id": None, "kind": media_kind(row), "status": "unavailable", "reason": "此本地文本来源未提供媒体资产"}]
        next_cursor = self._cursor(scope, {"offset": offset + len(items)}) if start > 0 else None
        return self._result(items, "local", False, "collected_only", next_cursor,
                            warning or "仅显示已经收集或导入的本地消息", session={"id": sid, "title": local.get("title", ""), "source": local.get("source", "ocr")},
                            order="chronological", direction="older")

    def _register_asset(self, scope, ident, info):
        asset_id = "asset:" + stable_id(scope, ident)
        with self._lock:
            self._assets[asset_id] = {**info, "scope": scope, "created": time.monotonic()}
            self._assets.move_to_end(asset_id)
            while len(self._assets) > 1000:
                self._assets.popitem(last=False)
        return asset_id

    def _message_assets(self, config, session, message, row):
        kind = media_kind(row)
        if kind not in ("image", "voice", "video", "emoji"):
            return []
        direct = row.get("mediaUrl")
        stamp = bounded_int(row.get("createTime", row.get("timestamp")), 0, 0, 10 ** 13)
        info = {"kind": kind, "talker": session["talker"], "session_id": session["id"],
                "message_id": message["id"], "timestamp": stamp, "url": direct or ""}
        if not direct and not stamp:
            return [{"id": None, "kind": kind, "status": "unavailable", "reason": "此消息没有可定位的媒体资产"}]
        ident = self._register_asset(self._scope_key(config), message["id"], info)
        return [{"id": ident, "kind": kind, "status": "on_demand", "reason": "按需向本机 WeFlow 读取；能否解码取决于该消息资源是否存在"}]

    def moments(self, config, username="", cursor=None, limit=20, query=""):
        if self._closed:
            raise ValueError("数据浏览服务已关闭")
        username, query, limit = clipped_text(username, 256), clipped_text(query), bounded_int(limit, 20, 1, 50)
        if not self._enabled(config):
            return self._result([], "local", False, "collected_only", warning="朋友圈需要可用的 WeFlow API；OCR 和孤立媒体缓存不能提供帖子索引")
        key = self._scope_key(config)
        scope = ("moments", key, username, query)
        state = self._read_cursor(cursor, scope) if cursor else {"offset": 0, "end": int(time.time())}
        client = None
        try:
            client = self._client(config)
            rows = client.moments_page(limit=limit + 1, offset=state["offset"], username=username, keyword=query, end=state["end"])
            more = len(rows) > limit
            items = [self._moment(config, client, row) for row in rows[:limit]]
            next_cursor = self._cursor(scope, {"offset": state["offset"] + len(items), "end": state["end"]}) if more else None
            return self._result(items, "weflow", True, "weflow_moments", next_cursor,
                                username=username, watermark=state["end"])
        except Exception as exc:
            return self._result([], "local", False, "collected_only", warning=self._warning(exc), username=username)
        finally:
            if client:
                client.close()

    def _moment(self, config, client, row):
        post_id = clipped_text(row.get("id") or row.get("tid") or stable_id(row.get("username"), row.get("createTime")), 256)
        assets = []
        for index, medium in enumerate((row.get("media") or [])[:20]):
            if not isinstance(medium, dict):
                continue
            kind = "video" if row.get("type") == 15 else "image"
            url = medium.get("url") or ""
            try:
                path = client.media_path(url)
            except Exception:
                assets.append({"id": None, "kind": kind, "status": "unavailable", "reason": "上游只提供朋友圈远程资源；未自动访问外网或调用代理下载"})
                continue
            asset_id = self._register_asset(self._scope_key(config), (post_id, index), {"kind": kind, "url": path})
            assets.append({"id": asset_id, "kind": kind, "status": "on_demand", "reason": "可按需读取上游已导出的本机媒体"})
        comments = row.get("comments") if isinstance(row.get("comments"), list) else []
        likes = row.get("likes") if isinstance(row.get("likes"), list) else []
        location = row.get("location") if isinstance(row.get("location"), dict) else {}
        return {"id": post_id, "username": clipped_text(row.get("username"), 256),
                "author": clipped_text(row.get("nickname") or row.get("username"), 256),
                "text": clipped_text(row.get("contentDesc"), 16000), "timestamp": row.get("createTime"),
                "type": row.get("type"), "media": assets, "like_count": len(likes), "comment_count": len(comments),
                "comments": [{"id": clipped_text(c.get("id"), 128), "author": clipped_text(c.get("nickname"), 256),
                               "text": clipped_text(c.get("content"), 2000), "reply_to": clipped_text(c.get("refNickname"), 256)}
                              for c in comments[:20] if isinstance(c, dict)],
                "location": clipped_text(location.get("label") or location.get("poiName") or location.get("city"), 256),
                "link_title": clipped_text(row.get("linkTitle"), 256), "source": "weflow"}

    def resolve_media(self, config, asset_id):
        if self._closed:
            return {"status": "unavailable", "kind": "unknown", "mime_type": None, "reason": "数据浏览服务已关闭"}
        with self._lock:
            asset = copy.deepcopy(self._assets.get(clipped_text(asset_id, 128)))
        unavailable = lambda reason, kind="unknown": {"status": "unavailable", "kind": kind, "mime_type": None, "reason": reason}
        if not asset or asset["scope"] != self._scope_key(config) or time.monotonic() - asset["created"] > 600:
            return unavailable("媒体引用无效或过期，请重新加载消息")
        if not self._enabled(config):
            return unavailable("尚未启用 WeFlow 数据源", asset["kind"])
        client = None
        try:
            client = self._client(config)
            url = asset.get("url")
            if not url:
                stamp = asset.get("timestamp", 0)
                rows = client.messages_page(asset["talker"], limit=200, start=stamp, end=stamp)["messages"]
                position = None
                for index, raw in enumerate(rows):
                    msg = normalize_message(raw, asset["session_id"])
                    if msg and msg["id"] == asset["message_id"]:
                        position = index
                        break
                if position is None:
                    return unavailable("媒体消息已变化或不在上游可读取范围内", asset["kind"])
                # Export exactly the selected row, never the whole conversation.
                exported = client.messages_page(asset["talker"], limit=1, offset=position, start=stamp, end=stamp, media=True)["messages"]
                raw = exported[0] if exported else {}
                msg = normalize_message(raw, asset["session_id"])
                if not msg or msg["id"] != asset["message_id"]:
                    return unavailable("媒体消息位置已改变，已停止读取", asset["kind"])
                url = raw.get("mediaUrl")
                if not url:
                    return unavailable("WeFlow 未提供可用媒体，可能尚未下载或无法解码", asset["kind"])
            payload, mime = client.read_media(url)
            return {"status": "available", "kind": asset["kind"], "mime_type": mime,
                    "data_url": "data:" + mime + ";base64," + base64.b64encode(payload).decode("ascii"),
                    "reason": None}
        except Exception as exc:
            return unavailable(self._warning(exc), asset["kind"])
        finally:
            if client:
                client.close()

    def close(self, wait=False):
        with self._lock:
            self._closed = True
            self._local.clear()
            self._remote.clear()
            self._indices.clear()
            self._cursors.clear()
            self._assets.clear()
            self._previews.clear()
        self._pool.shutdown(wait=wait, cancel_futures=True)
