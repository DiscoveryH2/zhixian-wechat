"""Synthetic posts and typed mock API responses; no WeChat or paid requests."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from core import moments
from core.client import ProviderError

CONFIG = {"base_url": "https://openrouter.ai/api", "model_name": "typesafe/jev-custom-version", "api_key": "test-secret-value"}
POST = {"id": "synthetic-post", "author": "合成作者", "text": "今天第一次完成十公里，慢慢进步。", "image_descriptions": ["视觉模型描述：一张跑步路线截图，文字可能识别不全。"]}


class Response:
    def __init__(self, data):
        self.data = json.dumps(data).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, size=-1):
        return self.data if size < 0 else self.data[:size]


class Transport:
    def __init__(self, comment="comment", draft_failure=False, rank_failure=False, confidence=True):
        self.requests = []
        self.comment, self.draft_failure, self.rank_failure, self.confidence = comment, draft_failure, rank_failure, confidence
    def open(self, request, timeout):
        payload = json.loads(request.data)
        self.requests.append(payload)
        if "messages" in payload:
            if self.draft_failure:
                raise urllib.error.HTTPError(request.full_url, 401, CONFIG["api_key"], {}, io.BytesIO(CONFIG["api_key"].encode()))
            return Response({"choices": [{"message": {"content": json.dumps(["十公里达成，恭喜！", "一步一步跑出来的进步", "这个小目标拿下了"], ensure_ascii=False)}}]})
        if self.rank_failure and "best_comment" in payload["questions"]:
            return Response({"answers": {}})
        answers = {}
        for name, question in payload["questions"].items():
            if question["type"] == "choice":
                keys = list(question["criteria"])
                choice = self.comment if name == "comment_advice" else ("reply_b" if name == "best_comment" else keys[0])
                answers[name] = {"type": "choice", "choice": choice}
                if self.confidence:
                    answers[name].update(confidence=0.7, probabilities={key: 0.8 if key == choice else 0.2 / (len(keys) - 1) for key in keys})
            elif question["type"] == "score":
                answers[name] = {"type": "score", "score": 1.4}
            else:
                answers[name] = {"type": "noul", "noul": 0.9}
        return Response({"answers": answers, "usage": {"input_tokens": 60}})


class MomentsTests(unittest.TestCase):
    def transport(self, **kwargs):
        transport = Transport(**kwargs)
        patcher = patch("core.client.urllib.request.build_opener", return_value=transport)
        patcher.start()
        self.addCleanup(patcher.stop)
        return transport

    def test_real_typed_judgment_then_independent_drafts_and_ranking(self):
        transport = self.transport()
        result = moments.analyze_post(POST, "普通朋友", [{"side": "other", "text": "合成私聊，仅作关系背景"}], CONFIG)
        self.assertEqual(result["post_id"], POST["id"])
        self.assertEqual(result["like_recommendation"], "like")
        self.assertEqual(result["comment_recommendation"], "comment")
        self.assertEqual(len(result["candidates"]), 3)
        self.assertEqual(result["best_index"], 1)
        self.assertEqual(result["candidates"][1]["score"], 0.8)
        self.assertFalse(result["automatic_actions"])
        self.assertTrue(all(c["channel"] == "comment" for c in result["candidates"]))
        self.assertIn("不代表作者的真实想法", result["warning"])
        self.assertEqual(len(transport.requests), 3)
        self.assertEqual(transport.requests[0]["model"], CONFIG["model_name"])
        self.assertIn("questions", transport.requests[0])
        self.assertNotIn("messages", transport.requests[0])
        self.assertIn("绝不能把私聊细节", transport.requests[1]["messages"][0]["content"])
        generation_state = json.loads(transport.requests[1]["messages"][1]["content"])
        self.assertNotIn("private_relationship_history", generation_state)
        self.assertNotIn("合成私聊，仅作关系背景", transport.requests[1]["messages"][1]["content"])
        self.assertIn("fallible", transport.requests[0]["state"]["post"]["image_description_notice"])
        self.assertNotIn(CONFIG["api_key"], json.dumps(result, ensure_ascii=False))

    def test_private_or_skip_advice_does_not_generate_public_comment(self):
        for choice in ("private_message", "skip", "wait"):
            transport = Transport(comment=choice)
            with patch("core.client.urllib.request.build_opener", return_value=transport):
                result = moments.analyze_post(POST, "同事", [], CONFIG)
            self.assertEqual(result["comment_recommendation"], choice)
            self.assertEqual(result["candidates"], [])
            self.assertEqual(len(transport.requests), 1)
            self.assertFalse(result["automatic_actions"])

    def test_failed_draft_retains_judgment_and_safe_warning(self):
        self.transport(draft_failure=True)
        result = moments.analyze_post(POST, "朋友", [], CONFIG)
        self.assertEqual(result["like_recommendation"], "like")
        self.assertEqual(result["candidates"], [])
        self.assertIn("草稿不可用", result["warning"])
        self.assertNotIn(CONFIG["api_key"], result["warning"])

    def test_failed_ranking_does_not_invent_winner(self):
        self.transport(rank_failure=True)
        result = moments.analyze_post(POST, "朋友", [], CONFIG)
        self.assertEqual(len(result["candidates"]), 3)
        self.assertIsNone(result["best_index"])
        self.assertTrue(all(c["score"] is None for c in result["candidates"]))

    def test_unknown_confidence_is_not_made_up(self):
        self.transport(confidence=False)
        result = moments.analyze_post(POST, "朋友", [], CONFIG)
        self.assertIsNone(result["confidence"])
        self.assertIsNone(result["like_confidence"])
        self.assertIsNone(result["comment_confidence"])
        self.assertTrue(all(c["score"] is None for c in result["candidates"]))

    def test_native_typesafe_provides_only_judgment_without_generator(self):
        transport = self.transport()
        result = moments.analyze_post(POST, "朋友", [], dict(CONFIG, base_url="https://api.typesafe.ai/v1", model_name="jev-latest"))
        self.assertEqual(len(transport.requests), 1)
        self.assertIn("未配置评论生成模型", result["warning"])
        self.assertEqual(result["candidates"], [])

    def test_plain_chat_cannot_replace_jev_typed_answers(self):
        class Fake:
            def open(self, *args, **kwargs):
                return Response({"choices": [{"message": {"content": "看起来适合点赞"}}]})
        with patch("core.client.urllib.request.build_opener", return_value=Fake()), self.assertRaises(ProviderError):
            moments.analyze_post(POST, "朋友", [], CONFIG)

    def test_missing_content_and_raw_image_in_post_are_rejected(self):
        for post in ({}, {"text": "", "image_descriptions": []}, {"image_descriptions": [b"raw-media"]}):
            with self.subTest(post=post), self.assertRaises(ProviderError):
                moments.analyze_post(post, "朋友", [], CONFIG)

    def test_image_only_post_retains_uncertainty_and_history_is_bounded(self):
        transport = self.transport(comment="wait")
        history = [{"side": "other", "text": "合成历史 " + str(i)} for i in range(100)]
        result = moments.analyze_post({"image_descriptions": ["可能是海边日落"]}, "朋友", history, CONFIG)
        state = transport.requests[0]["state"]
        self.assertEqual(len(state["private_relationship_history"]), 30)
        self.assertIn("never be quoted", state["history_notice"])
        self.assertTrue(result["post_id"].startswith("post:"))


if __name__ == "__main__":
    unittest.main()
