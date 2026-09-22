"""No paid or external calls: typed mock transports plus one loopback HTTP test."""
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.client import ProviderError, Route, post_json, resolve_decision, resolve_reply, validate_url
from core.engine import analyze, test_connection, _answers
from core.draft import parse_candidates, SYSTEM_PROMPT
from core.questions import build_state, judge_questions


MESSAGES = [{"id": "m1", "side": "me", "text": "晚饭六点见"}, {"id": "m2", "side": "other", "text": "好，老地方吗？", "sender": "小林"}]
CONFIG = {"base_url": "https://openrouter.ai/api/v1", "model_name": "typesafe/future-jev", "api_key": "test-secret-value", "context_limit": 10}


def decisions(payload):
    answers = {}
    for key, q in payload["questions"].items():
        if q["type"] == "noul":
            answers[key] = {"type": "noul", "noul": 0.8}
        elif q["type"] == "score":
            answers[key] = {"type": "score", "score": 2.75, "confidence": 0.7}
        else:
            options = list(q["criteria"])
            choice = "reply_b" if key == "best_reply" else options[0]
            answers[key] = {"type": "choice", "choice": choice, "confidence": 0.63, "probabilities": {k: (0.8 if k == choice else 0.2 / (len(options) - 1)) for k in options}}
    return {"answers": answers, "usage": {"input_tokens": 42, "output_tokens": 7}}


def draft_response():
    return {"choices": [{"message": {"content": json.dumps(["对，六点老地方见", "嗯，还是老地方", "对，到时候见"], ensure_ascii=False)}}], "usage": {"prompt_tokens": 50, "completion_tokens": 20}}


class FakeResponse:
    def __init__(self, data):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, n=-1):
        return self.data[:n] if n >= 0 else self.data


class FakeOpener:
    def __init__(self, callback):
        self.callback = callback
        self.requests = []
    def open(self, request, timeout):
        payload = json.loads(request.data)
        self.requests.append((request, payload, timeout))
        return FakeResponse(self.callback(request, payload))


class EndpointTests(unittest.TestCase):
    def test_openrouter_common_bases_and_model_is_not_locked(self):
        for base in ("https://openrouter.ai", "https://openrouter.ai/api", "https://openrouter.ai/api/v1/", "https://openrouter.ai/api/alpha/decisions"):
            route = resolve_decision(dict(CONFIG, base_url=base))
            self.assertEqual(route.url, "https://openrouter.ai/api/alpha/decisions")
            self.assertEqual(route.model, "typesafe/future-jev")
            self.assertEqual(route.mode, "openrouter")

    def test_native_and_gateway_paths(self):
        cases = {"https://api.typesafe.ai": "https://api.typesafe.ai/v1/systemone", "https://api.typesafe.ai/v1": "https://api.typesafe.ai/v1/systemone", "https://proxy.example/v1": "https://proxy.example/v1/systemone", "https://proxy.example/api": "https://proxy.example/api/alpha/decisions", "https://proxy.example/team/jev": "https://proxy.example/team/jev", "https://proxy.example/alpha/decisions": "https://proxy.example/alpha/decisions", "http://127.0.0.1:9999/v1/systemone": "http://127.0.0.1:9999/v1/systemone"}
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(resolve_decision(dict(CONFIG, base_url=source)).url, expected)

    def test_chat_url_is_never_misrepresented_as_jev(self):
        for base in ("https://proxy.example/v1/chat/completions", "https://proxy.example/v1/responses"):
            with self.assertRaises(ProviderError):
                resolve_decision(dict(CONFIG, base_url=base))

    def test_openrouter_compatible_gateway_base(self):
        config = dict(CONFIG, base_url="https://proxy.example/team/api/v1", reply_model="writer")
        decision = resolve_decision(config)
        self.assertEqual(decision.url, "https://proxy.example/team/api/alpha/decisions")
        reply = resolve_reply(config, decision)
        self.assertEqual(reply.url, "https://proxy.example/team/api/v1/chat/completions")
        self.assertEqual(reply.key, decision.key)

    def test_url_validation_and_no_secret_echo(self):
        for url in ("file:///tmp/data", "http://api.example.com", "https://secret@host.example/api", "https://host.example/api?key=test-secret-value", "https://host.example/#test-secret-value", "https://host.example:bad", "https://host.example/\n"):
            with self.subTest(url=url):
                with self.assertRaises(ProviderError) as error:
                    validate_url(url)
                self.assertNotIn("test-secret-value", str(error.exception))
        self.assertEqual(validate_url("http://[::1]:8080/v1"), "http://[::1]:8080/v1")

    def test_generation_and_keys_are_routed_separately(self):
        judge = resolve_decision(CONFIG)
        reply = resolve_reply(CONFIG, judge)
        self.assertEqual(reply.key, judge.key)
        self.assertNotEqual(reply.model, judge.model)
        native = resolve_decision(dict(CONFIG, base_url="https://api.typesafe.ai/v1", model_name="jev-latest"))
        self.assertIsNone(resolve_reply({}, native))
        with self.assertRaises(ProviderError):
            resolve_reply(dict(CONFIG, reply_model="writer", reply_base_url="https://different.example/v1"), judge)
        reply = resolve_reply(dict(CONFIG, reply_model="writer", reply_base_url="https://different.example/v1/chat/completions", reply_api_key="separate-test-key"), judge)
        self.assertEqual(reply.key, "separate-test-key")
        self.assertEqual(reply.model, "writer")
        self.assertNotIn("separate-test-key", repr(reply))


