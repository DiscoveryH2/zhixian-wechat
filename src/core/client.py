"""Small, bounded HTTP client for real typed Jev decision endpoints.

Protocol references (verified 2026-09-22): https://docs.typesafe.ai/api
https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-questions-and-answers-request
No provider response bodies, URLs, headers or keys appear in display errors.
"""
from __future__ import annotations

import ipaddress
import json
import math
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field


class ProviderError(Exception):
    """A fixed, user-displayable message; never wraps a provider body."""


@dataclass(frozen=True)
class Route:
    url: str
    model: str
    key: str = field(repr=False)
    mode: str = "gateway"


def _local(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError("请填写 API 地址。")
    if any(ord(c) < 32 for c in value):
        raise ProviderError("API 地址含空格或控制字符。")
    value = value.strip()
    if " " in value:
        raise ProviderError("API 地址含空格或控制字符。")
    try:
        p = urllib.parse.urlsplit(value)
        port = p.port
    except (ValueError, TypeError):
        raise ProviderError("API 地址格式不正确。") from None
    if (p.scheme not in ("https", "http") or not p.hostname or p.username
            or p.password or p.query or p.fragment):
        raise ProviderError("请使用不含账号、参数或片段的 HTTPS API 地址。")
    if p.scheme == "http" and not _local(p.hostname):
        raise ProviderError("远程 API 必须使用 HTTPS；本机 localhost 可使用 HTTP。")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), "", ""))


