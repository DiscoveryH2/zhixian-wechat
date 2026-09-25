import base64
import json
import unittest

from src.core.client import ProviderError
from src.core.work_agent import run_work_turn


def response(content="ok", calls=None, usage=None):
    return {"choices": [{"message": {"role": "assistant", "content": content, **({"tool_calls": calls} if calls is not None else {})}}], "usage": usage or {"total_tokens": 7}}


def call(name, args, call_id="c1"):
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class WorkAgentTests(unittest.TestCase):
    config = {"work_base_url": "https://example.test/v1", "work_model": "test-model", "work_api_key": "synthetic-test-key"}

    def test_tool_loop_search_read_write_and_results_redact_config(self):
        queue = [response(calls=[call("search_materials", {"query": "meeting date"})]),
                 response(calls=[call("read_material", {"id": "doc-1"}), call("write_artifact", {"name": "notes.md", "content": "Done", "mime_type": "text/markdown"})]),
                 response("The meeting is Friday. [doc-1]", usage={"total_tokens": 12})]
        def post(route, payload, timeout):
            self.assertEqual(route.url, "https://example.test/v1/chat/completions")
            self.assertEqual(route.key, "synthetic-test-key")
            return queue.pop(0)
        result = run_work_turn([{"role": "user", "content": "What is the meeting date?", "attachment_ids": []}],
                               [{"id": "doc-1", "name": "Calendar", "kind": "text", "text": "Meeting date: Friday"}], self.config, post_json_fn=post)
        self.assertIn("Friday", result["answer"])
        self.assertEqual(result["sources"], [{"id": "doc-1", "name": "Calendar"}])
        self.assertEqual(result["artifacts"][0]["name"], "notes.md")
        self.assertEqual(result["mode"], "tools")
        self.assertNotIn("synthetic-test-key", repr(result))
        self.assertEqual(result["usage"]["total_tokens"], 12)

    def test_traversal_rejected_and_limits(self):
        queue = [response(calls=[call("write_artifact", {"name": "../oops.txt", "content": "x", "mime_type": "text/plain"})]), response("rejected")]
        result = run_work_turn([{"role": "user", "content": "write"}], [], self.config, post_json_fn=lambda *a, **k: queue.pop(0))
        self.assertEqual(result["artifacts"], [])
        self.assertEqual(result["trace"][0]["status"], "rejected")
        self.assertLessEqual(len(result["artifacts"]), 3)

    def test_vision_payload_bounded_to_three_attached_images(self):
        seen = {}
        imgs = [{"id": str(i), "name": f"{i}.png", "kind": "image", "mime": "image/png", "data": bytes([i])} for i in range(4)]
        def post(route, payload, timeout):
            seen.update(payload)
            return response("image answer")
        result = run_work_turn([{"role": "user", "content": "Describe", "attachment_ids": ["0", "1", "2", "3"]}], imgs, self.config, post_json_fn=post)
        content = seen["messages"][-1]["content"]
        self.assertEqual(sum(part["type"] == "image_url" for part in content), 3)
        self.assertEqual(base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1]), b"\x00")
        self.assertEqual(result["answer"], "image answer")

    def test_followup_turn_reuses_recently_referenced_image(self):
        seen = {}
        image = {"id": "earlier-image", "name": "photo.png", "kind": "image", "mime": "image/png", "data": b"pixels"}
        history = [
            {"role": "user", "content": "What is in this picture?", "attachment_ids": ["earlier-image"]},
            {"role": "assistant", "content": "It appears to show a building."},
            {"role": "user", "content": "What color is the roof?", "attachment_ids": []},
        ]
        def post(route, payload, timeout):
            seen.update(payload)
            return response("The roof is red.")
        result = run_work_turn(history, [image], self.config, post_json_fn=post)
        user_parts = seen["messages"][-1]["content"]
        self.assertTrue(any(p.get("type") == "image_url" for p in user_parts))
        self.assertIn("earlier-image", [source["id"] for source in result["sources"]])
        self.assertIn({"tool": "vision_context", "status": "done"}, result["trace"])

    def test_tool_schema_rejection_falls_back_to_direct_and_retrieves_snippets(self):
        seen = []
        def post(route, payload, timeout):
            seen.append(payload)
            if "tools" in payload:
                raise ProviderError("HTTP 400：bad schema")
            return response("Direct answer")
        result = run_work_turn([{"role": "user", "content": "project alpha"}], [{"id": "a", "name": "Brief", "kind": "text", "text": "Project alpha launch is Monday."}], self.config, post_json_fn=post)
        self.assertEqual(result["mode"], "direct")
        self.assertEqual(result["answer"], "Direct answer")
        self.assertIn("Project alpha", seen[0]["messages"][-1]["content"])
        self.assertNotIn("Project alpha", seen[1]["messages"][0]["content"])

    def test_initial_request_includes_relevant_material_and_records_source(self):
        seen = {}
        def post(route, payload, timeout):
            seen.update(payload)
            return response("It is Monday.")
        result = run_work_turn([{"role": "user", "content": "When is launch?"}],
                               [{"id": "launch", "name": "Plan", "kind": "text", "text": "Launch date is Monday."}],
                               self.config, post_json_fn=post)
        user_content = seen["messages"][-1]["content"]
        self.assertIn("Launch date is Monday", user_content)
        self.assertIn("launch", user_content)
        self.assertEqual(result["sources"], [{"id": "launch", "name": "Plan"}])

    def test_chinese_search_matches_unsegmented_text(self):
        seen = {}
        def post(route, payload, timeout):
            seen.update(payload)
            return response("周五开会。")
        result = run_work_turn([{"role": "user", "content": "会议什么时候举行"}],
                               [{"id": "cn", "name": "日程", "kind": "text", "text": "项目会议安排在周五下午。"}],
                               self.config, post_json_fn=post)
        self.assertIn("周五下午", seen["messages"][-1]["content"])
        self.assertEqual(result["sources"][0]["id"], "cn")

    def test_generic_summary_request_sees_uploaded_file_without_tool_call(self):
        seen = {}
        def post(route, payload, timeout):
            seen.update(payload)
            return response("文件总结完成。")
        result = run_work_turn([{"role": "user", "content": "总结这个文件", "attachment_ids": ["brief"]}],
            [{"id": "brief", "name": "brief.txt", "kind": "text", "text": "第一季度目标是完成产品试点。"}],
            self.config, post_json_fn=post)
        self.assertIn("第一季度目标", seen["messages"][-1]["content"])
        self.assertEqual(result["sources"], [{"id": "brief", "name": "brief.txt"}])

    def test_read_material_is_paginated_and_limited_to_six_thousand(self):
        text = "x" * 9000
        queue = [response(calls=[call("read_material", {"id": "large", "offset": 5000, "limit": 6000})]), response("read")]
        seen = []
        def post(route, payload, timeout):
            seen.append(payload)
            return queue.pop(0)
        run_work_turn([{"role": "user", "content": "read"}], [{"id": "large", "name": "Large", "kind": "text", "text": text}], self.config, post_json_fn=post)
        tool_result = json.loads(seen[1]["messages"][-1]["content"])
        self.assertEqual(len(tool_result["text"]), 4000)
        self.assertEqual(tool_result["offset"], 5000)
        self.assertIsNone(tool_result["next_offset"])

    def test_total_conversation_text_is_bounded(self):
        seen = {}
        def post(route, payload, timeout):
            seen.update(payload)
            return response("ok")
        messages = [{"role": "user" if i == 0 else "assistant", "content": "x" * 9000} for i in range(10)]
        messages[-1] = {"role": "user", "content": "latest"}
        run_work_turn(messages, [], self.config, post_json_fn=post)
        char_count = sum(len(m.get("content", "")) if isinstance(m.get("content"), str) else sum(len(p.get("text", "")) for p in m["content"] if isinstance(p, dict)) for m in seen["messages"])
        self.assertLessEqual(char_count, 20_000)

    def test_empty_final_answer_fails_unless_artifact_was_created(self):
        with self.assertRaises(ProviderError):
            run_work_turn([{"role": "user", "content": "hi"}], [], self.config, post_json_fn=lambda *a, **k: response(""))
        queue = [response(calls=[call("write_artifact", {"name": "out.txt", "content": "done", "mime_type": "text/plain"})]), response("")]
        result = run_work_turn([{"role": "user", "content": "write"}], [], self.config, post_json_fn=lambda *a, **k: queue.pop(0))
        self.assertEqual(result["answer"], "")
        self.assertEqual(len(result["artifacts"]), 1)

    def test_non_chat_endpoint_is_rejected(self):
        config = dict(self.config, work_base_url="https://example.test/v1/responses")
        with self.assertRaises(ProviderError):
            run_work_turn([{"role": "user", "content": "hi"}], [], config, post_json_fn=lambda *a, **k: response("ok"))

    def test_malformed_provider_response_is_safe_and_redacted(self):
        with self.assertRaises(ProviderError) as caught:
            run_work_turn([{"role": "user", "content": "hi"}], [], self.config, post_json_fn=lambda *a, **k: {"private": "synthetic-test-key"})
        self.assertNotIn("synthetic-test-key", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