class EngineTests(unittest.TestCase):
    def transport(self, callback):
        opener = FakeOpener(callback)
        patched = patch("core.client.urllib.request.build_opener", return_value=opener)
        patched.start()
        self.addCleanup(patched.stop)
        return opener

    def test_full_chain_payload_and_normalized_result(self):
        opener = self.transport(lambda r, p: decisions(p) if "questions" in p else draft_response())
        result = analyze(MESSAGES, CONFIG, background="老地方是楼下餐厅", history=[{"side": "other", "text": "我不吃辣"}], reply_to="小林")
        self.assertEqual(result["best_index"], 1)
        self.assertEqual(result["candidates"][1]["score"], 0.8)
        self.assertEqual(result["confidence"], 0.63)
        self.assertEqual(result["risk"], 2.75)
        self.assertIsNone(result["urgency"])
        self.assertEqual(len(result["answers"]), 7)
        self.assertEqual(len(opener.requests), 3)
        judgment = next(p for _, p, _ in opener.requests if len(p.get("questions", {})) == 7)
        self.assertEqual(judgment["model"], CONFIG["model_name"])
        self.assertEqual(judgment["state"]["background"], "老地方是楼下餐厅")
        self.assertEqual(judgment["state"]["chat"]["reply_to"], "小林")
        self.assertEqual(judgment["state"]["chat"]["latest_from"], "other")
        generation = next(p for _, p, _ in opener.requests if "messages" in p)
        self.assertIn("老地方是楼下餐厅", generation["messages"][1]["content"])
        self.assertIn("不能编造", generation["messages"][0]["content"])
        self.assertEqual(generation["reasoning"], {"enabled": False})
        self.assertNotIn(CONFIG["api_key"], json.dumps(result, ensure_ascii=False))

    def test_draft_failure_does_not_remove_valid_judgments(self):
        def callback(request, payload):
            if "messages" in payload:
                raise urllib.error.HTTPError(request.full_url, 401, "leaked-test-secret-value", {}, io.BytesIO(b"test-secret-value"))
            return decisions(payload)
        opener = self.transport(callback)
        result = analyze(MESSAGES, CONFIG)
        self.assertTrue(result["intent"])
        self.assertEqual(result["candidates"], [])
        self.assertIsNone(result["best_index"])
        self.assertIn("候选生成失败", result["warning"])
        self.assertNotIn("test-secret-value", result["warning"])
        self.assertEqual(len(opener.requests), 2)

    def test_typesafe_decision_only_and_missing_confidence(self):
        def callback(request, payload):
            response = decisions(payload)
            for answer in response["answers"].values():
                answer.pop("confidence", None)
                answer.pop("probabilities", None)
            return {"data": response}
        opener = self.transport(callback)
        result = analyze(MESSAGES, dict(CONFIG, base_url="https://api.typesafe.ai/v1", model_name="jev-latest"))
        self.assertIsNone(result["confidence"])
        self.assertEqual(result["candidates"], [])
        self.assertIn("判断模式", result["warning"])
        self.assertEqual(len(opener.requests), 1)

    def test_null_invalid_risk_and_missing_probabilities_remain_unknown(self):
        def callback(request, payload):
            if "messages" in payload:
                return draft_response()
            response = decisions(payload)
            if "danger_level" in response["answers"]:
                response["answers"]["danger_level"]["score"] = 10
                response["answers"]["true_intent"]["confidence"] = None
            if "best_reply" in response["answers"]:
                response["answers"]["best_reply"].pop("probabilities")
            return response
        self.transport(callback)
        result = analyze(MESSAGES, CONFIG)
        self.assertIsNone(result["risk"])
        self.assertIsNone(result["confidence"])
        self.assertTrue(all(c["score"] is None for c in result["candidates"]))
        self.assertIn("部分判断", result["warning"])

    def test_rank_failure_keeps_real_drafts_without_fake_winner(self):
        def callback(request, payload):
            if "messages" in payload:
                return draft_response()
            if "best_reply" in payload["questions"]:
                return {"answers": {}}
            return decisions(payload)
        self.transport(callback)
        result = analyze(MESSAGES, CONFIG)
        self.assertEqual(len(result["candidates"]), 3)
        self.assertIsNone(result["best_index"])
        self.assertTrue(all(c["score"] is None for c in result["candidates"]))
        self.assertIn("候选排序不可用", result["warning"])

    def test_regular_chat_response_is_rejected_by_decision_parser(self):
        self.transport(lambda r, p: draft_response())
        with self.assertRaises(ProviderError) as error:
            analyze(MESSAGES, dict(CONFIG, base_url="https://api.typesafe.ai/v1"))
        self.assertIn("typed answers", str(error.exception))

    def test_malformed_typed_values(self):
        questions = judge_questions()
        for wrong in (None, [], "secret", {"true_intent": {"type": "choice", "choice": "unknown"}}, {"danger_level": {"score": float("nan")}}, {"danger_level": {"score": True}}, {"should_reply_now": {"noul": "0.9"}}):
            with self.subTest(wrong=wrong):
                with self.assertRaises(ProviderError):
                    _answers({"answers": wrong}, questions)

    def test_connection_tests_real_typed_protocol_and_generation(self):
        opener = self.transport(lambda r, p: decisions(p) if "questions" in p else draft_response())
        result = test_connection(CONFIG)
        self.assertTrue(result["success"])
        self.assertTrue(result["generation_available"])
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(opener.requests[0][1]["state"], {"status": "ready"})

    def test_context_200_boundary_matches_desktop_settings(self):
        messages = [{"side": "other", "text": f"第 {i} 条消息"} for i in range(205)]
        opener = self.transport(lambda r, p: decisions(p))
        for limit in (200, 250):
            with self.subTest(limit=limit):
                analyze(messages, dict(CONFIG, base_url="https://api.typesafe.ai/v1", context_limit=limit))
                transmitted = opener.requests[-1][1]["state"]["chat"]["messages"]
                self.assertEqual(len(transmitted), 200)
                self.assertEqual(transmitted[0]["text"], "第 5 条消息")
                self.assertEqual(transmitted[-1]["text"], "第 204 条消息")


