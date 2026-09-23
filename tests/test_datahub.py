"""Synthetic loopback HTTP integration tests; no real WeFlow or chat access."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from desk.datahub import DataHub
from desk.weflow import WeFlowClient, WeFlowError, normalize_message, validate_url

TEST_TOKEN = "synthetic-test-only"


class MockWeFlow:
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()
        self.fail_sessions = False
        self.fail_moments = False
        self.redirect = False
        self.external_asset = False
        stamp = int(time.time()) - 60
        self.sessions = [{"username": f"synthetic_{i}@chatroom" if i % 3 == 0 else f"synthetic_{i}",
                          "displayName": f"合成群组{i}" if i % 3 == 0 else f"合成联系人{i}",
                          "lastTimestamp": stamp - i, "unreadCount": i % 4,
                          "sessionType": "group" if i % 3 == 0 else "private"} for i in range(65)]
        self.messages = {}
        for session in self.sessions:
            talker = session["username"]
            self.messages[talker] = [{"serverId": f"{talker}-m{i}", "localId": i + 1,
                                      "createTime": stamp - i * 10, "isSend": i % 2,
                                      "localType": 3 if i == 0 else 34 if i == 1 else 1,
                                      "content": None if i < 2 else f"合成消息{i}",
                                      "senderUsername": "synthetic-author"} for i in range(9)]
        self.posts = [{"id": f"post-{i}", "username": "synthetic-friend-a" if i % 2 == 0 else "synthetic-friend-b",
                       "nickname": "合成好友", "createTime": stamp - i, "contentDesc": f"合成朋友圈{i}",
                       "type": 1, "media": [{"url": "https://example.invalid/never-fetch", "key": "private-media-key"}],
                       "likes": ["合成好友二"], "comments": [{"id": "comment", "nickname": "合成评论人", "content": "合成评论"}],
                       "rawXml": "not-for-front-end", "avatarUrl": "https://example.invalid/avatar"} for i in range(12)]
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                args = {key: values[0] for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
                with owner.lock:
                    owner.requests.append({"path": parsed.path, "params": args,
                                           "has_auth": self.headers.get("Authorization") == "Bearer " + TEST_TOKEN})
                if parsed.path == "/health":
                    return self.respond({"status": "ok"})
                if self.headers.get("Authorization") != "Bearer " + TEST_TOKEN:
                    return self.respond({"error": "Unauthorized"}, 401)
                if parsed.path == "/api/v1/sessions":
                    if owner.redirect:
                        self.send_response(302)
                        self.send_header("Location", "http://192.0.2.1/never-follow")
                        self.end_headers()
                        return
                    if owner.fail_sessions:
                        return self.respond({"error": "synthetic backend problem"}, 503)
                    query = args.get("keyword", "").casefold()
                    rows = [item for item in owner.sessions if query in item["displayName"].casefold() or query in item["username"]]
                    return self.respond({"success": True, "sessions": rows[:int(args.get("limit", 20))]})
                if parsed.path == "/api/v1/messages":
                    rows = owner.messages.get(args.get("talker"), [])
                    start, end = int(args.get("start", 0)), int(args.get("end", 0))
                    rows = [dict(row) for row in rows if (not start or row["createTime"] >= start) and (not end or row["createTime"] <= end)]
                    offset, limit = int(args.get("offset", 0)), int(args.get("limit", 50))
                    selected = rows[offset:offset + limit]
                    if args.get("media") == "1":
                        for row in selected:
                            if row["localType"] in (3, 34):
                                row["mediaUrl"] = ("http://192.0.2.1" if owner.external_asset else owner.url) + "/api/v1/media/example.png"
                                row["mediaLocalPath"] = "private-local-path-never-return"
                    return self.respond({"success": True, "messages": selected, "hasMore": offset + limit < len(rows)})
                if parsed.path == "/api/v1/sns/timeline":
                    if owner.fail_moments:
                        return self.respond({"error": "unsupported"}, 404)
                    author = args.get("usernames")
                    rows = [row for row in owner.posts if not author or row["username"] == author]
                    offset, limit = int(args.get("offset", 0)), int(args.get("limit", 20))
                    return self.respond({"success": True, "timeline": rows[offset:offset + limit]})
                if parsed.path == "/api/v1/media/example.png":
                    payload = b"\x89PNG\r\n\x1a\n" + b"synthetic-media-bytes"
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                return self.respond({"error": "not found"}, 404)

            def respond(self, value, code=200):
                raw = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def only(self, path):
        with self.lock:
            return [item for item in self.requests if item["path"] == path]


class DataHubHttpTests(unittest.TestCase):
    def setUp(self):
        self.server = MockWeFlow()
        self.addCleanup(self.server.close)
        self.events = []
        self.hub = DataHub(on_update=lambda kind, data: self.events.append((kind, data)))
        self.addCleanup(lambda: self.hub.close(wait=True))
        self.config = {"source": "weflow", "weflow_url": self.server.url, "weflow_token": TEST_TOKEN}

    def wait_previews(self):
        # Only the first matching preview is relevant to this contract. The
        # remaining 40 background HTTP requests may take longer on a hosted
        # Windows runner and are drained by close(wait=True) in cleanup.
        until = time.monotonic() + 10
        while not any(data["items"][0]["preview"] == "[图片]" for kind, data in self.events
                      if kind == "sessions_updated") and time.monotonic() < until:
            time.sleep(.01)
        self.assertTrue(any(data["items"][0]["preview"] == "[图片]" for kind, data in self.events
                            if kind == "sessions_updated"))

    def test_sessions_progressive_prefix_paging_and_search(self):
        first = self.hub.list_sessions(self.config, limit=20)
        self.assertEqual(len(first["items"]), 20)
        self.assertEqual(first["scope"], "weflow_index")
        second = self.hub.list_sessions(self.config, cursor=first["next_cursor"], limit=20)
        self.assertEqual(len(second["items"]), 20)
        self.assertFalse({s["id"] for s in first["items"]} & {s["id"] for s in second["items"]})
        requests = self.server.only("/api/v1/sessions")
        self.assertEqual([r["params"]["limit"] for r in requests], ["21", "41"])
        self.assertTrue(all("offset" not in r["params"] for r in requests))
        found = self.hub.list_sessions(self.config, query="合成群组", limit=3)
        self.assertTrue(all(s["type"] == "group" for s in found["items"]))
        self.wait_previews()
        self.assertTrue(any(data["items"][0]["preview"] == "[图片]" for kind, data in self.events if kind == "sessions_updated"))
        self.assertTrue(all(r["params"]["limit"] == "1" for r in self.server.only("/api/v1/messages")))

    def test_prewarm_fetches_only_five_single_message_previews(self):
        result = self.hub.prewarm(self.config).result(timeout=5)
        self.assertEqual(result["warmed"], 5)
        requests = self.server.only("/api/v1/messages")
        self.assertEqual(len(requests), 5)
        self.assertTrue(all(r["params"]["limit"] == "1" and r["params"]["media"] == "0" for r in requests))
        self.assertEqual(self.server.only("/api/v1/sessions")[0]["params"]["limit"], "6")

    def test_history_pages_are_on_demand_and_chronological(self):
        session = self.hub.list_sessions(self.config, limit=1)["items"][0]
        first = self.hub.messages(self.config, session["id"], limit=3)
        second = self.hub.messages(self.config, session["id"], cursor=first["next_cursor"], limit=3)
        self.assertEqual(len(first["items"]), 3)
        self.assertTrue(first["available"])
        self.assertTrue(all(m["historical"] for m in first["items"]))
        self.assertEqual([m["timestamp"] for m in first["items"]], sorted(m["timestamp"] for m in first["items"]))
        self.assertFalse({m["id"] for m in first["items"]} & {m["id"] for m in second["items"]})
        self.assertLess(max(m["timestamp"] for m in second["items"]), min(m["timestamp"] for m in first["items"]))
        media = [m for m in first["items"] if m["kind"] == "voice"][0]
        self.assertEqual(media["text"], "[语音]")
        self.assertEqual(media["media"][0]["status"], "on_demand")
        self.assertNotIn("rawContent", json.dumps(first))

    def test_unknown_frontend_talker_is_not_proxied(self):
        page = self.hub.messages(self.config, "arbitrary-talker")
        self.assertFalse(page["available"])
        self.assertEqual(self.server.only("/api/v1/messages"), [])

    def test_moments_pagination_friend_filter_and_no_remote_media_fetch(self):
        first = self.hub.moments(self.config, username="synthetic-friend-a", limit=2)
        second = self.hub.moments(self.config, username="synthetic-friend-a", cursor=first["next_cursor"], limit=2)
        self.assertEqual([item["id"] for item in first["items"]], ["post-0", "post-2"])
        self.assertEqual([item["id"] for item in second["items"]], ["post-4", "post-6"])
        self.assertTrue(all(item["username"] == "synthetic-friend-a" for item in second["items"]))
        self.assertEqual(first["items"][0]["media"][0]["status"], "unavailable")
        self.assertEqual(first["items"][0]["comment_count"], 1)
        encoded = json.dumps(first)
        self.assertNotIn("private-media-key", encoded)
        self.assertNotIn("example.invalid", encoded)
        self.assertNotIn("rawXml", encoded)
        requests = self.server.only("/api/v1/sns/timeline")
        self.assertEqual([r["params"]["offset"] for r in requests], ["0", "2"])
        self.assertTrue(all(r["params"]["media"] == "0" for r in requests))

    def test_media_resolution_exports_only_selected_message_and_hides_token_url(self):
        session = self.hub.list_sessions(self.config, limit=1)["items"][0]
        page = self.hub.messages(self.config, session["id"], limit=3)
        message = next(m for m in page["items"] if m["kind"] == "image")
        resolved = self.hub.resolve_media(self.config, message["media"][0]["id"])
        self.assertEqual(resolved["status"], "available")
        self.assertTrue(resolved["data_url"].startswith("data:image/png;base64,"))
        serialized = json.dumps(resolved)
        self.assertNotIn(TEST_TOKEN, serialized)
        self.assertNotIn("private-local-path", serialized)
        self.assertNotIn(self.server.url, serialized)
        exports = [r for r in self.server.only("/api/v1/messages") if r["params"]["media"] == "1"]
        self.assertEqual(len(exports), 1)
        self.assertEqual(exports[0]["params"]["limit"], "1")
        self.assertTrue(all(r["has_auth"] for r in self.server.only("/api/v1/media/example.png")))

    def test_media_external_url_and_unissued_id_are_rejected(self):
        self.server.external_asset = True
        session = self.hub.list_sessions(self.config, limit=1)["items"][0]
        page = self.hub.messages(self.config, session["id"], limit=1)
        asset = page["items"][0]["media"][0]
        self.assertEqual(self.hub.resolve_media(self.config, asset["id"])["status"], "unavailable")
        self.assertEqual(self.hub.resolve_media(self.config, "http://192.0.2.1")["status"], "unavailable")
        self.assertEqual(self.server.only("/api/v1/media/example.png"), [])

    def test_unavailable_service_returns_only_collected_sessions(self):
        self.server.fail_sessions = True
        self.hub.set_local_sessions([{"id": "ocr:first", "title": "同名", "source": "ocr", "messages": []},
                                     {"id": "import:second", "title": "同名", "source": "import", "count": 300, "preview": "[图片]"}])
        page = self.hub.list_sessions(self.config)
        self.assertEqual(page["scope"], "collected_only")
        self.assertFalse(page["available"])
        self.assertEqual(len(page["items"]), 2)
        imported = next(item for item in page["items"] if item["source"] == "import")
        self.assertEqual(imported["count"], 300)
        self.assertEqual(imported["preview"], "[图片]")
        self.server.fail_moments = True
        moments = self.hub.moments(self.config)
        self.assertFalse(moments["available"])
        self.assertEqual(moments["items"], [])

    def test_missing_token_and_ocr_mode_do_not_read_remote(self):
        page = self.hub.list_sessions({**self.config, "source": "ocr"})
        self.assertEqual(page["scope"], "collected_only")
        self.hub.moments({**self.config, "weflow_token": ""})
        self.assertEqual(self.server.requests, [])

    def test_local_history_pagination_keeps_distinct_import_ids(self):
        rows = [{"id": str(i), "side": "other", "text": f"合成{i}", "timestamp": i} for i in range(6)]
        self.hub.set_local_sessions([{"id": "ocr:same", "title": "同名", "source": "ocr", "messages": []},
                                     {"id": "import:same", "title": "同名", "source": "import", "messages": rows}])
        config = {"source": "ocr"}
        page = self.hub.list_sessions(config, limit=1)
        second = self.hub.list_sessions(config, cursor=page["next_cursor"], limit=1)
        self.assertNotEqual(page["items"][0]["id"], second["items"][0]["id"])
        first_messages = self.hub.messages(config, "import:same", limit=2)
        next_messages = self.hub.messages(config, "import:same", cursor=first_messages["next_cursor"], limit=2)
        self.assertEqual([m["id"] for m in first_messages["items"]], ["4", "5"])
        self.assertEqual([m["id"] for m in next_messages["items"]], ["2", "3"])
        self.assertEqual(self.server.requests, [])

    def test_close_refuses_new_browsing_and_media_requests(self):
        self.hub.close(wait=True)
        with self.assertRaises(ValueError):
            self.hub.list_sessions(self.config)
        self.assertEqual(self.hub.resolve_media(self.config, "unknown")["status"], "unavailable")
        self.assertEqual(self.server.requests, [])

    def test_cursor_cannot_cross_query_or_credentials(self):
        first = self.hub.list_sessions(self.config, limit=1)
        with self.assertRaises(ValueError):
            self.hub.list_sessions(self.config, query="different", cursor=first["next_cursor"])
        with self.assertRaises(ValueError):
            self.hub.list_sessions({**self.config, "weflow_token": "other"}, cursor=first["next_cursor"])

    def test_redirect_is_rejected_and_query_input_clipped(self):
        self.hub.list_sessions(self.config, query="合成" * 300)
        self.assertEqual(len(self.server.only("/api/v1/sessions")[0]["params"]["keyword"]), 200)
        self.server.redirect = True
        page = self.hub.list_sessions(self.config)
        self.assertFalse(page["available"])
        self.assertIn("重定向", page["warning"])


class WeFlowBoundaryTests(unittest.TestCase):
    def test_media_paths_prevent_ssrf_and_traversal(self):
        client = WeFlowClient("http://127.0.0.1:5031", TEST_TOKEN)
        for url in ("https://example.invalid/a.png", "http://127.0.0.1:6000/api/v1/media/x",
                    "file:///private", "/api/v1/media/../private", "/api/v1/media/%252e%252e/private",
                    "/api/v1/sns/media/proxy?url=x", "/api/v1/media/x?access_token=hidden", "/api/v1/media/a\\..\\x"):
            with self.subTest(url=url), self.assertRaises(WeFlowError):
                client.media_path(url)
        self.assertEqual(client.media_path("http://127.0.0.1:5031/api/v1/media/picture.png"), "/api/v1/media/picture.png")
        with self.assertRaises(WeFlowError):
            client.get("/api/v1/sns/media/proxy", {"url": "https://example.invalid"})

    def test_localhost_is_pinned_and_invalid_direction_is_not_guessed(self):
        self.assertEqual(validate_url("http://localhost:5031"), "http://127.0.0.1:5031")
        self.assertIsNone(normalize_message({"isSend": "unknown", "content": "合成"}, "s"))
        with self.assertRaises(ValueError):
            WeFlowClient(token="bad" + chr(10) + "value")


if __name__ == "__main__":
    unittest.main()
