import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from desk.auto_reply import AutoReplyGuard


class AutoReplyGuardTests(unittest.TestCase):
    def setUp(self):
        self.guard = AutoReplyGuard()
        self.session = {"id": "contact-1", "type": "contact"}
        self.message = {"id": "m-1", "incoming": True, "text": "合成测试消息"}
        self.event = {"id": "e-1", "incoming": True, "timestamp": 1000}
        self.config = {"auto_reply_enabled": True}
        self.policy = {"enabled": True, "session_ids": ["contact-1"],
                       "group_mode": "mention_only", "debounce_seconds": 3,
                       "cooldown_seconds": 60, "hourly_limit": 10,
                       "daily_limit": 30}

    def observe(self, **changes):
        policy = dict(self.policy)
        policy.update(changes)
        return self.guard.observe(self.session, self.message, self.event,
                                  self.config, policy, now=1000)

    def test_opt_in_debounce_then_single_claim(self):
        observed = self.observe()
        self.assertTrue(observed.allow)
        self.assertEqual(observed.ready_at, 1003)
        policy = dict(self.policy)
        early = self.guard.claim(self.session, self.message, self.event,
                                 self.config, policy, "好的", now=1002)
        self.assertEqual(early.reason, "debounce_pending")
        claimed = self.guard.claim(self.session, self.message, self.event,
                                   self.config, policy, "好的", now=1003)
        self.assertTrue(claimed.allow)
        repeated = self.guard.claim(self.session, self.message, self.event,
                                    self.config, policy, "好的", now=1004)
        self.assertEqual(repeated.reason, "event_not_pending")

    def test_disabled_or_unlisted_session_denied(self):
        self.assertEqual(self.observe(enabled=False).reason, "disabled")
        self.assertEqual(self.observe(session_ids=[]).reason, "session_not_opted_in")

    def test_unknown_session_kind_denied(self):
        self.session["type"] = "mystery"
        self.assertEqual(self.observe().reason, "unknown_session_type")

    def test_group_requires_reliable_directed_flag(self):
        self.session.update(id="group-1", type="group")
        self.policy["session_ids"] = ["group-1"]
        self.assertEqual(self.observe().reason, "group_not_directed_to_me")
        self.message["directed_to_me"] = True
        self.assertTrue(self.observe().allow)

    def test_all_group_mode_still_requires_explicit_opt_in(self):
        self.session.update(id="group-1", type="group")
        self.policy["session_ids"] = ["group-1"]
        self.assertTrue(self.observe(group_mode="all").allow)
        self.assertEqual(self.observe(group_mode="all").reason, "duplicate_event")

    def test_historical_self_old_and_ocr_events_denied(self):
        self.event["historical"] = True
        self.assertEqual(self.observe().reason, "historical_event")
        self.event.pop("historical")
        self.message["is_self"] = True
        self.assertEqual(self.observe().reason, "self_message")
        self.message.pop("is_self")
        self.event["source"] = "ocr_history"
        self.assertEqual(self.observe().reason, "non_live_source")
        self.event["source"] = "live"
        self.event["timestamp"] = 1
        self.assertEqual(self.observe().reason, "old_event")

    def test_current_visible_ocr_is_allowed_only_with_explicit_freshness(self):
        self.event["source"] = "ocr"
        self.event["live_visible"] = True
        self.event["historical"] = False
        self.message["historical"] = False
        self.assertTrue(self.observe().allow)

        guard = AutoReplyGuard()
        self.event["event_id"] = "e-next"
        self.message["id"] = "m-next"
        del self.message["historical"]
        self.assertEqual(guard.observe(self.session, self.message, self.event,
                                       self.config, self.policy, now=1000).reason,
                         "non_live_source")

    def test_duplicate_events_and_snapshot_restore(self):
        self.assertTrue(self.observe().allow)
        restored = AutoReplyGuard(self.guard.snapshot())
        self.assertEqual(restored.observe(self.session, self.message, self.event,
                                          self.config, self.policy, now=1000).reason,
                         "duplicate_event")

    def test_send_limits_and_cooldown(self):
        self.assertTrue(self.observe().allow)
        policy = dict(self.policy, cooldown_seconds=60)
        self.assertTrue(self.guard.claim(self.session, self.message, self.event,
                                         self.config, policy, "回复", now=1003).allow)
        self.message["id"] = "m-2"
        self.event["id"] = "e-2"
        self.event["timestamp"] = 1058
        self.assertTrue(self.guard.observe(self.session, self.message, self.event,
                                           self.config, policy, now=1058).allow)
        self.assertEqual(self.guard.claim(self.session, self.message, self.event,
                                          self.config, policy, "回复", now=1061).reason,
                         "cooldown")

    def test_candidate_safety(self):
        self.assertEqual(self.guard.assess_candidate("  ").reason, "empty_candidate")
        self.assertEqual(self.guard.assess_candidate("api_key=abc123").reason,
                         "sensitive_output")
        self.assertEqual(self.guard.assess_candidate("请打开 https://example.invalid/test").reason,
                         "external_link")
        self.assertEqual(self.guard.assess_candidate("x" * 181).reason,
                         "candidate_too_long")
        self.assertTrue(self.guard.assess_candidate("收到，我稍后回复").allow)

    def test_one_time_backlog_claim_shares_limits_and_never_replays(self):
        historical = {"id": "old-1", "side": "other", "text": "还需要回复吗？", "historical": True}
        policy = dict(self.policy, enabled=False)
        self.assertEqual(self.guard.claim_backlog(self.session, historical, policy, "好的", now=1000).reason,
                         "backlog_not_acknowledged")
        self.assertTrue(self.guard.claim_backlog(self.session, historical, policy, "好的",
                                                 acknowledged=True, now=1000).allow)
        self.assertEqual(self.guard.claim_backlog(self.session, historical, policy, "好的",
                                                   acknowledged=True, now=1001).reason,
                         "duplicate_event")
        next_message = {**historical, "id": "old-2"}
        self.assertEqual(self.guard.claim_backlog(self.session, next_message, policy, "好的",
                                                   acknowledged=True, now=1002).reason,
                         "cooldown")
        restored = AutoReplyGuard(self.guard.snapshot())
        self.assertEqual(restored.claim_backlog(self.session, historical, policy, "好的",
                                                acknowledged=True, now=1100).reason,
                         "duplicate_event")

    def test_backlog_cannot_resend_a_live_event_already_seen(self):
        self.message['side'] = 'other'
        self.assertTrue(self.observe().allow)
        policy = dict(self.policy, enabled=False)
        self.assertEqual(self.guard.claim_backlog(self.session, self.message, policy, '收到',
                                                  acknowledged=True, now=1004).reason,
                         'duplicate_event')


if __name__ == "__main__":
    unittest.main()