def _model(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ProviderError("请填写有效的模型名称。")
    return value.strip()


def _key(value: object, url: str) -> str:
    key = value.strip() if isinstance(value, str) else ""
    if any(ord(c) < 32 or ord(c) > 126 for c in key):
        raise ProviderError("API Key 格式不正确，请重新填写。")
    if not key and not _local(urllib.parse.urlsplit(url).hostname or ""):
        raise ProviderError("请先填写 API Key。")
    return key


def resolve_decision(config: dict) -> Route:
    """Known providers, /v1 bases, /api bases, or explicit gateway endpoint.

Custom /v1 -> /v1/systemone; custom /api -> /api/alpha/decisions.
Other nonempty paths are complete gateway endpoints, used without guessing.
Ordinary chat/completions and responses URLs are explicitly rejected.
"""
    url = validate_url(config.get("base_url") or "https://openrouter.ai/api/v1")
    p = urllib.parse.urlsplit(url)
    origin, path = f"{p.scheme}://{p.netloc}", p.path
    if path.endswith(("/chat/completions", "/responses", "/completions")):
        raise ProviderError("判断接口需要 Jev decisions 或 systemone 协议，不能填写普通 Chat API。")
    if p.hostname == "openrouter.ai":
        if path not in ("", "/api", "/api/v1", "/api/alpha", "/api/alpha/decisions"):
            raise ProviderError("OpenRouter 判断地址应为 /api/alpha/decisions 或 /api/v1 基础地址。")
        url, mode = origin + "/api/alpha/decisions", "openrouter"
    elif p.hostname == "api.typesafe.ai":
        if path not in ("", "/v1", "/v1/systemone"):
            raise ProviderError("TypeSafe 判断地址应为 /v1/systemone 或 /v1 基础地址。")
        url, mode = origin + "/v1/systemone", "typesafe"
    elif path.endswith("/systemone"):
        mode = "typesafe"
    elif path.endswith("/decisions"):
        mode = "decisions"
    elif path.endswith("/api"):
        url, mode = url + "/alpha/decisions", "decisions"
    elif path.endswith("/api/alpha"):
        url, mode = url + "/decisions", "decisions"
    elif path.endswith("/api/v1"):
        url, mode = url[:-len("/v1")] + "/alpha/decisions", "decisions"
    elif not path or path.endswith("/v1"):
        url, mode = url + ("/v1/systemone" if not path else "/systemone"), "typesafe"
    else:
        mode = "gateway"
    return Route(url, _model(config.get("model_name")), _key(config.get("api_key"), url), mode)


def _origin(url: str) -> tuple:
    p = urllib.parse.urlsplit(url)
    return p.scheme, p.hostname, p.port or (443 if p.scheme == "https" else 80)


def resolve_reply(config: dict, decision: Route) -> Route | None:
    model = (config.get("reply_model") or "").strip()
    base = (config.get("reply_base_url") or "").strip()
    separate_key = (config.get("reply_api_key") or "").strip()
    if not model and not base and not separate_key:
        if decision.mode != "openrouter":
            return None
        return Route("https://openrouter.ai/api/v1/chat/completions", "deepseek/deepseek-v4.1-flash", decision.key, "chat")
    if not model:
        raise ProviderError("高级回复设置需要填写回复模型名称。")
    if not base:
        if decision.mode == "openrouter":
            base = "https://openrouter.ai/api/v1"
        elif decision.url.endswith("/api/alpha/decisions"):
            base = decision.url[:-len("/alpha/decisions")] + "/v1"
        else:
            raise ProviderError("此判断服务需要在高级设置中单独填写回复 API 地址。")
    base = validate_url(base)
    p = urllib.parse.urlsplit(base)
    if p.hostname == "api.typesafe.ai" or p.path.endswith(("/decisions", "/systemone")):
        raise ProviderError("回复生成需要 Chat Completions 接口；TypeSafe Jev 判断接口不生成文字。")
    if p.path.endswith("/chat/completions"):
        url = base
    elif not p.path:
        url = base + ("/api/v1/chat/completions" if p.hostname == "openrouter.ai" else "/v1/chat/completions")
    else:
        url = base + "/chat/completions"
    key = separate_key
    if not key and _origin(url) == _origin(decision.url):
        key = decision.key
    return Route(url, _model(model), _key(key, url), "chat")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward an Authorization header to a redirected endpoint.
        raise ProviderError("API 地址发生重定向，请填写服务商提供的最终接口地址。")


def post_json(route: Route, payload: dict, timeout: float = 20.0, retries: int = 2) -> dict:
    try:
        timeout = max(1.0, min(float(timeout), 60.0))
        retries = max(0, min(int(retries), 2))
        if not math.isfinite(timeout):
            timeout = 20.0
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise ProviderError("请求配置或输入内容格式不正确。") from None
    headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json", "User-Agent": "Zhixian-PC/0.1"}
    if route.key:
        headers["Authorization"] = "Bearer " + route.key
    opener = urllib.request.build_opener(_NoRedirect())
    for attempt in range(retries + 1):
        req = urllib.request.Request(route.url, data=data, headers=headers, method="POST")
        try:
            with opener.open(req, timeout=timeout) as response:
                body = response.read(2 * 1024 * 1024 + 1)
            if len(body) > 2 * 1024 * 1024:
                raise ProviderError("API 返回内容过大，已停止处理。")
            try:
                result = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                raise ProviderError("API 未返回有效 JSON，请检查接口地址。") from None
            if not isinstance(result, dict):
                raise ProviderError("API 返回结构不正确，应为 JSON 对象。")
            if result.get("error"):
                raise ProviderError("服务返回错误，请检查模型、额度和接口配置。")
            return result
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code in (429, 529) and attempt < retries:
                time.sleep(0.5 * 2 ** attempt)
                continue
            message = {401: "API Key 无效或已过期。", 402: "服务余额不足。", 403: "服务拒绝访问，请检查模型和账号权限。", 404: "API 路径或模型不存在，请检查地址和模型名称。", 422: "服务不接受 Jev 请求格式，请确认使用 decisions/systemone 协议。", 429: "请求过于频繁，有限重试后仍未成功。", 529: "模型服务繁忙，有限重试后仍未成功。"}.get(code, "模型服务请求失败。")
            raise ProviderError(f"HTTP {code}：{message}") from None
        except (socket.timeout, TimeoutError):
            if attempt < retries:
                time.sleep(0.5 * 2 ** attempt)
                continue
            raise ProviderError("模型请求超时，请稍后重试。") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise ProviderError("无法连接模型服务，请检查网络和 API 地址。") from None
    raise ProviderError("模型请求未完成。")
