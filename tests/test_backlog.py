from datetime import datetime, timezone
import unittest

from src.core.backlog import evaluate_backlog, prefilter_backlog


NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def history(candidate=None):
    candidate = candidate or {"id": "in-1", "side": "other", "text": "周末你有空吗？", "timestamp": NOW.timestamp()}
    return [
        {"id": "out-1", "side": "me", "text": "最近怎么样？", "timestamp": NOW.timestamp() - 60},
        candidate,
    ]


def config():
    return {"base_url": "https://api.typesafe.ai/v1", "model_name": "test-model", "api_key": "test"}


def responder(choice="reply", confidence=0.9):
    def post(route, payload, timeout=20):
        assert payload["model"] == "test-model"
        assert list(payload["questions"]) == ["reply_now"]
        assert len(payload["state"]["chat"]["messages"]) <= 30
        return {"answers": {"reply_now": {"type": "choice", "choice": choice, "confidence": confidence}},
                "usage": {"input_tokens": 100, "total_tokens": 120}}
    return post


class BacklogTests(unittest.TestCase):
    def test_prefilter_latest_unresolved_turn(self):
        rows = history() + [{"id": "in-2", "side": "other", "text": "你看呢？", "timestamp": NOW.timestamp()}]
        result = prefilter_backlog(rows, chat_type="private", chat_name="Synthetic Contact", now=NOW)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["target_message_id"], "in-2")
        self.assertEqual(result["context"][-1]["text"], "你看呢？")


    def test_direct_model_yes_and_no_are_typed(self):
        yes = evaluate_backlog(history(), config(), chat_type="direct", chat_name="Synthetic Contact", now=NOW,
                               post_json_fn=responder("reply"))
        no = evaluate_backlog(history(), config(), chat_type="private", chat_name="Synthetic Contact", now=NOW,
                              post_json_fn=responder("skip"))
        self.assertIs(yes["should_reply"], True)
        self.assertEqual(yes["target_message_id"], "in-1")
        self.assertEqual(yes["usage"]["input_tokens"], 100)
        self.assertIs(no["should_reply"], False)


    def test_self_last_and_stale_message_are_skipped_without_provider_call(self):
        rows = history() + [{"id": "out-2", "side": "me", "text": "好", "timestamp": NOW.timestamp()}]
        called = []
        result = evaluate_backlog(rows, config(), chat_type="private", chat_name="Synthetic Contact", now=NOW,
                                  post_json_fn=lambda *a, **k: called.append(1))
        self.assertEqual(result["reason"], "self_is_last_message")
        self.assertFalse(called)
        old = {"id": "old", "side": "other", "text": "在吗", "timestamp": NOW.timestamp() - 8 * 86400}
        result = prefilter_backlog(history(old), chat_type="private", chat_name="Synthetic Contact", now=NOW)
        self.assertEqual(result["reason"], "stale_over_7_days")

    def test_unknown_ocr_time_requires_ack_and_stronger_confidence(self):
        unknown = {"id": "ocr-1", "side": "other", "text": "周末你有空吗？", "timestamp": None}
        denied = prefilter_backlog(history(unknown), chat_name="Synthetic Contact", now=NOW)
        self.assertEqual(denied["reason"], "missing_or_invalid_timestamp")
        accepted = evaluate_backlog(history(unknown), config(), chat_name="Synthetic Contact", now=NOW,
                                    current_session_acknowledged=True,
                                    post_json_fn=responder("reply", 0.7))
        self.assertIsNone(accepted["should_reply"])
        strong = evaluate_backlog(history(unknown), config(), chat_name="Synthetic Contact", now=NOW,
                                  current_session_acknowledged=True,
                                  post_json_fn=responder("reply", 0.8))
        self.assertIs(strong["should_reply"], True)
        self.assertIn("时间未知", strong["warning"])
        stale = dict(unknown, timestamp=NOW.timestamp() - 8 * 86400)
        skipped = evaluate_backlog(history(stale), config(), chat_name="Synthetic Contact", now=NOW,
                                   current_session_acknowledged=True,
                                   post_json_fn=responder("reply", 1.0))
        self.assertEqual(skipped["reason"], "stale_over_7_days")


    def test_group_requires_explicit_direction_or_literal_mention(self):
        group = [{"id": "g-out", "side": "me", "text": "有事群里说", "timestamp": NOW.timestamp() - 60},
                 {"id": "g-in", "side": "other", "text": "谁周末能来？", "timestamp": NOW.timestamp()}]
        self.assertEqual(prefilter_backlog(group, chat_type="group", chat_name="Synthetic Group", now=NOW)["reason"], "group_not_explicitly_directed")
        tagged = [dict(group[0]), dict(group[1], text="@River 周末能来吗？")]
        self.assertTrue(prefilter_backlog(tagged, chat_type="group", chat_name="Synthetic Group", own_display_name="River", now=NOW)["eligible"])
        marked = [dict(group[0]), dict(group[1], directed_to_me=True)]
        self.assertTrue(prefilter_backlog(marked, chat_type="group", chat_name="Synthetic Group", now=NOW)["eligible"])
        self.assertEqual(prefilter_backlog(group, chat_type="group", chat_name="Synthetic Group", own_display_name="River", now=NOW)["reason"], "group_not_explicitly_directed")


    def test_provider_failure_malformed_and_ambiguous_never_fabricate_positive(self):
        failed = evaluate_backlog(history(), config(), chat_name="Synthetic Contact", now=NOW,
                                  post_json_fn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("private payload")))
        malformed = evaluate_backlog(history(), config(), chat_name="Synthetic Contact", now=NOW,
                                     post_json_fn=lambda *a, **k: {"choices": [{"text": "reply"}]})
        unsure = evaluate_backlog(history(), config(), chat_name="Synthetic Contact", now=NOW,
                                  post_json_fn=responder("uncertain"))
        self.assertIsNone(failed["should_reply"])
        self.assertTrue(failed["warning"])
        self.assertNotIn("private payload", failed["warning"])
        self.assertIsNone(malformed["should_reply"])
        self.assertIsNone(unsure["should_reply"])


if __name__ == "__main__":
    unittest.main()