class ContextAndDraftTests(unittest.TestCase):
    def test_seven_android_questions_and_bounded_deduplicated_history(self):
        questions = judge_questions()
        self.assertEqual(set(questions), {"literal_question", "true_intent", "danger_level", "should_reply_now", "best_action", "she_needs", "tension_resolved"})
        self.assertEqual(len(questions["danger_level"]["criteria"]), 10)
        self.assertTrue(all("background" in q["instructions"] for q in questions.values()))
        state = build_state(MESSAGES, "朋友", background="事实", history=MESSAGES + [{"side": "other", "text": "之前约好了六点"}])
        self.assertEqual(state["history"], [{"from": "other", "text": "之前约好了六点"}])
        self.assertEqual(state["chat"]["messages"][0]["from"], "me")

    def test_parse_real_candidates_fenced_json_and_no_fabricated_fallback(self):
        response = {"choices": [{"message": {"content": '```json\n["好", "行", "六点见"]\n```'}}]}
        self.assertEqual(parse_candidates(response), ["好", "行", "六点见"])
        for response in ({}, {"choices": []}, {"choices": [{"message": {"content": "这是解释，不是候选"}}]}, {"choices": [{"message": {"content": "[]"}}]}):
            with self.assertRaises(ProviderError):
                parse_candidates(response)


