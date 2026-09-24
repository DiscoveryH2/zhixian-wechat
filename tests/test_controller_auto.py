"""Offline Qt orchestration tests for opt-in reply-only sending."""
import sys
import tempfile
import time
import unittest
from threading import Event
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtWidgets import QApplication
from desk.controller import Controller, AUTO_DISCLOSURE

APP = QApplication.instance() or QApplication([])


class AutoControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.native = patch('desk.capture_service.CaptureService')
        self.capture_type = self.native.start()
        self.ctrl = Controller(Path(self.temp.name) / 'data')
        self.ctrl.store.secrets['api_key'] = 'synthetic-only'
        self.sid = 'ocr:synthetic-contact'
        self.ctrl._on_capture('session', {'id': self.sid, 'title': 'Synthetic contact', 'source': 'ocr'})
        self.ctrl._on_capture('messages', {'session_id': self.sid, 'title': 'Synthetic contact',
            'source': 'ocr', 'historical': True, 'messages': [{
                'id': 'msg-prior', 'side': 'me', 'sender': '我', 'kind': 'text',
                'text': 'Prior context.', 'source': 'ocr', 'historical': True, 'timestamp': None}]})
        self.ctrl.handle('configure_auto_reply', {'allowlist': [
            {'session_id': self.sid, 'type': 'private'}],
            'debounce_seconds': 2, 'cooldown_seconds': 15,
            'hourly_limit': 2, 'daily_limit': 3, 'group_mode': 'mention_only'})

    def tearDown(self):
        self.ctrl.close()
        APP.processEvents()
        self.native.stop()
        self.temp.cleanup()

    def _incoming(self, ident='msg-new', text='Can we discuss the draft?'):
        self.ctrl._on_capture('messages', {'session_id': self.sid, 'title': 'Synthetic contact',
            'source': 'ocr', 'historical': False,
            'tail_baseline_verified': True, 'tail_verified': True, 'messages': [{
                'id': ident, 'side': 'other', 'sender': 'Synthetic contact', 'kind': 'text',
                'text': text, 'source': 'ocr', 'historical': False, 'timestamp': None}]})

    def _drain(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            APP.processEvents()
            if predicate():
                return
            time.sleep(.01)
        self.fail('Queued Qt callback did not arrive')

    def _make_ready(self):
        for pending in self.ctrl.auto_guard._pending.values():
            pending['ready_at'] = time.time() - 1
        if self.ctrl.auto_trigger:
            self.ctrl.auto_trigger['ready_at'] = time.time() - 1

    def _prepared(self, decision=None, analysis=None):
        decision = decision or {'should_reply': True, 'target_message_id': 'msg-new', 'confidence': 0.95}
        analysis = analysis or {'risk': 1, 'should_reply': True, 'best_index': 0,
                                'candidates': [{'text': 'Yes, let us discuss it.'}]}
        return {**analysis, '_auto_decision': decision}

    def test_default_off_and_start_requires_explicit_acknowledgment(self):
        self.assertFalse(self.ctrl.snapshot()['auto_reply']['enabled'])
        with self.assertRaises(ValueError):
            self.ctrl.handle('start_auto_reply', {})
        self._incoming()
        self.assertIsNone(self.ctrl.auto_trigger)
        self.assertFalse(self.capture_type.return_value.send.called)

    def test_debounce_defers_model_until_new_message_settles(self):
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self._incoming()
        with patch.object(self.ctrl.models, 'submit') as model:
            self.ctrl._run_pending()
        model.assert_not_called()
        self.assertEqual(self.ctrl.pending, self.sid)
        self.assertTrue(self.ctrl.timer.isActive())

    def test_new_incoming_during_send_is_reconsidered_after_send_completes(self):
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self.ctrl.auto_send_busy = True
        self._incoming(ident='arrived-during-send')
        self.assertIsNone(self.ctrl.auto_trigger)
        self.assertEqual(self.ctrl.auto_deferred['message_id'], 'arrived-during-send')
        self.ctrl._on_auto_sent(self.sid, {'result': {'status': 'sent'}}, None)
        self.assertFalse(self.ctrl.auto_send_busy)
        self.assertIsNone(self.ctrl.auto_deferred)
        self.assertEqual(self.ctrl.auto_trigger['message']['id'], 'arrived-during-send')
        self.capture_type.return_value.send.assert_not_called()

    def test_only_new_allowed_message_claims_once_and_calls_verified_sender(self):
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self._incoming()
        self.assertIsNotNone(self.ctrl.auto_trigger)
        self._make_ready()
        self.capture_type.return_value.send.return_value = {'success': True, 'status': 'sent'}
        with patch.object(self.ctrl, '_prepare_live_auto_task', return_value=self._prepared()):
            self.ctrl._run_pending()
            self._drain(lambda: self.ctrl.auto_recent and self.ctrl.auto_recent[-1]['status'] == 'sent')
        self.capture_type.return_value.send.assert_called_once_with(
            'Yes, let us discuss it.' + AUTO_DISCLOSURE, 'Synthetic contact', 'Can we discuss the draft?', '', 'Prior context.')
        self.assertEqual(self.ctrl.snapshot()['auto_reply']['sent_hour'], 1)
        self.assertNotIn('Yes, let us discuss it.', str(self.ctrl.snapshot()['auto_reply']['recent']))
        self._incoming()  # same stable message ID is a replay, never a second send
        self.assertEqual(self.capture_type.return_value.send.call_count, 1)

    def test_model_result_without_safe_judgment_never_sends(self):
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self._incoming()
        self._make_ready()
        bad = self._prepared(analysis={'risk': None, 'should_reply': True, 'best_index': None,
                                       'candidates': [{'text': 'Maybe.'}]})
        with patch.object(self.ctrl, '_prepare_live_auto_task', return_value=bad):
            self.ctrl._run_pending()
            self._drain(lambda: bool(self.ctrl.auto_recent))
        self.capture_type.return_value.send.assert_not_called()
        self.assertEqual(self.ctrl.auto_recent[-1]['status'], 'skipped')

    def test_typed_skip_never_sends_or_needs_analysis(self):
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self._incoming()
        self._make_ready()
        skipped = {'_auto_decision': {'should_reply': False, 'target_message_id': 'msg-new',
                                     'confidence': 1.0, 'reason': 'not_needed'}}
        with patch.object(self.ctrl, '_prepare_live_auto_task', return_value=skipped):
            self.ctrl._run_pending()
            self._drain(lambda: bool(self.ctrl.auto_recent))
        self.capture_type.return_value.send.assert_not_called()
        self.assertEqual(self.ctrl.auto_recent[-1]['status'], 'skipped')

    def test_typed_yes_allows_safe_holding_reply_even_if_analysis_says_no(self):
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self._incoming()
        self._make_ready()
        prepared = self._prepared(analysis={'risk': 1, 'should_reply': False, 'best_index': 0,
                                             'candidates': [{'text': 'I will check and get back to you.'}]})
        self.capture_type.return_value.send.return_value = {'success': True, 'status': 'sent'}
        with patch.object(self.ctrl, '_prepare_live_auto_task', return_value=prepared):
            self.ctrl._run_pending()
            self._drain(lambda: self.ctrl.auto_recent and self.ctrl.auto_recent[-1]['status'] == 'sent')
        self.capture_type.return_value.send.assert_called_once()
        self.assertEqual(self.capture_type.return_value.send.call_args.args[0],
                         'I will check and get back to you.' + AUTO_DISCLOSURE)

    def test_analysis_non_reply_or_midlevel_risk_never_sends(self):
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        for ident, risk in [('risk-four', 4)]:
            with self.subTest(ident=ident):
                self._incoming(ident=ident)
                self._make_ready()
                prepared = self._prepared({'should_reply': True, 'target_message_id': ident,
                                           'confidence': 0.95},
                    {'risk': risk, 'should_reply': True, 'best_index': 0,
                     'candidates': [{'text': 'Synthetic candidate.'}]})
                before = len(self.ctrl.auto_recent)
                with patch.object(self.ctrl, '_prepare_live_auto_task', return_value=prepared):
                    self.ctrl._run_pending()
                    self._drain(lambda: len(self.ctrl.auto_recent) > before)
                self.assertEqual(self.ctrl.auto_recent[-1]['status'], 'skipped')
        self.capture_type.return_value.send.assert_not_called()

    def test_disclosure_is_appended_exactly_once_when_model_includes_it(self):
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self._incoming(ident='msg-disclosure')
        self._make_ready()
        prepared = self._prepared({'should_reply': True, 'target_message_id': 'msg-disclosure',
                                   'confidence': 0.95},
            {'risk': 0, 'should_reply': True, 'best_index': 0,
                             'candidates': [{'text': 'Okay.' + AUTO_DISCLOSURE}]})
        self.capture_type.return_value.send.return_value = {'success': True, 'status': 'sent'}
        with patch.object(self.ctrl, '_prepare_live_auto_task', return_value=prepared):
            self.ctrl._run_pending()
            self._drain(lambda: self.ctrl.auto_recent and self.ctrl.auto_recent[-1]['status'] == 'sent')
        sent_text = self.capture_type.return_value.send.call_args.args[0]
        self.assertEqual(sent_text, 'Okay.' + AUTO_DISCLOSURE)
        self.assertEqual(sent_text.count(AUTO_DISCLOSURE), 1)

    def test_ocr_group_needs_explicit_all_mode_without_reliable_mention_flag(self):
        self.ctrl.handle('configure_auto_reply', {'allowlist': [
            {'session_id': self.sid, 'type': 'group'}], 'group_mode': 'mention_only'})
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self._incoming(ident='group-1')
        self.assertIsNone(self.ctrl.auto_trigger)
        self.ctrl.handle('configure_auto_reply', {'allowlist': [
            {'session_id': self.sid, 'type': 'group'}], 'group_mode': 'all'})
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self._incoming(ident='group-2', text='Can this group test be answered?')
        self.assertEqual(self.ctrl.auto_trigger['message']['id'], 'group-2')

    def test_group_all_skips_when_typed_confidence_is_low(self):
        self.ctrl.handle('configure_auto_reply', {'allowlist': [
            {'session_id': self.sid, 'type': 'group'}], 'group_mode': 'all'})
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self._incoming(ident='group-question', text='Does anybody know this?')
        self._make_ready()
        prepared = self._prepared({'should_reply': True, 'target_message_id': 'group-question',
                                   'confidence': 0.7},
            {'risk': 1, 'should_reply': True, 'best_index': 0,
             'candidates': [{'text': 'Synthetic answer.'}]})
        with patch.object(self.ctrl, '_prepare_live_auto_task', return_value=prepared):
            self.ctrl._run_pending()
            self._drain(lambda: bool(self.ctrl.auto_recent))
        self.capture_type.return_value.send.assert_not_called()
        self.assertEqual(self.ctrl.auto_recent[-1]['status'], 'skipped')

    def test_group_short_or_repeated_ocr_text_never_starts_auto_send(self):
        self.ctrl.handle('configure_auto_reply', {'allowlist': [
            {'session_id': self.sid, 'type': 'group'}], 'group_mode': 'all'})
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self._incoming(ident='short-group', text='能用吗')
        self.assertIsNone(self.ctrl.auto_trigger)
        self._incoming(ident='long-group', text='请问这个功能现在能用吗？')
        self.assertEqual(self.ctrl.auto_trigger['message']['id'], 'long-group')
        self._incoming(ident='repeat-group', text='请问这个功能现在能用吗？')
        self.assertIsNone(self.ctrl.auto_trigger)
        self.capture_type.return_value.send.assert_not_called()

    def test_emergency_stop_invalidates_inflight_analysis(self):
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self._incoming()
        self._make_ready()
        release = Event()
        def prepare(*args):
            release.wait(1)
            return self._prepared()
        with patch.object(self.ctrl, '_prepare_live_auto_task', side_effect=prepare):
            self.ctrl._run_pending()
            self.ctrl.handle('stop_auto_reply', {'emergency': True})
            release.set()
            self._drain(lambda: self.ctrl.inflight is None)
        self.capture_type.return_value.send.assert_not_called()
        self.assertFalse(self.ctrl.snapshot()['auto_reply']['enabled'])

    def test_imported_archive_cannot_be_allowed(self):
        with self.assertRaises(ValueError):
            self.ctrl.handle('configure_auto_reply', {'allowlist': [
                {'session_id': 'import:synthetic', 'type': 'private'}]})

    def test_non_current_ocr_session_cannot_be_armed(self):
        self.ctrl._on_capture('session', {'id': 'ocr:another', 'title': 'Another chat', 'source': 'ocr'})
        with self.assertRaises(ValueError):
            self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})

    def test_ambiguous_ocr_viewport_pauses_instead_of_rebinding_same_title(self):
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self._incoming(ident='current-new')
        self.assertIsNotNone(self.ctrl.auto_trigger)
        self.ctrl._on_capture('messages', {'session_id': self.sid, 'title': 'Synthetic contact',
            'source': 'ocr', 'historical': True, 'messages': [{
                'id': 'different-viewport', 'side': 'other', 'sender': 'Synthetic contact',
                'text': 'Unrelated old context', 'source': 'ocr', 'historical': True}]})
        self.assertTrue(self.ctrl.auto_paused)
        self.assertIsNone(self.ctrl.auto_trigger)
        self.capture_type.return_value.send.assert_not_called()

    def test_stable_background_viewport_rebases_without_sending_old_batch(self):
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self._incoming(ident='before-gap')
        self.assertIsNotNone(self.ctrl.auto_trigger)
        self.ctrl._on_capture('messages', {'session_id': self.sid, 'title': 'Synthetic contact',
            'source': 'ocr', 'historical': True, 'background_stable': True,
            'tail_baseline_verified': True, 'tail_verified': True, 'messages': [
                {'id': 'new-baseline-1', 'side': 'me', 'text': 'Visible self context',
                 'source': 'ocr', 'historical': True},
                {'id': 'new-baseline-2', 'side': 'other', 'sender': 'Synthetic contact',
                 'text': 'Visible incoming baseline', 'source': 'ocr', 'historical': True}]})
        self.assertFalse(self.ctrl.auto_paused)
        self.assertIsNone(self.ctrl.auto_trigger)
        self.assertEqual(self.ctrl.sessions[self.sid]['messages'][-1]['id'], 'new-baseline-2')
        self.capture_type.return_value.send.assert_not_called()
        self._incoming(ident='after-gap', text='A newer question?')
        self.assertEqual(self.ctrl.auto_trigger['message']['id'], 'after-gap')

    def test_partial_overlap_scroll_without_tail_anchor_pauses(self):
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self.ctrl._on_capture('messages', {'session_id': self.sid, 'title': 'Synthetic contact',
            'source': 'ocr', 'historical': False, 'tail_baseline_verified': False,
            'tail_verified': False, 'messages': [{
                'id': 'old-scrolled-bubble', 'side': 'other', 'sender': 'Synthetic contact',
                'text': 'An older unrelated message', 'source': 'ocr', 'historical': False}]})
        self.assertTrue(self.ctrl.auto_paused)
        self.assertIsNone(self.ctrl.auto_trigger)
        self.capture_type.return_value.send.assert_not_called()

    def test_allowlist_persists_but_sender_never_auto_starts_after_restart(self):
        self.ctrl.handle('start_auto_reply', {'acknowledge_send': True})
        self.ctrl.close()
        another = Controller(Path(self.temp.name) / 'data')
        try:
            state = another.snapshot()['auto_reply']
            self.assertFalse(state['enabled'])
            self.assertEqual(state['allowlist'][0]['session_id'], self.sid)
            self.assertEqual(state['group_mode'], 'mention_only')
        finally:
            another.close()

    def test_corrupt_persisted_rate_state_blocks_rearming(self):
        self.ctrl.close()
        target = Path(self.temp.name) / 'data/auto-reply.json'
        target.write_text('{corrupt', encoding='utf-8')
        another = Controller(Path(self.temp.name) / 'data')
        try:
            self.assertFalse(another.snapshot()['auto_reply']['enabled'])
            with self.assertRaisesRegex(ValueError, '状态损坏'):
                another.handle('start_auto_reply', {'acknowledge_send': True})
        finally:
            another.close()


if __name__ == '__main__':
    unittest.main()
