"""Opt-in, loopback-only WeFlow HTTP/SSE reader. No write/send endpoints."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

# Endpoint and field contract: hicccc77/WeFlow, 70aff53ef17b5d34b1915a878d2a16a2d5dcce05,
# electron/services/httpService.ts and electron/services/snsService.ts (4.3.0).
READ_ENDPOINTS = frozenset({"/health", "/api/v1/health", "/api/v1/sessions", "/api/v1/messages",
                            "/api/v1/contacts", "/api/v1/group-members", "/api/v1/push/messages",
                            "/api/v1/sns/timeline", "/api/v1/sns/usernames"})
MEDIA_LABELS = {"image": "[图片]", "voice": "[语音]", "video": "[视频]", "emoji": "[表情]",
                "file": "[文件]", "card": "[名片]", "location": "[位置]"}
# Type 49 also contains quoted replies, links and other app messages; retain
# its upstream parsed text instead of mislabeling every such message as a file.
MESSAGE_TYPES = {3: "image", 34: "voice", 43: "video", 47: "emoji", 42: "card", 48: "location"}


class WeFlowError(RuntimeError):
    def __init__(self, message, code="unavailable"):
        super().__init__(message)
        self.code = code


def clipped_text(value, limit=200):
    return "".join(c for c in str(value or "").strip() if c >= " " or c in "\n\t")[:limit]


def bounded_int(value, default, low, high):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError, OverflowError):
        return default


def media_kind(raw):
    kind = raw.get("mediaType") or raw.get("kind")
    if kind in MEDIA_LABELS:
        return kind
    return MESSAGE_TYPES.get(bounded_int(raw.get("localType", raw.get("type")), 0, 0, 2 ** 48), "text")


def message_preview(raw, limit=160):
    kind = media_kind(raw)
    if kind in MEDIA_LABELS:
        return MEDIA_LABELS[kind]
    content = raw.get("content") or raw.get("parsedContent") or raw.get("text") or ""
    return clipped_text(content, limit)


def stable_id(*parts):
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def validate_url(value):
    text = str(value or "http://127.0.0.1:5031").strip()
    if len(text) > 2048 or any(ord(c) < 32 or c == " " for c in text):
        raise ValueError("WeFlow 地址含非法字符")
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:
        raise ValueError("WeFlow 地址无效") from None
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("WeFlow 地址应为本机 HTTP 服务地址，不得包含凭据或参数")
    host = (parsed.hostname or "").lower()
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    if not local:
        raise ValueError("WeFlow 仅允许连接 localhost 或回环 IP 地址")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("WeFlow 端口无效") from exc
    if parsed.path not in ("", "/"):
        raise ValueError("WeFlow 地址请填写服务根地址，例如 http://127.0.0.1:5031")
    # Avoid DNS/hosts-file overrides of localhost sending credentials elsewhere.
    authority = parsed.netloc
    if host == "localhost":
        authority = "127.0.0.1" + (f":{parsed.port}" if parsed.port is not None else "")
    return urllib.parse.urlunsplit((parsed.scheme, authority, "", "", ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise WeFlowError("WeFlow 返回重定向，已拒绝跟随", "unsafe_redirect")


def normalize_message(raw, session_id, incoming=False):
    """Raw HTTP messages have direction; the upstream SSE only emits incoming."""
    sent = raw.get("isSend", raw.get("is_send"))
    if sent is not None:
        if str(sent).lower() not in ("1", "true", "0", "false"):
            return None
        side = "me" if str(sent).lower() in ("1", "true") else "other"
    elif raw.get("side") in ("me", "other"):
        side = raw["side"]
    elif incoming:
        side = "other"
    else:
        # Do not silently invent a direction for an incompatible API schema.
        return None
    content = message_preview(raw, 16000)
    if content is None or not str(content).strip():
        return None
    timestamp = raw.get("timestamp", raw.get("createTime", raw.get("create_time")))
    sender = clipped_text(raw.get("senderUsername") or raw.get("senderName") or raw.get("sourceName"), 256)
    raw_id = next((raw.get(key) for key in ("serverId", "rawid", "messageKey", "localId")
                   if str(raw.get(key) or "") not in ("", "0", "None")), None)
    if str(raw_id) in ("0", "None", ""):
        raw_id = stable_id(side, sender, timestamp, str(content))
    return {"id": "wfmsg:" + stable_id(session_id, str(raw_id)), "side": side,
            "text": str(content).strip(), "sender": sender, "timestamp": timestamp,
            "source": "weflow", "kind": media_kind(raw)}


class WeFlowClient:
    def __init__(self, url="http://127.0.0.1:5031", token="", timeout=3):
        self.url = validate_url(url)
        self.token = str(token or "")
        if len(self.token) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in self.token):
            raise ValueError("WeFlow Token 格式无效")
        try:
            self.timeout = max(.2, min(10, float(timeout))) if math.isfinite(float(timeout)) else 3
        except (ValueError, TypeError):
            self.timeout = 3
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._stream = None
        self._lock = threading.Lock()
        self.last_event_id = ""

    def _open(self, path, params=None, stream=False, authenticated=True):
        if path not in READ_ENDPOINTS:
            if not path.startswith("/api/v1/media/") or self.media_path(path) != path:
                raise WeFlowError("不允许访问此 WeFlow 端点", "unsafe_endpoint")
        headers = {"Accept": "text/event-stream" if stream else "application/json"}
        if authenticated:
            if not self.token:
                raise WeFlowError("请先在高级设置填写 WeFlow API Token", "unauthorized")
            headers["Authorization"] = "Bearer " + self.token
        if stream and self.last_event_id:
            if not re.fullmatch(r"[0-9]{1,20}", self.last_event_id):
                raise WeFlowError("WeFlow 事件游标无效", "invalid_response")
            headers["Last-Event-ID"] = self.last_event_id
        url = self.url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            return self.opener.open(urllib.request.Request(url, headers=headers), timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise WeFlowError("WeFlow API Token 无效", "unauthorized") from None
            if exc.code == 403:
                raise WeFlowError("WeFlow 拒绝此请求，请检查服务权限或主动推送设置", "forbidden") from None
            if exc.code == 404:
                raise WeFlowError("此 WeFlow 版本不提供所需接口", "unsupported") from None
            raise WeFlowError(f"WeFlow 接口返回 HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError):
            raise WeFlowError("无法连接本机 WeFlow，请确认已打开并开启 API 服务") from None

    def get(self, path, params=None, authenticated=True):
        try:
            with self._open(path, params, authenticated=authenticated) as response:
                body = response.read(4 * 1024 * 1024 + 1)
        except (OSError, TimeoutError):
            raise WeFlowError("读取 WeFlow 数据超时，请稍后重试") from None
        if len(body) > 4 * 1024 * 1024:
            raise WeFlowError("WeFlow 响应过大，请缩小查询范围", "invalid_response")
        try:
            result = json.loads(body)
        except (UnicodeError, ValueError):
            raise WeFlowError("WeFlow 返回了无法识别的数据", "invalid_response") from None
        if not isinstance(result, dict):
            raise WeFlowError("WeFlow 响应格式不兼容", "invalid_response")
        if result.get("success") is False:
            raise WeFlowError("WeFlow 尚未能读取微信，请先检查 WeFlow 数据连接")
        return result

    def health(self):
        return self.get("/health", authenticated=False).get("status") == "ok"

    def sessions(self, limit=20, keyword=""):
        rows = self.get("/api/v1/sessions", {"limit": bounded_int(limit, 20, 1, 10000),
                                              "keyword": clipped_text(keyword)}).get("sessions", [])
        if not isinstance(rows, list):
            raise WeFlowError("WeFlow 会话列表格式不兼容", "invalid_response")
        return [row for row in rows if isinstance(row, dict)]

    def messages(self, talker, limit=50):
        return self.messages_page(talker, limit=limit)["messages"]

    def messages_page(self, talker, limit=50, offset=0, start=0, end=0, media=False, keyword=""):
        talker = clipped_text(talker, 256)
        if not talker or "\n" in talker or "\t" in talker:
            raise ValueError("会话标识无效")
        result = self.get("/api/v1/messages", {"talker": talker, "limit": bounded_int(limit, 50, 1, 200),
                            "offset": bounded_int(offset, 0, 0, 1000000), "start": bounded_int(start, 0, 0, 10 ** 13),
                            "end": bounded_int(end, 0, 0, 10 ** 13), "media": int(bool(media)),
                            "keyword": clipped_text(keyword)})
        rows = result.get("messages", [])
        if not isinstance(rows, list):
            raise WeFlowError("WeFlow 消息格式不兼容", "invalid_response")
        return {"messages": [row for row in rows if isinstance(row, dict)],
                "hasMore": result.get("hasMore") is True}

    def moments_page(self, limit=20, offset=0, username="", keyword="", end=0):
        username = clipped_text(username, 256)
        if any(c in username for c in ",\n\t"):
            raise ValueError("朋友圈好友标识无效")
        result = self.get("/api/v1/sns/timeline", {"limit": bounded_int(limit, 20, 1, 200),
                         "offset": bounded_int(offset, 0, 0, 1000000), "usernames": username,
                         "keyword": clipped_text(keyword), "end": bounded_int(end, 0, 0, 10 ** 13),
                         "media": 0, "inline": 0, "replace": 0})
        rows = result.get("timeline", [])
        if not isinstance(rows, list):
            raise WeFlowError("WeFlow 朋友圈格式不兼容", "invalid_response")
        return [row for row in rows if isinstance(row, dict)]

    def media_path(self, value):
        value = str(value or "")
        if len(value) > 2048 or any(ord(c) < 32 for c in value):
            raise WeFlowError("媒体地址无效", "unsafe_media")
        try:
            parsed = urllib.parse.urlsplit(value)
            if parsed.query or parsed.fragment or parsed.username or parsed.password:
                raise ValueError
            if parsed.netloc or parsed.scheme:
                origin = validate_url(urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")))
                if origin != self.url:
                    raise ValueError
            decoded = parsed.path
            for _ in range(4):
                expanded = urllib.parse.unquote(decoded)
                if expanded == decoded:
                    break
                decoded = expanded
            if (not decoded.startswith("/api/v1/media/") or "%" in decoded or "\\" in decoded
                    or any(p in (".", "..", "") for p in decoded.split("/")[1:])
                    or any(ord(c) < 32 for c in decoded)):
                raise ValueError
            return urllib.parse.quote(decoded, safe="/")
        except (ValueError, TypeError):
            raise WeFlowError("此媒体没有可安全访问的本机资产地址", "unsafe_media") from None

    def read_media(self, value, max_bytes=16 * 1024 * 1024):
        path = self.media_path(value)
        try:
            with self._open(path) as response:
                payload = response.read(max_bytes + 1)
        except (OSError, TimeoutError):
            raise WeFlowError("读取媒体超时，请重试") from None
        if len(payload) > max_bytes:
            raise WeFlowError("媒体超出当前预览大小限制", "too_large")
        mime = self.media_mime(payload)
        if not mime:
            raise WeFlowError("该媒体格式暂不支持安全预览", "unsupported_media")
        return payload, mime

    @staticmethod
    def media_mime(payload):
        if payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if payload.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if payload.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
            return "image/webp"
        if payload[:4] == b"RIFF" and payload[8:12] == b"WAVE":
            return "audio/wav"
        if payload.startswith(b"OggS"):
            return "audio/ogg"
        if payload.startswith(b"ID3") or (len(payload) > 2 and payload[0] == 255 and payload[1] & 0xe0 == 0xe0):
            return "audio/mpeg"
        if len(payload) > 12 and payload[4:8] == b"ftyp":
            return "video/mp4"
        return None

    def close(self):
        with self._lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass

    def events(self, stop_event):
        response = self._open("/api/v1/push/messages", stream=True)
        with self._lock:
            self._stream = response
        try:
            event, lines, event_id = "message", [], ""
            while not stop_event.is_set():
                line = response.readline(256 * 1024)
                if not line:
                    break
                text = line.decode("utf-8").rstrip("\r\n")
                if not text:
                    if lines:
                        try:
                            payload = json.loads("\n".join(lines))
                        except (ValueError, UnicodeError):
                            payload = None
                        if isinstance(payload, dict):
                            if event_id:
                                self.last_event_id = event_id
                            yield event, payload
                    event, lines, event_id = "message", [], ""
                elif text.startswith("event:"):
                    event = text[6:].strip()
                elif text.startswith("data:"):
                    lines.append(text[5:].lstrip())
                elif text.startswith("id:"):
                    event_id = text[3:].strip()
        finally:
            with self._lock:
                if self._stream is response:
                    self._stream = None
            response.close()
