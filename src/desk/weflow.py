"""Opt-in, loopback-only WeFlow HTTP/SSE reader. No write/send endpoints."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import threading
import urllib.error
import urllib.parse
import urllib.request


def stable_id(*parts):
    encoded = json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def validate_url(value):
    parsed = urllib.parse.urlsplit(str(value or "http://127.0.0.1:5031").strip())
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
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("WeFlow 返回重定向，已拒绝跟随")


def normalize_message(raw, session_id, incoming=False):
    """Raw HTTP messages have direction; the upstream SSE only emits incoming."""
    sent = raw.get("isSend", raw.get("is_send"))
    if sent is not None:
        side = "me" if str(sent).lower() in ("1", "true") else "other"
    elif raw.get("side") in ("me", "other"):
        side = raw["side"]
    elif incoming:
        side = "other"
    else:
        # Do not silently invent a direction for an incompatible API schema.
        return None
    content = raw.get("content", raw.get("parsedContent", raw.get("text", "")))
    if content is None or not str(content).strip():
        return None
    timestamp = raw.get("timestamp", raw.get("createTime", raw.get("create_time")))
    sender = str(raw.get("senderUsername") or raw.get("senderName") or raw.get("sourceName") or "")
    raw_id = raw.get("serverId") or raw.get("rawid") or raw.get("messageKey") or raw.get("localId")
    if str(raw_id) in ("0", "None", ""):
        raw_id = stable_id(side, sender, timestamp, str(content))
    return {"id": "wfmsg:" + stable_id(session_id, str(raw_id)), "side": side,
            "text": str(content).strip(), "sender": sender, "timestamp": timestamp,
            "source": "weflow"}


class WeFlowClient:
    def __init__(self, url="http://127.0.0.1:5031", token="", timeout=3):
        self.url = validate_url(url)
        self.token = str(token or "")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        self._stream = None
        self._lock = threading.Lock()
        self.last_event_id = ""

    def _open(self, path, params=None, stream=False, authenticated=True):
        headers = {"Accept": "text/event-stream" if stream else "application/json"}
        if authenticated:
            if not self.token:
                raise RuntimeError("请先在高级设置填写 WeFlow API Token")
            headers["Authorization"] = "Bearer " + self.token
        if stream and self.last_event_id:
            headers["Last-Event-ID"] = self.last_event_id
        url = self.url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            return self.opener.open(urllib.request.Request(url, headers=headers), timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise RuntimeError("WeFlow API Token 无效") from None
            if exc.code == 403:
                raise RuntimeError("WeFlow 主动推送未启用，请在 WeFlow 设置中启用") from None
            raise RuntimeError(f"WeFlow 接口返回 HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError):
            raise RuntimeError("无法连接本机 WeFlow，请确认已打开并开启 API 服务") from None

    def get(self, path, params=None, authenticated=True):
        with self._open(path, params, authenticated=authenticated) as response:
            body = response.read(4 * 1024 * 1024 + 1)
        if len(body) > 4 * 1024 * 1024:
            raise RuntimeError("WeFlow 响应过大，请缩小查询范围")
        try:
            result = json.loads(body)
        except (UnicodeError, ValueError):
            raise RuntimeError("WeFlow 返回了无法识别的数据") from None
        if not isinstance(result, dict):
            raise RuntimeError("WeFlow 响应格式不兼容")
        if result.get("success") is False:
            raise RuntimeError("WeFlow 尚未能读取微信，请先检查 WeFlow 数据连接")
        return result

    def health(self):
        return self.get("/health", authenticated=False).get("status") == "ok"

    def sessions(self, limit=20):
        return self.get("/api/v1/sessions", {"limit": limit}).get("sessions", [])

    def messages(self, talker, limit=50):
        return self.get("/api/v1/messages", {"talker": talker, "limit": limit}).get("messages", [])

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