class TransportTests(unittest.TestCase):
    def test_bounded_rate_limit_retries_and_redacted_error(self):
        def fail(request, payload):
            raise urllib.error.HTTPError(request.full_url, 429, "test-secret-value", {}, io.BytesIO(b"test-secret-value"))
        opener = FakeOpener(fail)
        with patch("core.client.urllib.request.build_opener", return_value=opener), patch("core.client.time.sleep"):
            with self.assertRaises(ProviderError) as error:
                post_json(Route("https://example.com/api", "model", "test-secret-value"), {}, retries=100)
        self.assertEqual(len(opener.requests), 3)
        self.assertNotIn("test-secret-value", str(error.exception))
        self.assertIn("429", str(error.exception))

    def test_overload_retries_and_then_succeeds(self):
        count = [0]
        def respond(request, payload):
            count[0] += 1
            if count[0] < 3:
                raise urllib.error.HTTPError(request.full_url, 529, "busy", {}, io.BytesIO(b"ignored"))
            return {"answers": {"ok": {"type": "noul", "noul": 1}}}
        opener = FakeOpener(respond)
        with patch("core.client.urllib.request.build_opener", return_value=opener), patch("core.client.time.sleep"):
            response = post_json(Route("https://example.com/api", "model", "key"), {})
        self.assertEqual(len(opener.requests), 3)
        self.assertIn("answers", response)

    def test_redirect_does_not_forward_credentials(self):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/credential-receiver")
                self.end_headers()
            def do_GET(self):
                captured.append(self.path)
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            route = Route(f"http://127.0.0.1:{server.server_port}/redirect", "model", "test-secret-value")
            with self.assertRaises(ProviderError) as error:
                post_json(route, {})
            self.assertEqual(captured, ["/redirect"])
            self.assertNotIn("test-secret-value", str(error.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_malformed_json_has_safe_error(self):
        opener = FakeOpener(lambda r, p: b"not-json-test-secret-value")
        with patch("core.client.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(ProviderError) as error:
                post_json(Route("https://example.com/api", "model", "test-secret-value"), {})
        self.assertNotIn("test-secret-value", str(error.exception))

    def test_timeout_is_bounded(self):
        opener = FakeOpener(lambda r, p: (_ for _ in ()).throw(TimeoutError("test-secret-value")))
        with patch("core.client.urllib.request.build_opener", return_value=opener), patch("core.client.time.sleep"):
            with self.assertRaises(ProviderError) as error:
                post_json(Route("https://example.com/api", "model", "key"), {}, timeout=200, retries=1)
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(opener.requests[0][2], 60)
        self.assertNotIn("test-secret-value", str(error.exception))

    def test_actual_loopback_http_request_and_native_response(self):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                captured.append((self.path, self.headers.get("Authorization"), payload))
                content = json.dumps(decisions(payload)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = test_connection({"base_url": f"http://127.0.0.1:{server.server_port}/v1", "model_name": "jev-custom-build", "api_key": ""})
            self.assertTrue(result["success"])
            self.assertFalse(result["generation_available"])
            self.assertEqual(captured[0][0], "/v1/systemone")
            self.assertIsNone(captured[0][1])
            self.assertEqual(captured[0][2]["model"], "jev-custom-build")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
