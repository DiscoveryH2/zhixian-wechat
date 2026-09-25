"""Application orchestration. All session mutations run on the Qt application thread."""
from __future__ import annotations

import copy
import base64
import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QFileDialog

from .store import Store, _atomic, _load, normalize_title
from .auto_reply import AutoReplyGuard
from .imports import ChatLabImporter
from .datahub import DataHub
from .wechat_db_source import WeChatDBSource
from .wechat_sqlcipher import discover_cipher_source
from .version import VERSION
from .tasks import ModelTasks, HelperTasks

AUTO_DISCLOSURE = '（以上内容为知弦生成）'


class Controller(QObject):
    stateChanged = Signal(dict)
    toast = Signal(str, str)
    captureEvent = Signal(str, dict)
    analysisDone = Signal(str, int, str, object, object)
    windowAction = Signal(str, object)
    dataEvent = Signal(str, object)
    sessionLoaded = Signal(str, object, object)
    importFinished = Signal(object, object)
    mediaFinished = Signal(str, str, object, object)
    autoSendFinished = Signal(str, object, object)
    catchUpPrepared = Signal(int, str, str, object, object)
    catchUpSent = Signal(int, str, object, object)

    def __init__(self, directory: Path):
        super().__init__()
        from .capture_service import CaptureService
        self.store = Store(directory)
        self.imports = ChatLabImporter(directory)
        self.hub = DataHub(on_update=lambda kind, data: self.dataEvent.emit(kind, data))
        self.capture = CaptureService(lambda k, p: self.captureEvent.emit(k, p),
                                      Path(directory) / 'send-audit.jsonl')
        self.pool = HelperTasks()
        self.models = ModelTasks()
        self.sessions = {s['id']: s for s in self.store.load_history() if isinstance(s, dict) and s.get('id')}
        self.results, self.result_fingerprints = {}, {}
        self.current_id = next(iter(self.sessions), None)
        self.live_id = None
        self.follow_live = True
        self.catalog = {'source': 'local', 'scope': 'collected_only', 'available': bool(self.imports.count()),
                        'loaded': len(self.sessions), 'imported': self.imports.count(), 'preview_updates': []}
        self.catalog_cursors = OrderedDict()
        self.catalog_lock = threading.RLock()
        self.catalog_items = OrderedDict()
        self.moments = {'items': [], 'available': False, 'source': 'local', 'warning': '尚无朋友圈数据源'}
        self.manual_moments = OrderedDict()
        self.moment_posts = OrderedDict()
        self.media_cache = OrderedDict()
        self.media_inputs = OrderedDict()
        self.background_url = self._background_url()
        self.status = {'capture': 'idle', 'analysis': 'idle', 'detail': '配置接口后，开始读取微信本机数据库。',
                       'last_error': self.store.error, 'source': self.store.config['source'], 'connected': False}
        self.revision, self.job_serial = 0, 0
        self.inflight = None
        self.pending = None
        self.pending_manual = False
        self.auto_file = self.store.directory / 'auto-reply.json'
        saved_auto = _load(self.auto_file, {})
        saved_auto = saved_auto if isinstance(saved_auto, dict) else {}
        guard_state = saved_auto.get('guard')
        self.auto_integrity_error = self.auto_file.exists() and not (
            saved_auto.get('version') == 1 and isinstance(guard_state, dict) and
            isinstance(guard_state.get('seen'), list) and isinstance(guard_state.get('sent'), list) and
            isinstance(saved_auto.get('allowlist'), list))
        try:
            self.auto_guard = AutoReplyGuard(guard_state)
        except (TypeError, ValueError):
            self.auto_integrity_error = True
            self.auto_guard = AutoReplyGuard()
        self.auto_policy = self._auto_policy_from(saved_auto.get('policy'))
        self.auto_policy['enabled'] = False  # Every launch requires an explicit opt-in.
        self.auto_allowlist = [entry for entry in (saved_auto.get('allowlist') or [])
                               if isinstance(entry, dict) and entry.get('session_id') and entry.get('type') in ('private', 'group')][:50]
        self.auto_paused = False
        self.auto_detail = ('自动回复状态文件无法可靠读取；请重新保存名单并重启应用后再启用。'
                            if self.auto_integrity_error else
                            '默认关闭。选择会话并明确启用后，只回复新收到的消息。')
        self.auto_recent = []
        self.auto_trigger = None
        self.auto_running_trigger = None
        self.auto_epoch = 0
        self.auto_send_busy = False
        self.auto_deferred = None
        self.catchup = {'session_id': None, 'status': 'idle', 'reason': ''}
        self.catchup_serial = 0
        self.catchup_busy = False
        self.closed = False
        self.allow_initial = False
        self.paused_by_user = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._run_pending)
        self.captureEvent.connect(self._on_capture)
        self.analysisDone.connect(self._on_analysis)
        self.dataEvent.connect(self._on_data)
        self.sessionLoaded.connect(self._on_session_loaded)
        self.importFinished.connect(self._on_import_finished)
        self.mediaFinished.connect(self._on_media_finished)
        self.autoSendFinished.connect(self._on_auto_sent)
        self.catchUpPrepared.connect(self._on_catchup_prepared)
        self.catchUpSent.connect(self._on_catchup_sent)
        self._sync_hub()
        if self.store.config['source'] in ('weflow', 'auto') and self.store.secrets['weflow_token']:
            self.hub.prewarm(self.store.full_config(), limit=5)

    def _background_url(self):
        target = self.store.directory / 'background.jpg'
        if not target.is_file() or target.stat().st_size > 1024 * 1024:
            return ''
        return 'data:image/jpeg;base64,' + base64.b64encode(target.read_bytes()).decode('ascii')

    def _appearance(self):
        return {'theme': self.store.config['theme'], 'font_scale': self.store.config['font_scale'],
                'background_url': self.background_url}

    @staticmethod
    def _auto_policy_from(value):
        data = value if isinstance(value, dict) else {}
        def bounded(name, default, minimum, maximum):
            try:
                return min(maximum, max(minimum, int(data.get(name, default))))
            except (TypeError, ValueError):
                return default
        return {'enabled': False, 'session_ids': [],
                'group_mode': data.get('group_mode') if data.get('group_mode') in ('mention_only', 'all') else 'mention_only',
                'debounce_seconds': bounded('debounce_seconds', 3, 2, 15),
                'cooldown_seconds': bounded('cooldown_seconds', 60, 15, 3600),
                'hourly_limit': bounded('hourly_limit', 10, 1, 30),
                'daily_limit': bounded('daily_limit', 30, 1, 100)}

    def _auto_public(self):
        now = time.time()
        sent = self.auto_guard.snapshot().get('sent', [])
        enabled = self.auto_policy['enabled']
        visible_allowed = (any(item.get('source') == 'wechat_db' for item in self.auto_allowlist)
                           if self.store.config['source'] == 'wechat_db' else
                           any(item['session_id'] == self.live_id for item in self.auto_allowlist))
        waiting_for_target = enabled and not self.auto_paused and self.status['capture'] == 'live' and not visible_allowed
        status = ('off' if not enabled else 'paused' if self.auto_paused else
                  'running' if self.status['capture'] == 'live' and visible_allowed else 'blocked')
        detail = ('微信当前显示的会话不在自动回复名单中；请切回允许会话并保持聊天尾部可见。'
                  if waiting_for_target else self.auto_detail)
        return {'enabled': enabled, 'paused': self.auto_paused, 'status': status,
                'detail': detail, 'waiting_for_target': waiting_for_target,
                'allowlist': copy.deepcopy(self.auto_allowlist),
                'group_mode': self.auto_policy['group_mode'],
                'debounce_seconds': self.auto_policy['debounce_seconds'],
                'cooldown_seconds': self.auto_policy['cooldown_seconds'],
                'hourly_limit': self.auto_policy['hourly_limit'],
                'daily_limit': self.auto_policy['daily_limit'],
                'sent_hour': sum(1 for item in sent if now - item['at'] < 3600),
                'sent_day': sum(1 for item in sent if now - item['at'] < 86400),
                'recent': copy.deepcopy(self.auto_recent[-12:]),
                'catchup': copy.deepcopy(self.catchup)}

    def _save_auto_state(self):
        # Never restore an armed sender after process restart. This file contains
        # only local session metadata and deduplication IDs, no message text.
        stored = {**self.auto_policy, 'enabled': False}
        stored['session_ids'] = []
        guard = self.auto_guard.snapshot()
        guard['pending'] = {}
        _atomic(self.auto_file, {'version': 1, 'policy': stored,
                                  'allowlist': self.auto_allowlist, 'guard': guard})

    def _auto_record(self, sid, status, reason):
        session = self.sessions.get(sid) or {}
        chosen = next((item for item in self.auto_allowlist if item['session_id'] == sid), {})
        self.auto_recent.append({'id': uuid.uuid4().hex, 'title': str(session.get('title') or chosen.get('title') or '会话')[:100],
                                 'is_group': chosen.get('type') == 'group', 'at': time.time(),
                                 'status': status, 'reason': str(reason)[:160]})
        self.auto_recent = self.auto_recent[-20:]

    def _cancel_auto_work(self):
        self.auto_epoch += 1
        self.auto_trigger = None
        self.auto_running_trigger = None
        self.auto_deferred = None
        if self.catchup_busy:
            self.catchup_serial += 1
            self.catchup_busy = False
            self.catchup = {**self.catchup, 'status': 'skipped', 'reason': '一次性历史补回已停止。'}
        if hasattr(self.capture, 'cancel_pending_writes'):
            self.capture.cancel_pending_writes()

    def _configure_auto(self, params):
        entries = params.get('allowlist')
        if not isinstance(entries, list) or len(entries) > 50:
            raise ValueError('请从会话目录选择不超过 50 个联系人或群聊。')
        resolved, seen = [], set()
        for raw in entries:
            if not isinstance(raw, dict):
                raise ValueError('自动回复会话配置无效。')
            sid = str(raw.get('session_id') or '')[:256]
            if not sid:
                continue
            legacy = next((item for item in self.auto_allowlist if item['session_id'] == sid
                           and item.get('source') == 'ocr'), None)
            if legacy and self.store.config['source'] == 'wechat_db':
                meta = self._migrate_ocr_auto_target(legacy)
                sid = meta['id']
            else:
                with self.catalog_lock:
                    meta = copy.deepcopy(self.catalog_items.get(sid))
                meta = meta or copy.deepcopy(self.sessions.get(sid))
                saved = next((item for item in self.auto_allowlist if item['session_id'] == sid
                              and item.get('source') == 'wechat_db'), None)
                if not meta and saved and self.store.config['source'] == 'wechat_db':
                    meta = self._verify_saved_db_auto_target(saved)
            if sid in seen:
                continue
            if not meta or meta.get('source') not in ('ocr', 'weflow', 'wechat_db'):
                raise ValueError('自动回复只能选择实时微信会话，不能选择导入归档。')
            if meta.get('source') == 'ocr' and (sid != self.live_id or self.status['capture'] != 'live'):
                raise ValueError('OCR 自动回复只能选择当前微信正在显示的会话。')
            if meta.get('source') in ('weflow', 'wechat_db'):
                talker = str(meta.get('talker') or meta.get('id') or '')
                kind = 'group' if talker.endswith('@chatroom') else meta.get('type')
                if kind not in ('private', 'group'):
                    raise ValueError('数据库或 WeFlow 未提供可靠的会话类型，已拒绝自动发送。')
            else:
                kind = raw.get('type')
                if kind not in ('private', 'group'):
                    raise ValueError('请明确确认当前 OCR 会话是单聊还是群聊。')
            resolved.append({'session_id': sid, 'title': str(meta.get('title') or '')[:200],
                             'type': kind, 'is_group': kind == 'group', 'source': meta['source']})
            seen.add(sid)
        if len({entry['source'] for entry in resolved}) > 1:
            raise ValueError('自动回复一次只能使用一种实时消息来源，不能混合名单。')
        self._cancel_auto_work()
        self.auto_allowlist = resolved
        self.auto_policy = self._auto_policy_from(params)
        self.auto_policy['session_ids'] = [entry['session_id'] for entry in resolved]
        self.auto_paused = False
        self.auto_detail = '配置已保存，默认关闭；启用后会向微信当前可核验的会话直接发送回复。'
        try:
            self._save_auto_state()
        except OSError:
            self.auto_policy['enabled'] = False
            self.auto_detail = '无法保存自动回复状态，已拒绝启动。'
            raise
        self.emit()
        return self._auto_public()

    def _migrate_ocr_auto_target(self, legacy):
        """Upgrade one old title-only OCR allowlist entry to a unique DB ID."""
        title = str(legacy.get('title') or '').strip()
        expected_type = legacy.get('type')
        if not title or expected_type not in ('private', 'group'):
            raise ValueError('旧 OCR 名单缺少会话名称或类型，请从数据库会话目录重新选择。')
        try:
            source = self._wechat_db_reader()
            matches, offset = [], 0
            while offset < 100000:
                page = source.sessions(limit=500, offset=offset)
                matches.extend(item for item in page
                               if item.get('type') == expected_type
                               and normalize_title(item.get('name')) == normalize_title(title))
                if len(matches) > 1:
                    break
                offset += len(page)
                if len(page) < 500:
                    break
        except Exception:
            raise ValueError('无法核验旧 OCR 名单，请从数据库会话目录重新选择。') from None
        if len(matches) != 1:
            raise ValueError('旧 OCR 名单无法唯一对应数据库会话，请从数据库会话目录重新选择。')
        item = matches[0]
        migrated = {'id': item['id'], 'title': item.get('name') or title,
                    'type': expected_type, 'source': 'wechat_db', 'talker': item['id']}
        self._remember_catalog([migrated])
        return migrated

    def _verify_saved_db_auto_target(self, saved):
        """Resolve a persisted DB allowlist ID after a fresh app launch."""
        ident = str(saved.get('session_id') or '')
        expected_type = saved.get('type')
        if not ident or expected_type not in ('private', 'group'):
            raise ValueError('数据库自动回复名单无效，请重新选择会话。')
        try:
            source = self._wechat_db_reader()
            matches, offset = [], 0
            while offset < 100000:
                page = source.sessions(limit=500, offset=offset)
                matches.extend(item for item in page if item.get('id') == ident)
                offset += len(page)
                if len(page) < 500:
                    break
        except Exception:
            raise ValueError('无法核验数据库自动回复名单，请重新选择会话。') from None
        if len(matches) != 1 or matches[0].get('type') != expected_type:
            raise ValueError('数据库自动回复名单已过期，请重新选择会话。')
        item = matches[0]
        meta = {'id': ident, 'title': item.get('name') or ident,
                'type': expected_type, 'source': 'wechat_db', 'talker': ident}
        self._remember_catalog([meta])
        return meta

    def _start_auto(self, params):
        if self.auto_integrity_error:
            raise ValueError('自动回复去重或限额状态损坏；请重新保存名单并重启应用后再启用。')
        if params.get('acknowledge_send') is not True:
            raise ValueError('请确认：自动回复会真正发送给所选微信联系人或群聊。')
        if not self.auto_allowlist:
            raise ValueError('请先选择允许自动回复的实时会话。')
        if not self.store.secrets['api_key']:
            raise ValueError('请先配置 Jev API Key。')
        source = self.auto_allowlist[0].get('source')
        if source == 'weflow' and self.store.config['source'] != 'weflow':
            raise ValueError('请先在设置中启用已连接的 WeFlow 消息来源。')
        if source == 'wechat_db' and self.store.config['source'] != 'wechat_db':
            raise ValueError('请先在设置中启用本机微信数据库来源。')
        if source == 'ocr' and self.store.config['source'] == 'wechat_db':
            raise ValueError('旧 OCR 名单尚未迁移，请重新保存自动回复名单。')
        if source == 'ocr' and (self.auto_allowlist[0]['session_id'] != self.live_id or self.status['capture'] != 'live'):
            raise ValueError('请让已选的 OCR 会话保持在当前可见的微信窗口。')
        from core.client import resolve_decision, resolve_reply
        cfg = self.store.full_config()
        if resolve_reply(cfg, resolve_decision(cfg)) is None:
            raise ValueError('当前判断服务没有回复生成模型，请先配置生成服务。')
        self.auto_policy['session_ids'] = [entry['session_id'] for entry in self.auto_allowlist]
        self.auto_policy['enabled'] = True
        self.auto_paused = False
        self.auto_detail = ('自动回复已启用；只处理启用之后的新入站文字。数据库模式仍要求目标会话在微信窗口中可唯一核验。'
                            if source == 'wechat_db' else
                            '自动回复已启用；只处理启用之后的新入站文字，发送前还会核验微信窗口和输入框。')
        if self.status['capture'] not in ('live', 'searching'):
            self.capture.start(cfg)
            self.status.update(capture='searching', detail='正在连接微信本地数据库…' if source == 'wechat_db' else '正在定位微信窗口…')
        try:
            self._save_auto_state()
        except OSError:
            self.auto_policy['enabled'] = False
            self.auto_detail = '无法保存自动回复状态，已拒绝启动。'
            raise
        self.emit()
        return self._auto_public()

    def _stop_auto(self, emergency=False, pause=False):
        self._cancel_auto_work()
        if pause:
            self.auto_paused = True
            self.auto_detail = '自动回复已暂停；新消息不会发送。'
        else:
            self.auto_policy['enabled'] = False
            self.auto_paused = False
            self.auto_detail = '紧急停止已生效；已提交给微信的消息无法撤回。' if emergency else '自动回复已关闭。'
        self._save_auto_state()
        self.emit()
        return self._auto_public()

    def _auto_observe(self, session, message, capture_event):
        if not self.auto_policy['enabled'] or self.auto_paused:
            return None
        if message.get('side') != 'other' or message.get('kind', 'text') != 'text' or not str(message.get('text') or '').strip():
            return None
        sid = session['id']
        chosen = next((item for item in self.auto_allowlist if item['session_id'] == sid), None)
        if not chosen:
            return None
        if chosen['type'] == 'group' and not str(message.get('sender') or '').strip():
            self.auto_detail = '群聊发言人无法核对，未自动回复。'
            return None
        if capture_event.get('source') == 'ocr':
            if capture_event.get('sidebar_changed') is not True:
                self.auto_detail = '左侧会话摘要没有出现新的签名；滚动或旧消息不会触发自动回复。'
                return None
            if capture_event.get('tail_baseline_verified') is not True:
                self.auto_detail = '新摘要无法与可见聊天尾部核对，跳过本轮并继续监听。'
                return None
            if capture_event.get('tail_verified') is not True:
                self.auto_detail = '最新文字过短，未通过自动回复尾部核验；继续等待。'
                return None
            if chosen['type'] == 'group' and len(re.sub(r'\s+', '', str(message.get('text') or ''))) < 8:
                self.auto_detail = '群聊消息过短，无法排除相似旧消息，本轮未自动发送。'
                return None
            text_key = re.sub(r'\s+', '', str(message.get('text') or ''))
            if any(m.get('side') == 'other' and m.get('sender') == message.get('sender') and
                   re.sub(r'\s+', '', str(m.get('text') or '')) == text_key
                   for m in session.get('messages', [])[:-1][-100:]):
                self.auto_detail = '近期出现相同发言人和内容，无法可靠去重，本轮未自动发送。'
                return None
        if self.auto_send_busy:
            # Coalesce a burst to its newest incoming turn. The send completion
            # will recheck that this is still the latest message before judging.
            self.auto_deferred = {'session_id': sid, 'message_id': message['id'],
                                  'source': capture_event.get('source'),
                                  'sidebar_changed': capture_event.get('sidebar_changed'),
                                  'sidebar_signature': capture_event.get('sidebar_signature'),
                                  'tail_baseline_verified': capture_event.get('tail_baseline_verified'),
                                  'tail_verified': capture_event.get('tail_verified'),
                                  'observed_at': time.time()}
            self.auto_detail = '发送中收到新消息；完成后将重新核对最新一轮。'
            return None
        counters = self._auto_public()
        if counters['sent_hour'] >= self.auto_policy['hourly_limit'] or counters['sent_day'] >= self.auto_policy['daily_limit']:
            self.auto_detail = '自动回复已达到发送上限，未为这条消息调用模型。'
            return None
        typed = {**session, 'type': chosen['type']}
        source = capture_event.get('source')
        event = {'source': source, 'historical': bool(capture_event.get('historical')),
                 'incoming': True, 'live_visible': source == 'ocr' and sid == self.live_id,
                 'sidebar_signature': capture_event.get('sidebar_signature') or '',
                 'timestamp': capture_event.get('observed_at') or time.time()}
        observed = self.auto_guard.observe(typed, message, event, {'auto_reply_enabled': True}, self.auto_policy)
        if not observed.allow:
            return None
        try:
            self._save_auto_state()
        except OSError:
            self.auto_paused = True
            self.auto_detail = '无法保存新消息去重状态，自动回复已暂停。'
            return None
        self.auto_trigger = {'session_id': sid, 'message': copy.deepcopy(message), 'event': event,
                             'ready_at': observed.ready_at, 'epoch': self.auto_epoch,
                             'type': chosen['type']}
        self.auto_detail = '已检测到允许会话的新消息，正在等待对方发完并准备回复。'
        return max(0, int((observed.ready_at - time.time()) * 1000))

    def _auto_dispatch(self, trigger, result, judged):
        sid = trigger['session_id']
        if (not self.auto_policy['enabled'] or self.auto_paused or self.auto_send_busy or
                trigger['epoch'] != self.auto_epoch or
                (trigger['event'].get('source') != 'wechat_db' and sid != self.live_id) or
                self.status['capture'] != 'live'):
            self._auto_record(sid, 'skipped', '会话或自动回复状态已变化')
            return
        session = self.sessions.get(sid)
        if not session or not session.get('messages') or session['messages'][-1].get('id') != trigger['message']['id']:
            self._auto_record(sid, 'skipped', '已有更新的消息，旧候选未发送')
            return
        risk = result.get('risk')
        if isinstance(risk, bool) or not isinstance(risk, (int, float)) or not 0 <= risk <= 3:
            self._auto_record(sid, 'skipped', '风险判断较高或缺失，未自动发送')
            return
        if (judged.get('should_reply') is not True or
                judged.get('target_message_id') != trigger['message']['id']):
            self._auto_record(sid, 'skipped', 'Jev 未明确认为现在值得回复这条消息')
            return
        if (trigger['type'] == 'group' and self.auto_policy['group_mode'] == 'all'
                and trigger['message'].get('directed_to_me') is not True):
            confidence = judged.get('confidence')
            if (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or
                    confidence < 0.85):
                self._auto_record(sid, 'skipped', '群聊未明确指向我，Jev 回复置信度不足')
                return
        prior = next((str(m.get('text') or '') for m in reversed(session['messages'][:-1])
                      if m.get('kind', 'text') == 'text' and str(m.get('text') or '').strip()), '')
        if not prior:
            self._auto_record(sid, 'skipped', '缺少可核验的上一条上下文，未发送')
            return
        candidates, index = result.get('candidates') or [], result.get('best_index')
        if not isinstance(index, int) or not 0 <= index < len(candidates):
            self._auto_record(sid, 'skipped', '没有经过 Jev 排序的可用回复')
            return
        draft = str(candidates[index].get('text') or '').strip()
        reply = draft.replace(AUTO_DISCLOSURE, '').rstrip() + AUTO_DISCLOSURE
        typed = {**session, 'type': trigger['type']}
        claimed = self.auto_guard.claim(typed, trigger['message'], trigger['event'],
                                        {'auto_reply_enabled': True}, self.auto_policy,
                                        reply, incoming_text=trigger['message'].get('text', ''))
        if not claimed.allow:
            self._auto_record(sid, 'skipped', '发送边界未通过：' + claimed.reason)
            return
        # Claim before dispatch prevents reconnect/restart from sending twice.
        try:
            self._save_auto_state()
        except OSError:
            self.auto_paused = True
            self.auto_detail = '无法保存发送去重状态，自动回复已暂停。'
            self._auto_record(sid, 'failed', '发送状态未能安全保存')
            return
        self.auto_send_busy = True
        self.auto_detail = '正在用数据库核验最新来信，并检查微信标题与输入框。' if trigger['event'].get('source') == 'wechat_db' else '正在对微信当前窗口与输入框做最终核验。'
        sender = trigger['message'].get('sender', '') if trigger['type'] == 'group' else ''
        try:
            if trigger['event'].get('source') == 'wechat_db':
                future = self.pool.submit(self.capture.send_db, reply, sid, trigger['message']['id'])
            else:
                future = self.pool.submit(self.capture.send, reply, session['title'],
                                          trigger['message'].get('text', ''), sender, prior,
                                          trigger['event'].get('sidebar_signature', ''))
            future.add_done_callback(lambda done: self._emit_auto_send_done(sid, trigger['epoch'], done))
        except Exception:
            self.auto_send_busy = False
            self.auto_paused = True
            self.auto_detail = '自动发送任务未能启动，已暂停。'
            self._auto_record(sid, 'failed', '发送任务启动失败')

    def _emit_auto_send_done(self, sid, epoch, future):
        if self.closed:
            return
        if future.cancelled():
            self.autoSendFinished.emit(sid, {'epoch': epoch}, '自动发送任务已取消')
            return
        try:
            self.autoSendFinished.emit(sid, {'epoch': epoch, 'result': future.result()}, None)
        except Exception as exc:
            self.autoSendFinished.emit(sid, {'epoch': epoch}, self.safe_error(exc))

    def _on_auto_sent(self, sid, payload, error):
        if self.closed:
            return
        self.auto_send_busy = False
        result = payload.get('result') if isinstance(payload, dict) else None
        status = result.get('status') if isinstance(result, dict) else None
        if status == 'sent':
            self._auto_record(sid, 'sent', '已在目标微信会话确认发出消息')
            self.auto_detail = '上一条回复已在目标会话确认；继续等待新消息。'
        elif status == 'sent_unconfirmed':
            self.auto_paused = True
            self._auto_record(sid, 'failed', '已按下发送键，但未能确认送达；已暂停，请检查微信')
            self.auto_detail = '发送结果尚未确认，自动回复已暂停。请检查微信后再启用。'
        elif isinstance(error, str) and error.startswith('数据库发送核验：'):
            self.auto_paused = True
            detail = error.split('：', 1)[1][:160]
            self._auto_record(sid, 'failed', detail)
            self.auto_detail = detail + ' 自动回复已暂停。'
        else:
            self.auto_paused = True
            self._auto_record(sid, 'failed', '安全核验或发送失败，已暂停')
            self.auto_detail = '安全核验未通过，自动回复已暂停。'
        self._save_auto_state()
        self._resume_deferred_auto()
        self.emit()

    def _resume_deferred_auto(self):
        deferred, self.auto_deferred = self.auto_deferred, None
        if (not deferred or not self.auto_policy['enabled'] or self.auto_paused or
                self.auto_send_busy or self.status['capture'] != 'live'):
            return
        sid = deferred['session_id']
        session = self.sessions.get(sid)
        if ((deferred['source'] != 'wechat_db' and sid != self.live_id)
                or not session or not session.get('messages') or
                session['messages'][-1].get('id') != deferred['message_id']):
            return
        message = session['messages'][-1]
        if message.get('side') != 'other':
            return
        delay = self._auto_observe(session, message, {
            'source': deferred['source'], 'historical': False,
            'sidebar_changed': deferred.get('sidebar_changed'),
            'sidebar_signature': deferred.get('sidebar_signature'),
            'tail_baseline_verified': deferred.get('tail_baseline_verified'),
            'tail_verified': deferred.get('tail_verified'),
            'observed_at': deferred['observed_at']})
        if delay is not None:
            self.pending = sid
            self.pending_manual = False
            self.timer.start(max(self.store.config['debounce_ms'], delay))

    def _prepare_catchup_task(self, cfg, session, chat_type, own_display_name, group_mode):
        judged = self.models.submit('judge_backlog', {
            'messages': session['messages'], 'config': cfg, 'chat_type': chat_type,
            'chat_name': session['title'], 'own_display_name': own_display_name,
            'current_session_acknowledged': True,
            'group_mode': group_mode}).result(timeout=180)
        if judged.get('should_reply') is not True:
            return {'judged': judged, 'analysis': None}
        draft_config = dict(cfg)
        draft_config['style'] = (str(draft_config.get('style') or '')[:2000] +
                                 '\n一次性历史补回请用不超过130字的自然短句，只回答仍然需要回应的最后一轮消息。'
                                 '不编造事实或承诺；无需添加来源标识，应用会在发送前附上固定的知弦说明。')
        analyzed = self.models.submit('analyze', {'messages': session['messages'],
            'config': draft_config, 'background': '', 'history': None, 'reply_to': None}).result(timeout=180)
        return {'judged': judged, 'analysis': analyzed}

    def _prepare_live_auto_task(self, cfg, session, analysis_payload, trigger, group_mode):
        """Require a typed send-now judgment before paying for a draft."""
        judged = self.models.submit('judge_backlog', {
            'messages': session['messages'], 'config': cfg, 'chat_type': trigger['type'],
            'chat_name': session['title'], 'own_display_name': '',
            'current_session_acknowledged': True, 'group_mode': group_mode,
            'require_prior_self': False if trigger['type'] == 'group' else True}).result(timeout=180)
        if (judged.get('should_reply') is not True or
                judged.get('target_message_id') != trigger['message']['id']):
            return {'_auto_decision': judged}
        analyzed = self.models.submit('analyze', analysis_payload).result(timeout=180)
        analyzed['_auto_decision'] = judged
        return analyzed

    def _catch_up_once(self, params):
        if params.get('acknowledge_send') is not True:
            raise ValueError('请确认：一次性历史补回可能向当前微信会话直接发送一条消息。')
        if self.catchup_busy or self.auto_send_busy or (self.auto_policy['enabled'] and not self.auto_paused):
            raise ValueError('已有自动回复任务在运行，请先暂停后再执行历史补回。')
        if not self.store.secrets['api_key']:
            raise ValueError('请先配置 Jev API Key。')
        sid = str(params.get('session_id') or '')[:256]
        chosen = next((item for item in self.auto_allowlist if item['session_id'] == sid), None)
        if not chosen or chosen.get('source') not in ('ocr', 'weflow', 'wechat_db'):
            raise ValueError('历史补回只允许名单内的实时会话，导入归档不能发送。')
        session = self.sessions.get(sid)
        db_selected = chosen['source'] == 'wechat_db'
        if (not session or (not db_selected and sid != self.live_id) or self.status['capture'] != 'live' or
                session.get('source') != chosen['source'] or len(session.get('messages') or []) < 2):
            raise ValueError('请先读取目标会话至少两条可核对的上下文。')
        if session['messages'][-1].get('side') != 'other':
            raise ValueError('你已是最后发言人；没有待补回的最新来信。')
        if session['messages'][-1].get('kind', 'text') != 'text':
            raise ValueError('历史补回目前只支持最后一条为文字消息。')
        if chosen['type'] == 'group' and not str(session['messages'][-1].get('sender') or '').strip():
            raise ValueError('群聊最后发言人无法核对，已拒绝历史补回。')
        from core.client import resolve_decision, resolve_reply
        cfg = self.store.full_config()
        if resolve_reply(cfg, resolve_decision(cfg)) is None:
            raise ValueError('当前判断服务没有可用的回复生成模型。')
        own_display_name = str(params.get('own_display_name') or '').strip()[:80]
        self.auto_policy['session_ids'] = [entry['session_id'] for entry in self.auto_allowlist]
        self.catchup_serial += 1
        serial = self.catchup_serial
        frozen = copy.deepcopy(session)
        fingerprint = self.fingerprint(frozen)
        self.catchup_busy = True
        self.catchup = {'session_id': sid, 'status': 'judging',
                        'reason': '正在判断最近一轮历史来信是否仍值得回复。'}
        try:
            future = self.pool.submit(self._prepare_catchup_task, cfg, frozen, chosen['type'],
                                      own_display_name, self.auto_policy['group_mode'])
            future.add_done_callback(lambda done: self._emit_catchup_prepared(serial, sid, fingerprint, done))
        except Exception:
            self.catchup_busy = False
            self.catchup = {'session_id': sid, 'status': 'failed', 'reason': '历史判断任务未能启动。'}
            raise
        self.emit()
        return {'started': True, 'session_id': sid}

    def _emit_catchup_prepared(self, serial, sid, fingerprint, future):
        if self.closed or future.cancelled():
            return
        try:
            self.catchUpPrepared.emit(serial, sid, fingerprint, future.result(), None)
        except Exception as exc:
            self.catchUpPrepared.emit(serial, sid, fingerprint, None, self.safe_error(exc))

    def _on_catchup_prepared(self, serial, sid, fingerprint, prepared, error):
        if self.closed or serial != self.catchup_serial or not self.catchup_busy:
            return
        session = self.sessions.get(sid)
        if error or not prepared:
            self.catchup = {'session_id': sid, 'status': 'failed', 'reason': '历史判断或草稿生成未完成。'}
            self.catchup_busy = False
            self._auto_record(sid, 'failed', '历史判断或草稿生成失败')
        elif (not session or (session.get('source') != 'wechat_db' and sid != self.live_id) or self.status['capture'] != 'live' or
                self.fingerprint(session) != fingerprint):
            self.catchup = {'session_id': sid, 'status': 'skipped', 'reason': '会话或最近消息已变化，旧判断未发送。'}
            self.catchup_busy = False
            self._auto_record(sid, 'skipped', '会话或最近消息已变化')
        else:
            judged = prepared.get('judged') or {}
            analyzed = prepared.get('analysis') or {}
            target = session['messages'][-1]
            if judged.get('should_reply') is not True or judged.get('target_message_id') != target.get('id'):
                self.catchup = {'session_id': sid, 'status': 'skipped',
                                'reason': 'Jev 判断当前没有需要补回的明确消息。'}
                self.catchup_busy = False
                self._auto_record(sid, 'skipped', '历史消息不需回复或判断不充分')
            elif (analyzed.get('should_reply') is not True or
                  isinstance(analyzed.get('risk'), bool) or
                  not isinstance(analyzed.get('risk'), (int, float)) or
                  not 0 <= analyzed['risk'] <= 3):
                self.catchup = {'session_id': sid, 'status': 'skipped',
                                'reason': '回复时机或风险判断未满足自动发送条件。'}
                self.catchup_busy = False
                self._auto_record(sid, 'skipped', '回复时机或风险条件不满足')
            else:
                candidates, index = analyzed.get('candidates') or [], analyzed.get('best_index')
                if not isinstance(index, int) or not 0 <= index < len(candidates):
                    self.catchup = {'session_id': sid, 'status': 'skipped', 'reason': '没有完成 Jev 排序的候选回复。'}
                    self.catchup_busy = False
                    self._auto_record(sid, 'skipped', '没有完成候选排序')
                else:
                    reply = str(candidates[index].get('text') or '').strip().replace(AUTO_DISCLOSURE, '').rstrip() + AUTO_DISCLOSURE
                    previous = next((str(m.get('text') or '') for m in reversed(session['messages'][:-1])
                                     if m.get('kind', 'text') == 'text' and str(m.get('text') or '').strip()), '')
                    typed = {**session, 'type': next((item['type'] for item in self.auto_allowlist
                                if item['session_id'] == sid), 'unknown')}
                    claimed = self.auto_guard.claim_backlog(typed, target, self.auto_policy, reply,
                                acknowledged=True, incoming_text=target.get('text', '')) if previous else None
                    if not claimed or not claimed.allow:
                        self.catchup = {'session_id': sid, 'status': 'skipped',
                                        'reason': '重复、限额、草稿或可见上下文未通过发送边界。'}
                        self.catchup_busy = False
                        self._auto_record(sid, 'skipped', '发送边界未通过')
                    else:
                        try:
                            self._save_auto_state()
                            sender = target.get('sender', '') if typed['type'] == 'group' else ''
                            self.catchup = {'session_id': sid, 'status': 'sending',
                                            'reason': '正在核验数据库最新来信、微信标题与输入框。'}
                            if session.get('source') == 'wechat_db':
                                future = self.pool.submit(self.capture.send_db, reply, sid, target['id'])
                            else:
                                future = self.pool.submit(self.capture.send, reply, session['title'],
                                                          target.get('text', ''), sender, previous)
                            future.add_done_callback(lambda done: self._emit_catchup_sent(serial, sid, done))
                        except Exception:
                            self.catchup = {'session_id': sid, 'status': 'failed',
                                            'reason': '无法安全保存或启动发送任务。'}
                            self.catchup_busy = False
                            self._auto_record(sid, 'failed', '发送任务未启动')
        self.emit()

    def _emit_catchup_sent(self, serial, sid, future):
        if self.closed or future.cancelled():
            return
        try:
            self.catchUpSent.emit(serial, sid, future.result(), None)
        except Exception as exc:
            self.catchUpSent.emit(serial, sid, None, self.safe_error(exc))

    def _on_catchup_sent(self, serial, sid, result, error):
        if self.closed or serial != self.catchup_serial or not self.catchup_busy:
            return
        self.catchup_busy = False
        status = result.get('status') if isinstance(result, dict) else None
        if status == 'sent':
            self.catchup = {'session_id': sid, 'status': 'sent',
                            'reason': '已确认目标会话发出的消息。'}
            self._auto_record(sid, 'sent', '一次性历史补回已确认发出')
        elif status == 'sent_unconfirmed':
            self.catchup = {'session_id': sid, 'status': 'failed',
                            'reason': '已按下发送键，但未确认发出；请检查微信，系统不会重试。'}
            self._auto_record(sid, 'failed', '发送结果不确定，未重试')
        else:
            self.catchup = {'session_id': sid, 'status': 'failed',
                            'reason': '发送前核验未通过或任务失败；没有自动重试。'}
            self._auto_record(sid, 'failed', '发送前核验失败')
        self._save_auto_state()
        self.emit()

    def _sync_hub(self):
        self.hub.set_local_sessions(self.sessions)

    def _remember_catalog(self, items):
        with self.catalog_lock:
            for item in items:
                if item.get('id'):
                    self.catalog_items[item['id']] = dict(item)
                    self.catalog_items.move_to_end(item['id'])
            while len(self.catalog_items) > 5000:
                self.catalog_items.popitem(last=False)

    def _catalog_cursor(self, state):
        ident = uuid.uuid4().hex
        with self.catalog_lock:
            self.catalog_cursors[ident] = (time.monotonic(), state)
            while len(self.catalog_cursors) > 300:
                self.catalog_cursors.popitem(last=False)
        return ident

    def _read_catalog_cursor(self, value, source, query):
        with self.catalog_lock:
            entry = self.catalog_cursors.get(str(value or ''))
        if not entry or time.monotonic() - entry[0] > 600 or entry[1]['source'] != source or entry[1]['query'] != query:
            raise ValueError('会话分页已过期，请重新搜索。')
        return copy.deepcopy(entry[1])

    def _import_folder_task(self, root):
        """Enumerate and index a chosen export directory off the UI thread."""
        root = Path(root).resolve()
        files = []
        for path in root.rglob('*'):
            try:
                if (not path.is_file() or path.suffix.lower() not in ('.json', '.jsonl')
                        or not path.resolve().is_relative_to(root)
                        or path.stat().st_size > 1024 * 1024 * 1024):
                    continue
                with path.open('rb') as stream:
                    head = stream.read(4096)
                if b'"chatlab"' not in head and not (b'"_type"' in head and b'"header"' in head):
                    continue
                files.append(str(path))
                if len(files) > 5000:
                    raise ValueError('目录中超过 5000 份聊天文件，请分批导入。')
            except OSError:
                continue
        if not files:
            raise ValueError('目录中没有可识别的 ChatLab JSON/JSONL 文件。')
        aggregate = {'success': True, 'sessions': 0, 'messages': 0, 'items': [], 'skipped': 0}
        for index, filename in enumerate(files, 1):
            try:
                result = self.imports.import_files([filename])
                aggregate['sessions'] += result['sessions']
                aggregate['messages'] += result['messages']
                aggregate['items'].extend(result['items'])
            except (OSError, ValueError, json.JSONDecodeError):
                aggregate['skipped'] += 1
            self.dataEvent.emit('import_progress', {'files_done': index, 'files_total': len(files),
                                'messages': aggregate['messages'], 'sessions': aggregate['sessions']})
        if not aggregate['sessions']:
            raise ValueError('未成功导入任何 ChatLab 会话，请检查导出格式与帐号 ID。')
        return aggregate

    def _catalog_item(self, item):
        result = dict(item)
        result['is_group'] = item.get('type') == 'group' or str(item.get('talker') or '').endswith('@chatroom')
        source = item.get('source')
        result['auto_reply_eligible'] = bool(source in ('weflow', 'wechat_db') or
                                             (source == 'ocr' and item.get('id') == self.live_id and self.status['capture'] == 'live'))
        result['auto_reply_type_known'] = source in ('weflow', 'wechat_db') and item.get('type') in ('private', 'group')
        return result

    @staticmethod
    def _wechat_db_reader():
        account, storage, connections = discover_cipher_source()
        return WeChatDBSource(storage, self_wxid=account.wxid,
                              self_display_name=account.display_name, connect_factory=connections)

    def _wechat_db_catalog_page(self, query, cursor, limit):
        state = self._read_catalog_cursor(cursor, 'wechat_db', query) if cursor else {'offset': 0}
        offset = int(state['offset'])
        reader = self._wechat_db_reader()
        # Read the first page immediately. Name search is a bounded walk over
        # SessionTable pages until the adapter can use a contact-name index.
        if not query:
            rows = reader.sessions(limit + 1, offset)
            selected = rows[:limit]
            next_offset = offset + len(selected)
            more = len(rows) > limit
        else:
            selected, next_offset, more = [], offset, False
            scanned = 0
            needle = query.casefold()
            while len(selected) <= limit and scanned < 20000:
                batch = reader.sessions(200, next_offset)
                if not batch:
                    break
                for row in batch:
                    next_offset += 1
                    if needle in str(row.get('name') or '').casefold() or needle in str(row.get('id') or '').casefold():
                        selected.append(row)
                        if len(selected) > limit:
                            more = True
                            break
                scanned += len(batch)
                if more or len(batch) < 200:
                    break
            if more:
                selected = selected[:limit]
                # The overflow row must be reconsidered on the next page.
                next_offset -= 1
        items = [{'id': row['id'], 'title': row.get('name') or row['id'], 'type': row.get('type'),
                  'source': 'wechat_db', 'talker': row['id'], 'preview': row.get('preview') or '',
                  'updated': row.get('sort_timestamp') or row.get('timestamp') or 0,
                  'last_message_kind': {3: 'image', 34: 'voice'}.get(row.get('last_msg_type'))}
                 for row in selected]
        next_cursor = self._catalog_cursor({'source': 'wechat_db', 'query': query, 'offset': next_offset}) if more else None
        return {'items': items, 'next_cursor': next_cursor, 'has_more': bool(next_cursor),
                'source': 'wechat_db', 'scope': 'all_available', 'available': True,
                'warning': None, 'total_loaded': len(items)}

    def _list_sessions_task(self, config, query, cursor, limit, source, collected_snapshot=None):
        query = str(query or '').strip()[:100]
        limit = min(50, max(1, int(limit or 20)))
        source = source if source in ('all', 'weflow', 'wechat_db', 'import', 'collected') else 'all'
        if source == 'all' and config.get('source') == 'wechat_db':
            source = 'wechat_db'
        if source == 'import':
            result = self.imports.list_sessions(query, cursor, limit)
        elif source == 'wechat_db':
            try:
                result = self._wechat_db_catalog_page(query, cursor, limit)
            except Exception:
                result = {'items': [], 'next_cursor': None, 'has_more': False, 'source': 'wechat_db',
                          'scope': 'unavailable', 'available': False,
                          'warning': '无法只读访问本机微信数据库；请检查 CipherTalk 已配置账号和微信登录状态。'}
        elif source == 'collected':
            state = self._read_catalog_cursor(cursor, source, query) if cursor else {'offset': 0}
            rows = [self._catalog_item({'id': item['id'], 'title': item['title'], 'source': item.get('source', 'ocr'),
                     'type': item.get('type', 'unknown'), 'preview': (item.get('messages') or [{}])[-1].get('text', '')[:140],
                     'updated': item.get('updated'), 'count': len(item.get('messages', []))})
                    for item in (collected_snapshot or []) if item.get('source') != 'import' and query.casefold() in item.get('title', '').casefold()]
            rows.sort(key=lambda row: row.get('updated') or 0, reverse=True)
            offset = state['offset']
            next_cursor = self._catalog_cursor({'source': source, 'query': query, 'offset': offset + limit}) if offset + limit < len(rows) else None
            result = {'items': rows[offset:offset + limit], 'next_cursor': next_cursor, 'has_more': bool(next_cursor),
                      'source': 'collected', 'scope': 'collected_only', 'available': False, 'total': len(rows),
                      'warning': '这里仅包含知弦已经读取的会话。'}
        elif source == 'weflow':
            if not config.get('weflow_token'):
                result = {'items': [], 'next_cursor': None, 'has_more': False, 'source': 'weflow',
                          'scope': 'unavailable', 'available': False, 'warning': '需要在高级设置连接本机 WeFlow 服务。'}
            else:
                result = self.hub.list_sessions({**config, 'source': 'weflow'}, query, cursor, limit)
        else:
            state = self._read_catalog_cursor(cursor, source, query) if cursor else {
                'source': source, 'query': query, 'hub_buf': [], 'imp_buf': [],
                'hub_cursor': None, 'imp_cursor': None, 'hub_more': True, 'imp_more': True,
                'seen': [], 'hub_warning': None, 'hub_available': False,
            }
            output = []
            attempts = 0
            while len(output) < limit and attempts < limit * 5 + 4:
                attempts += 1
                if not state['hub_buf'] and state['hub_more']:
                    cfg = {**config, 'source': 'weflow'} if config.get('weflow_token') else {**config, 'source': 'ocr'}
                    page = self.hub.list_sessions(cfg, query, state['hub_cursor'], limit)
                    state['hub_buf'] = [self._catalog_item(item) for item in page['items']]
                    state['hub_cursor'], state['hub_more'] = page.get('next_cursor'), bool(page.get('has_more'))
                    state['hub_warning'], state['hub_available'] = page.get('warning'), bool(page.get('available'))
                if not state['imp_buf'] and state['imp_more']:
                    page = self.imports.list_sessions(query, state['imp_cursor'], limit)
                    state['imp_buf'] = [self._catalog_item(item) for item in page['items']]
                    state['imp_cursor'], state['imp_more'] = page.get('next_cursor'), bool(page.get('has_more'))
                candidates = [(state[k][0].get('updated') or 0, k) for k in ('hub_buf', 'imp_buf') if state[k]]
                if not candidates:
                    break
                _, selected = max(candidates)
                item = state[selected].pop(0)
                if item['id'] not in state['seen']:
                    output.append(item)
                    state['seen'].append(item['id'])
                    state['seen'] = state['seen'][-5000:]
            more = bool(state['hub_buf'] or state['imp_buf'] or state['hub_more'] or state['imp_more'])
            next_cursor = self._catalog_cursor(state) if more and output else None
            archive_count = self.imports.count()
            result = {'items': output, 'next_cursor': next_cursor, 'has_more': bool(next_cursor),
                      'source': 'mixed' if archive_count and state['hub_available'] else 'weflow' if state['hub_available'] else 'local',
                      'scope': 'all_available' if state['hub_available'] else 'imported_archive' if archive_count else 'collected_only',
                      'available': bool(state['hub_available'] or archive_count),
                      'warning': state['hub_warning'], 'total_loaded': len(output)}
        result['items'] = [self._catalog_item(item) for item in result.get('items', [])]
        result['detail'] = result.get('warning') or ''
        self._remember_catalog(result['items'])
        self.dataEvent.emit('catalog_meta', {key: result.get(key) for key in ('source', 'scope', 'available', 'warning', 'total_loaded')})
        return result

    def _session_page_task(self, config, sid, cursor=None, limit=50):
        if sid.startswith('import:'):
            return self.imports.messages(sid, cursor, limit)
        with self.catalog_lock:
            source = (self.catalog_items.get(sid) or {}).get('source')
        if source == 'wechat_db' or (config.get('source') == 'wechat_db' and not sid.startswith(('ocr:', 'weflow:'))):
            reader = self._wechat_db_reader()
            before = self._read_catalog_cursor(cursor, 'wechat_db_messages', sid)['before'] if cursor else None
            rows = reader.messages(sid, limit=min(100, int(limit)) + 1, before=before)
            selected = rows[:limit]
            next_cursor = (self._catalog_cursor({'source': 'wechat_db_messages', 'query': sid,
                           'before': selected[-1]['_cursor']}) if len(rows) > limit and selected else None)
            items = [{k: v for k, v in row.items() if k != '_cursor'} for row in reversed(selected)]
            return {'items': items, 'next_cursor': next_cursor, 'has_more': bool(next_cursor),
                    'source': 'wechat_db', 'available': True, 'scope': 'wechat_db_history',
                    'order': 'chronological', 'direction': 'older'}
        return self.hub.messages(config, sid, cursor, limit)

    def _session_first_page(self, config, sid):
        page = self._session_page_task(config, sid, None, 50)
        if not page.get('available') and not page.get('items'):
            raise ValueError(page.get('warning') or '无法读取这个会话，请检查数据源。')
        if sid.startswith('import:'):
            meta = self.imports.get_session(sid)
        else:
            with self.catalog_lock:
                meta = copy.deepcopy(self.catalog_items.get(sid))
        if not meta:
            raise ValueError('会话目录已过期，请重新打开会话选择窗口。')
        return {'session': {'id': sid, 'title': meta.get('title') or '未命名会话',
                'source': meta.get('source') or page.get('source'), 'type': meta.get('type'),
                'messages': page.get('items', []), 'updated': meta.get('updated') or 0,
                'next_cursor': page.get('next_cursor'), 'has_more': bool(page.get('has_more'))},
                'page': page}

    def _run_agent_task(self, source_config, model_config, selected):
        """Read only the selected latest pages before entering the isolated model process."""
        sessions = []
        for sid, known in selected:
            source = known.get('source') or ''
            if source in ('ocr', 'manual'):
                session = known
            else:
                try:
                    page = self._session_page_task(source_config, sid, None, 40)
                except Exception:
                    raise ValueError('无法读取所选会话，请检查数据来源后重试。') from None
                if not page.get('available') and not page.get('items'):
                    raise ValueError('所选会话当前不可读取，请检查数据来源后重试。')
                session = {**known, 'messages': page.get('items') or []}
                # A fresh source page can replace an in-memory transcript or
                # image description added by an explicit user media action.
                understood = {str(item.get('id')): item for item in known.get('messages', [])
                              if isinstance(item, dict) and (item.get('transcript') or item.get('image_description'))}
                for item in session['messages']:
                    cached = understood.get(str(item.get('id'))) if isinstance(item, dict) else None
                    if cached and item.get('kind') == cached.get('kind') and isinstance(cached.get('text'), str):
                        item['text'] = cached['text']
            messages = []
            for raw in (session.get('messages') or [])[-40:]:
                if not isinstance(raw, dict):
                    continue
                messages.append({
                    'id': str(raw.get('id') or '')[:128],
                    'side': raw.get('side') if raw.get('side') in ('me', 'other') else 'unknown',
                    'sender': str(raw.get('sender') or '')[:80],
                    'kind': raw.get('kind') if raw.get('kind') in ('text', 'image', 'voice') else 'text' if isinstance(raw.get('text'), str) and raw['text'].strip() else 'unknown',
                    'text': str(raw.get('text') or '')[:1200],
                    'timestamp': raw.get('timestamp') if isinstance(raw.get('timestamp'), (int, float, str)) else None,
                    'directed_to_me': raw.get('directed_to_me') is True,
                })
            sessions.append({
                'id': sid, 'title': str(session.get('title') or '未命名会话')[:200],
                'type': session.get('type') if session.get('type') in ('private', 'group') else 'unknown',
                'source': str(session.get('source') or '')[:30],
                'messages': messages,
            })
        return self.models.submit('run_agent', {'sessions': sessions, 'config': model_config}).result(timeout=185)

    def _load_page(self, config, sid, cursor=None, limit=30):
        page = self._session_page_task(config, sid, cursor, limit)
        return page

    def _list_moments_task(self, config, sid, cursor, limit):
        with self.catalog_lock:
            selected = copy.deepcopy(self.catalog_items.get(sid)) if sid else None
        if sid and not selected:
            selected = self.imports.get_session(sid)
        username = selected.get('talker') if selected and selected.get('source') == 'weflow' else ''
        if sid and not username:
            page = {'items': [], 'next_cursor': None, 'has_more': False, 'source': 'local',
                    'available': False, 'scope': 'collected_only',
                    'warning': '这位联系人没有可用的朋友圈帐号映射；可以手动补充其动态。'}
        else:
            page = self.hub.moments({**config, 'source': 'weflow'}, username or '', cursor, limit)
        with self.catalog_lock:
            manual = [copy.deepcopy(p) for p in reversed(self.manual_moments.values())
                      if not sid or p.get('session_id') == sid]
        if not cursor:
            page['items'] = manual + page.get('items', [])
        page['detail'] = page.get('warning') or ''
        page['available'] = bool(page.get('available') or manual)
        with self.catalog_lock:
            for post in page['items']:
                self.moment_posts[post['id']] = dict(post)
                self.moment_posts.move_to_end(post['id'])
            while len(self.moment_posts) > 500:
                self.moment_posts.popitem(last=False)
        self.dataEvent.emit('moments_meta', {'available': page['available'], 'source': page['source'],
                                            'warning': page.get('warning')})
        return page

    def _media_bytes(self, config, sid, mid, asset_id=None):
        with self.catalog_lock:
            chosen = self.media_inputs.get(mid)
        if chosen:
            data, mime, kind = chosen
            return {'status': 'ok', 'kind': kind, 'mime_type': mime, 'data': data, 'source': 'user_selected'}
        if str(sid).startswith('import:'):
            media = self.imports.resolve_media(sid, mid)
            if media.get('status') != 'ok':
                return {'status': 'unavailable', 'reason': media.get('reason', '媒体尚不可用')}
            return {'status': 'ok', 'kind': media['kind'], 'mime_type': media['mime_type'],
                    'data': media['data'], 'source': 'user_selected'}
        if not asset_id:
            for item in self.sessions.get(sid, {}).get('messages', []):
                if item.get('id') == mid:
                    asset_id = next((m.get('id') for m in item.get('media', []) if m.get('id')), None)
                    break
        if not asset_id:
            return {'status': 'unavailable', 'reason': '当前来源没有可读取的媒体文件；可从微信保存后手动选择。'}
        resolved = self.hub.resolve_media({**config, 'source': 'weflow'}, asset_id)
        if resolved.get('status') != 'available':
            return {'status': 'unavailable', 'reason': resolved.get('reason', '媒体尚不可用')}
        data_url = resolved.get('data_url') or ''
        if len(data_url) > 28 * 1024 * 1024 or ';base64,' not in data_url:
            return {'status': 'unavailable', 'reason': '媒体格式或体积不受支持。'}
        try:
            raw = base64.b64decode(data_url.split(';base64,', 1)[1], validate=True)
        except (ValueError, base64.binascii.Error):
            return {'status': 'unavailable', 'reason': '媒体数据无法解码。'}
        return {'status': 'ok', 'kind': resolved['kind'], 'mime_type': resolved['mime_type'],
                'data': raw, 'source': 'weflow_local'}

    def _preview_media(self, config, sid, mid, asset_id=None):
        result = self._media_bytes(config, sid, mid, asset_id)
        if result['status'] != 'ok':
            return {**result, 'available': False, 'success': False, 'message': result.get('reason')}
        if result['kind'] == 'image':
            from io import BytesIO
            from PIL import Image, ImageOps
            try:
                if len(result['data']) > 12 * 1024 * 1024:
                    raise ValueError('too large')
                with Image.open(BytesIO(result['data'])) as image:
                    if image.width * image.height > 30_000_000:
                        raise ValueError('too many pixels')
                    image = ImageOps.exif_transpose(image)
                    image.thumbnail((960, 960))
                    canvas = Image.new('RGB', image.size, '#f6f8f7')
                    canvas.paste(image.convert('RGB'))
                    output = BytesIO()
                    canvas.save(output, 'JPEG', quality=78, optimize=True)
                    payload, mime = output.getvalue(), 'image/jpeg'
            except (OSError, ValueError, Image.DecompressionBombError):
                return {'status': 'unavailable', 'available': False, 'success': False,
                        'reason': '此图片格式无法在本机预览。'}
        elif result['kind'] == 'voice':
            payload, mime = result['data'], result['mime_type']
            if len(payload) > 12 * 1024 * 1024:
                return {'status': 'unavailable', 'available': False, 'success': False,
                        'reason': '语音文件过大，请先压缩或转写。'}
        else:
            return {'status': 'unavailable', 'available': False, 'success': False,
                    'reason': '当前预览仅支持图片与语音。'}
        return {'status': 'ok', 'available': True, 'success': True, 'kind': result['kind'], 'mime_type': mime,
                'data_url': 'data:' + mime + ';base64,' + base64.b64encode(payload).decode('ascii')}

    def _media_model_task(self, config, sid, mid, kind, asset_id=None, chosen=None):
        if chosen:
            source = 'user_selected'
            data, mime = chosen
        else:
            media = self._media_bytes(config, sid, mid, asset_id)
            if media['status'] != 'ok':
                return {'success': False, 'available': False, 'status': 'unsupported',
                        'message': media.get('reason', '媒体尚不可用')}
            source, data, mime = media['source'], media['data'], media['mime_type']
        cfg = {**config, 'media_allow_cloud': True}  # This call follows a user click.
        task = 'analyze_image' if kind == 'image' else 'transcribe_audio'
        result = self.models.submit(task, {'data': data, 'mime_type': mime, 'config': cfg, 'source': source}).result(timeout=180)
        if not result.get('success'):
            return {'success': False, 'available': False, 'status': result.get('status'),
                    'message': result.get('warning') or '模型无法处理所选媒体。'}
        if not result.get('text'):
            return {'success': False, 'available': False, 'status': 'empty',
                    'message': result.get('warning') or '没有识别出可用内容。'}
        return {'success': True, 'available': True, 'kind': kind, 'text': result.get('text', ''),
                'description': result.get('text', '') if kind == 'image' else None,
                'transcript': result.get('text', '') if kind == 'voice' else None,
                'model': result.get('model'), 'warning': result.get('warning'),
                'message': result.get('warning')}

    def _analyze_moment_task(self, cfg, post, sid):
        history = []
        if sid:
            if sid.startswith('import:'):
                history = self.imports.messages(sid, limit=30).get('items', [])
            else:
                history = copy.deepcopy(self.sessions.get(sid, {}).get('messages', [])[-30:])
                if not history and sid.startswith('weflow:'):
                    history = self.hub.messages({**cfg, 'source': 'weflow'}, sid, limit=30).get('items', [])
        contact = next((entry for entry in self.store.contacts if post.get('author') in [entry['name'], *entry['aliases']]), None)
        relationship = contact.get('relationship') if contact else cfg.get('relationship', '朋友')
        details = {'id': post['id'], 'text': post.get('text', ''), 'author': post.get('author', ''),
                   'image_descriptions': post.get('image_descriptions', [])}
        image_assets = [item for item in post.get('media', []) if item.get('id') and item.get('kind') == 'image']
        media_warning = ''
        if image_assets and not details['image_descriptions']:
            # The explicit Analyze action may use the first available picture.
            resolved = self._media_bytes(cfg, sid or '', '', image_assets[0]['id'])
            if resolved.get('status') == 'ok':
                seen = self.models.submit('analyze_image', {'data': resolved['data'], 'mime_type': resolved['mime_type'],
                     'config': {**cfg, 'media_allow_cloud': True}, 'source': resolved['source']}).result(timeout=180)
                if seen.get('success') and seen.get('text'):
                    details['image_descriptions'] = [seen['text']]
                    if len(image_assets) > 1:
                        media_warning = '只识别了第一张可用配图，其余配图未用于判断。'
                else:
                    media_warning = seen.get('warning') or '配图暂时无法识别，以下仅依据文字判断。'
            else:
                media_warning = resolved.get('reason', '配图未能读取，以下仅依据文字判断。')
        if not details['text'] and not details['image_descriptions']:
            return {'available': False, 'message': '这条动态只有未能识别的配图，请先提供可用图片。'}
        result = self.models.submit('analyze_moment', {'post': details, 'relationship': relationship or '朋友',
                          'history': history, 'config': cfg}).result(timeout=180)
        if sid and not history:
            media_warning += ' 未取得这位好友的聊天历史，建议仅依据当前动态。'
        if media_warning:
            result['warning'] = (result.get('warning') or '') + ' ' + media_warning.strip()
        like = result.get('like_recommendation')
        return {**result, 'available': True, 'like': True if like == 'like' else False if like == 'skip' else None,
                'recommendation': result.get('comment_label') or result.get('like_label'),
                'reason': result.get('topic') or result.get('warning'),
                'comments': [item['text'] for item in result.get('candidates', []) if item.get('text')]}

    def snapshot(self):
        current = copy.deepcopy(self.sessions.get(self.current_id))
        if current:
            current['active'] = self.current_id == self.live_id and self.status['capture'] == 'live'
        session_list = [{'id': s['id'], 'title': s['title'], 'count': len(s.get('messages', [])),
                         'preview': s.get('messages', [{}])[-1].get('text', '')[:100] if s.get('messages') else '',
                         'updated': s.get('updated', 0), 'source': s.get('source', 'ocr'),
                         'type': s.get('type', 'unknown'),
                         'auto_reply_eligible': s.get('source') in ('weflow', 'wechat_db') or
                                                (s.get('source') == 'ocr' and s['id'] == self.live_id and self.status['capture'] == 'live'),
                         'auto_reply_type_known': s.get('source') in ('weflow', 'wechat_db') and s.get('type') in ('private', 'group')}
                         for s in sorted(self.sessions.values(), key=lambda s: s.get('updated', 0), reverse=True)]
        return {'config': self.store.public_config(), 'status': dict(self.status), 'sessions': session_list,
                'current_session': current, 'analysis': copy.deepcopy(self.results.get(self.current_id)),
                'notes': copy.deepcopy(self.store.notes), 'contacts': copy.deepcopy(self.store.contacts),
                 'catalog': copy.deepcopy(self.catalog), 'moments': copy.deepcopy(self.moments),
                 'appearance': self._appearance(), 'auto_reply': self._auto_public(),
                'version': VERSION}

    def emit(self):
        if not self.closed:
            self.stateChanged.emit(self.snapshot())

    @staticmethod
    def fingerprint(session):
        raw = json.dumps(session.get('messages', [])[-200:], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def handle(self, method, params):
        if method == 'bootstrap':
            return self.snapshot()
        if method == 'configure_auto_reply':
            return self._configure_auto(params)
        if method == 'start_auto_reply':
            return self._start_auto(params)
        if method == 'pause_auto_reply':
            return self._stop_auto(pause=True)
        if method == 'stop_auto_reply':
            return self._stop_auto(emergency=bool(params.get('emergency')))
        if method == 'catch_up_auto_reply':
            return self._catch_up_once(params)
        if method == 'run_agent':
            raw = params.get('session_ids')
            if not isinstance(raw, list) or not 1 <= len(raw) <= 3:
                raise ValueError('请从会话目录选择 1 至 3 个会话。')
            ids = [str(value) for value in raw]
            if any(not value or len(value) > 256 for value in ids) or len(set(ids)) != len(ids):
                raise ValueError('会话选择无效，请重新选择。')
            if not self.store.secrets['api_key']:
                raise ValueError('请先在设置中填写 Jev API Key。')
            with self.catalog_lock:
                catalog = {sid: copy.deepcopy(self.catalog_items.get(sid)) for sid in ids}
            selected = []
            for sid in ids:
                session = copy.deepcopy(self.sessions.get(sid) or catalog.get(sid))
                if not session:
                    raise ValueError('会话目录已更新，请重新选择需要巡检的会话。')
                selected.append((sid, session))
            source_config = self.store.full_config()
            model_keys = ('api_key', 'base_url', 'model_name', 'reply_api_key', 'reply_base_url',
                          'reply_model', 'relationship', 'style', 'context_limit', 'timeout')
            model_config = {key: source_config[key] for key in model_keys if key in source_config}
            return self.pool.submit(self._run_agent_task, source_config, model_config, selected)
        if method == 'list_sessions':
            config = self.store.full_config()
            # The worker must not iterate mutable Qt-owned session state.
            collected = copy.deepcopy(list(self.sessions.values())) if params.get('source') == 'collected' else None
            return self.pool.submit(self._list_sessions_task, config, params.get('query', ''),
                                    params.get('cursor'), params.get('limit', 20), params.get('source', 'all'), collected)
        if method == 'list_moments':
            config = self.store.full_config()
            return self.pool.submit(self._list_moments_task, config, params.get('session_id'),
                                    params.get('cursor'), min(50, max(1, int(params.get('limit') or 20))))
        if method == 'manual_moment':
            author = str(params.get('author') or '').strip()[:200]
            content = str(params.get('text') or '').strip()[:16000]
            if not author or not content:
                raise ValueError('请填写好友名称和动态内容。')
            sid = str(params.get('session_id') or '')[:256]
            item = {'id': 'manual-moment:' + uuid.uuid4().hex, 'author': author, 'username': '',
                    'session_id': sid or None, 'text': content, 'timestamp': time.time(),
                    'media': [], 'source': 'manual'}
            with self.catalog_lock:
                self.manual_moments[item['id']] = item
                self.moment_posts[item['id']] = item
                while len(self.manual_moments) > 200:
                    self.manual_moments.popitem(last=False)
            self.emit()
            return {'success': True, 'available': True, 'item': item}
        if method == 'analyze_moment':
            ident = str(params.get('moment_id') or '')[:256]
            with self.catalog_lock:
                post = copy.deepcopy(self.moment_posts.get(ident))
            if not post:
                raise ValueError('这条动态已过期，请刷新后再分析。')
            sid = str(params.get('session_id') or post.get('session_id') or '')[:256]
            if not self.store.secrets['api_key']:
                raise ValueError('请先在设置中填写 Jev API Key。')
            return self.pool.submit(self._analyze_moment_task, self.store.full_config(), post, sid)
        if method == 'import_chat_records':
            mode = params.get('mode') or 'files'
            if mode == 'folder':
                directory = QFileDialog.getExistingDirectory(None, '选择 ChatLab 导出目录')
                if not directory:
                    return {'cancelled': True}
                root = Path(directory)
                future = self.pool.submit(self._import_folder_task, root)
                future.add_done_callback(lambda done: self._emit_import_done(done))
                return future
            elif mode == 'files':
                files, _ = QFileDialog.getOpenFileNames(None, '选择 ChatLab JSON/JSONL', '', 'ChatLab 文件 (*.json *.jsonl)')
            else:
                raise ValueError('导入方式无效。')
            if not files:
                if mode == 'folder':
                    raise ValueError('目录中没有可识别的 ChatLab JSON/JSONL 文件。')
                return {'cancelled': True}
            future = self.pool.submit(self.imports.import_files, files,
                    lambda progress: self.dataEvent.emit('import_progress', progress))
            future.add_done_callback(lambda done: self._emit_import_done(done))
            return future
        if method == 'load_session_messages':
            sid = str(params.get('session_id') or '')[:256]
            if not sid:
                raise ValueError('请选择一个会话。')
            future = self.pool.submit(self._load_page, self.store.full_config(), sid,
                                      params.get('cursor'), min(100, max(1, int(params.get('limit') or 30))))
            future.add_done_callback(lambda done: self._emit_session_done(sid, 'older', done))
            return future
        if method == 'load_media':
            sid, mid = str(params.get('session_id') or ''), str(params.get('message_id') or '')
            return self.pool.submit(self._preview_media, self.store.full_config(), sid, mid,
                                    params.get('asset_id'))
        if method in ('analyze_image', 'transcribe_voice'):
            sid, mid = str(params.get('session_id') or ''), str(params.get('message_id') or '')
            if not sid or not mid:
                raise ValueError('需要明确的会话与消息。')
            kind = 'image' if method == 'analyze_image' else 'voice'
            future = self.pool.submit(self._media_model_task, self.store.full_config(), sid, mid,
                                      kind, params.get('asset_id'))
            future.add_done_callback(lambda done: self._emit_media_done(sid, mid, done))
            return future
        if method == 'choose_media':
            sid = str(params.get('session_id') or self.current_id or '')
            if not sid or sid not in self.sessions:
                raise ValueError('请先选择需要补充的会话。')
            kind = params.get('kind')
            if kind not in ('image', 'voice'):
                raise ValueError('仅支持选择图片或语音文件。')
            file_filter = '图片 (*.png *.jpg *.jpeg *.webp *.gif)' if kind == 'image' else '语音 (*.wav *.mp3 *.m4a *.ogg *.opus *.flac)'
            filename, _ = QFileDialog.getOpenFileName(None, '选择需要理解的本机媒体', '', file_filter)
            if not filename:
                return {'cancelled': True}
            from mimetypes import guess_type
            path = Path(filename)
            limit = 8 * 1024 * 1024 if kind == 'image' else 20 * 1024 * 1024
            if path.stat().st_size > limit:
                raise ValueError('媒体文件过大，请缩小后再试。')
            mime = guess_type(path.name)[0] or 'application/octet-stream'
            data = path.read_bytes()
            mid = 'manual-media:' + uuid.uuid4().hex
            with self.catalog_lock:
                self.media_inputs[mid] = (data, mime, kind)
                self.media_inputs.move_to_end(mid)
                while len(self.media_inputs) > 10:
                    self.media_inputs.popitem(last=False)
            session = self.sessions[sid]
            session['messages'].append({'id': mid, 'side': 'other', 'sender': '手动提供',
                         'kind': kind, 'text': '[图片]' if kind == 'image' else '[语音]',
                         'timestamp': time.time(), 'source': 'manual_media', 'media': []})
            self.emit()
            future = self.pool.submit(self._media_model_task, self.store.full_config(), sid, mid,
                                      kind, None, (data, mime))
            future.add_done_callback(lambda done: self._emit_media_done(sid, mid, done))
            return future
        if method == 'set_appearance':
            config = {}
            if 'theme' in params:
                config['theme'] = params['theme']
            if 'font_scale' in params:
                config['font_scale'] = params['font_scale']
            self.store.save_config(config)
            self.emit()
            return {'success': True, 'appearance': self._appearance()}
        if method == 'choose_background':
            filename, _ = QFileDialog.getOpenFileName(None, '选择知弦背景图片', '', '图片 (*.png *.jpg *.jpeg *.webp)')
            if not filename:
                return {'cancelled': True, 'appearance': self._appearance()}
            from PIL import Image, ImageOps
            from io import BytesIO
            path = Path(filename)
            if path.stat().st_size > 15 * 1024 * 1024:
                raise ValueError('背景图片超过 15 MB，请先缩小。')
            try:
                with Image.open(path) as image:
                    if image.width * image.height > 30_000_000:
                        raise ValueError('背景图片像素过大。')
                    image = ImageOps.exif_transpose(image)
                    image.thumbnail((1920, 1200))
                    canvas = image.convert('RGB')
                    for quality in (75, 62, 50):
                        output = BytesIO()
                        canvas.save(output, 'JPEG', quality=quality, optimize=True)
                        data = output.getvalue()
                        if len(data) <= 1024 * 1024:
                            break
                        canvas.thumbnail((int(canvas.width * .8), int(canvas.height * .8)))
                    if len(data) > 1024 * 1024:
                        raise ValueError('背景图片无法压缩至安全大小，请选择另一张。')
            except (OSError, Image.DecompressionBombError):
                raise ValueError('无法读取此图片，请选择 PNG、JPEG 或 WebP。') from None
            target = self.store.directory / 'background.jpg'
            temporary = target.with_suffix('.jpg.tmp')
            temporary.write_bytes(data)
            os.replace(temporary, target)
            self.background_url = self._background_url()
            self.emit()
            return {'success': True, 'appearance': self._appearance()}
        if method == 'clear_background':
            (self.store.directory / 'background.jpg').unlink(missing_ok=True)
            self.background_url = ''
            self.emit()
            return {'success': True, 'appearance': self._appearance()}
        if method == 'save_config':
            if self.auto_policy['enabled']:
                self._stop_auto()
            self.store.save_config(params.get('config', {}))
            self.revision += 1
            self.windowAction.emit('always_on_top', self.store.config['always_on_top'])
            self.emit()
            return self.store.public_config()
        if method == 'test_connection':
            return self.models.submit('test_connection', self.store.merged(params.get('config', {})))
        if method == 'start_capture':
            self.paused_by_user = False
            self.allow_initial = True
            self.capture.start(self.store.full_config())
            self.status.update(capture='searching', detail='正在定位微信窗口…', last_error='')
            self.emit()
            return {'success': True}
        if method == 'pause_capture':
            if self.auto_policy['enabled']:
                self._stop_auto(pause=True)
            self.paused_by_user = True
            self.capture.pause()
            self.revision += 1
            self.timer.stop()
            self.pending = None
            self.status.update(capture='paused', analysis='idle', connected=False, detail='采集已暂停')
            self.emit()
            return {'success': True}
        if method == 'one_shot':
            self.paused_by_user = False
            self.allow_initial = True
            self.capture.one_shot(self.store.full_config())
            return {'success': True}
        if method == 'refresh_sources':
            return self.pool.submit(self.capture.sources, self.store.full_config())
        if method == 'select_session':
            ident = params.get('session_id')
            if ident not in self.sessions:
                future = self.pool.submit(self._session_first_page, self.store.full_config(), str(ident or '')[:256])
                future.add_done_callback(lambda done: self._emit_session_done(str(ident or ''), 'select', done))
                return future
            self.current_id = ident
            self.follow_live = ident == self.live_id
            self.emit()
            return self.snapshot()
        if method == 'analyze':
            ident = params.get('session_id') or self.current_id
            if not ident or not self.sessions.get(ident, {}).get('messages'):
                raise ValueError('还没有可分析的消息。')
            if not self.store.secrets['api_key']:
                raise ValueError('请先在设置中填写 API Key。')
            self.pending = ident
            self.pending_manual = True
            self._run_pending()
            return {'success': True}
        if method == 'copy_reply':
            QApplication.clipboard().setText(str(params.get('text', ''))[:16000])
            return {'success': True}
        if method == 'fill_reply':
            ident = params.get('session_id')
            if ident != self.live_id or self.status['capture'] != 'live':
                raise ValueError('当前微信会话与候选所属会话不一致，请切回原会话。')
            session = self.sessions.get(ident)
            result = self.results.get(ident, {})
            if self.result_fingerprints.get(ident) != self.fingerprint(session):
                raise ValueError('对话已更新，请重新分析后再填入。')
            candidates = result.get('candidates', [])
            index = int(params.get('index', -1))
            if not 0 <= index < len(candidates):
                raise ValueError('候选回复不存在。')
            return self.pool.submit(self.capture.fill, candidates[index]['text'], session['title'])
        if method == 'save_note':
            result = self.store.save_note(params.get('note', {}))
        elif method == 'save_contact':
            result = self.store.save_contact(params.get('contact', {}))
        elif method in ('delete_note', 'delete_contact'):
            self.store.delete('notes' if method == 'delete_note' else 'contacts', params.get('id'))
            result = {'success': True}
        elif method == 'import_notes':
            blocks = str(params.get('text', '')).replace('\r\n', '\n').split('\n\n')
            count = 0
            for block in blocks[:100]:
                lines = block.strip().splitlines()
                if lines:
                    self.store.save_note({'title': lines[0], 'content': '\n'.join(lines[1:]) or lines[0]})
                    count += 1
            result = {'success': True, 'count': count}
        elif method == 'clear_history':
            self._stop_auto()
            self.revision += 1
            self.sessions.clear()
            self.results.clear()
            self.result_fingerprints.clear()
            self.media_inputs.clear()
            self.store.clear_history()
            self.capture.reset_history()
            self.current_id = self.live_id = None
            self.follow_live = True
            self.timer.stop()
            self.pending = None
            result = {'success': True}
        elif method == 'manual_context':
            title = str(params.get('title', '')).strip()[:100] or '手动分析'
            text = str(params.get('text', '')).strip()[:32000]
            if not text:
                raise ValueError('请粘贴需要分析的对话。')
            ident = 'manual:' + hashlib.sha256(title.encode()).hexdigest()[:16]
            messages = []
            for index, line in enumerate(text.splitlines()):
                line = line.strip()
                if not line:
                    continue
                mine = line.startswith(('我：', '我:', 'me:', 'me：'))
                for prefix in ('我：', '我:', 'me:', 'me：', '对方：', '对方:', 'other:'):
                    if line.startswith(prefix):
                        line = line[len(prefix):].strip()
                        break
                messages.append({'id': f'{time.time_ns()}:{index}', 'text': line, 'side': 'me' if mine else 'other',
                                 'sender': '我' if mine else '对方', 'timestamp': time.time(), 'source': 'manual'})
            self.sessions[ident] = {'id': ident, 'title': title, 'messages': messages, 'source': 'manual', 'updated': time.time()}
            self.current_id = ident
            self.follow_live = False
            result = {'success': True, 'session_id': ident}
        elif method == 'set_compact':
            self.windowAction.emit('compact', bool(params.get('enabled')))
            result = {'success': True}
        elif method == 'open_data_folder':
            os.startfile(str(self.store.directory))
            result = {'success': True}
        elif method == 'quit':
            self.windowAction.emit('quit', True)
            result = {'success': True}
        else:
            raise ValueError('此操作暂不支持。')
        self.emit()
        return result

    def _emit_import_done(self, future):
        if self.closed or future.cancelled():
            return
        try:
            self.importFinished.emit(future.result(), None)
        except Exception as exc:
            self.importFinished.emit(None, self.safe_error(exc))

    def _on_import_finished(self, result, error):
        if self.closed:
            return
        self.catalog['progress'] = None
        if result and result.get('success'):
            self.catalog['imported'] = self.imports.count()
            self.catalog['available'] = True
            self.catalog['scope'] = 'imported_archive'
            self._sync_hub()
        self.emit()

    def _emit_session_done(self, sid, kind, future):
        if self.closed or future.cancelled():
            return
        try:
            self.sessionLoaded.emit(sid, {'kind': kind, 'result': future.result()}, None)
        except Exception as exc:
            self.sessionLoaded.emit(sid, None, self.safe_error(exc))

    def _on_session_loaded(self, sid, payload, error):
        if self.closed or error or not payload:
            return
        result = payload['result']
        if payload['kind'] == 'select':
            session = result['session']
            self.sessions[sid] = session
            self.current_id = sid
            self.follow_live = False
            if self.results.get(sid) and self.result_fingerprints.get(sid) != self.fingerprint(session):
                self.results.pop(sid, None)
                self.result_fingerprints.pop(sid, None)
        else:
            session = self.sessions.get(sid)
            if session:
                combined = {m['id']: m for m in [*result.get('items', []), *session.get('messages', [])] if m.get('id')}
                # Pages are requested explicitly. Never silently discard an older
                # page while the user is traversing a large archive.
                session['messages'] = sorted(combined.values(), key=lambda m: (m.get('timestamp') or 0, m['id']))
                session['next_cursor'] = result.get('next_cursor')
                session['has_more'] = bool(result.get('has_more'))
        self._sync_hub()
        self.emit()

    def _emit_media_done(self, sid, mid, future):
        if self.closed or future.cancelled():
            return
        try:
            self.mediaFinished.emit(sid, mid, future.result(), None)
        except Exception as exc:
            self.mediaFinished.emit(sid, mid, None, self.safe_error(exc))

    def _on_media_finished(self, sid, mid, result, error):
        if self.closed or error or not result or not result.get('success'):
            return
        session = self.sessions.get(sid)
        if not session:
            return
        for message in session.get('messages', []):
            if message.get('id') != mid:
                continue
            if result.get('kind') == 'voice' and result.get('transcript'):
                message['transcript'] = result['transcript']
                message['text'] = '[语音转写] ' + result['transcript']
            elif result.get('kind') == 'image' and result.get('description'):
                message['image_description'] = result['description']
                message['text'] = '[图片识别] ' + result['description']
            break
        else:
            return
        if self.store.config['auto_analyze'] and sid == self.live_id and session['messages'][-1]['id'] == mid:
            self.pending = sid
            self.pending_manual = True
            self.timer.start(400)
        self._sync_hub()
        self.emit()

    def _on_data(self, kind, data):
        if self.closed:
            return
        if kind == 'sessions_updated':
            items = data.get('items') or []
            self._remember_catalog(items)
            indexed = {entry['id']: entry for entry in self.catalog['preview_updates']}
            for item in items:
                indexed[item['id']] = item
            self.catalog['preview_updates'] = list(indexed.values())[-100:]
            self.catalog['source'] = data.get('source') or self.catalog['source']
        elif kind == 'catalog_meta':
            self.catalog.update({name: value for name, value in data.items() if value is not None})
            self.catalog['imported'] = self.imports.count()
        elif kind == 'import_progress':
            self.catalog['progress'] = data
        elif kind == 'moments_meta':
            self.moments.update(data)
        self.emit()

    def _on_capture(self, kind, data):
        if self.closed:
            return
        if self.paused_by_user and not (kind == 'status' and data.get('capture') == 'paused'):
            return
        if kind == 'status':
            self.status.update({k: v for k, v in data.items() if k in ('capture', 'detail', 'source')})
            self.status['connected'] = self.status['capture'] == 'live'
            if self.status['capture'] == 'error':
                self.status['last_error'] = self.safe_error(data.get('detail') or '采集未完成。')
                if self.auto_policy['enabled']:
                    self._cancel_auto_work()
                    self.auto_paused = True
                    self.auto_detail = '微信采集失去可靠状态，自动回复已暂停。'
            elif self.status['capture'] == 'live':
                self.status['last_error'] = ''
        elif kind == 'error':
            self.status.update(capture='error', connected=False, last_error=data.get('message', '采集失败'))
            if self.auto_policy['enabled']:
                self._cancel_auto_work()
                self.auto_paused = True
                self.auto_detail = '微信消息状态发生变化，自动回复已暂停。'
        elif kind in ('session', 'messages'):
            ident = data.get('session_id') or data.get('id')
            if not ident:
                self.live_id = None
                self.emit()
                return
            is_live = data.get('source') != 'manual'
            if is_live:
                previous = self.live_id
                self.live_id = ident
                # A deliberate archive selection stays put when the visible
                # WeChat window changes in the background.
                if self.current_id is None or self.follow_live:
                    self.current_id = ident
            session = self.sessions.setdefault(ident, {'id': ident, 'title': data.get('title', '当前会话'),
                                                        'source': data.get('source', 'ocr'), 'messages': [], 'updated': time.time()})
            session['title'] = data.get('title') or session['title']
            if data.get('type') in ('private', 'group'):
                session['type'] = data['type']
            if data.get('talker'):
                session['talker'] = data['talker']
            if kind == 'messages':
                historical = bool(data.get('historical', False))
                initial_allowed = self.allow_initial
                self.allow_initial = False
                # A missing bubble overlap alone is ambiguous: scrolling old
                # records can look like fresh messages. The selected sidebar
                # preview changes only when that chat receives a new latest
                # message. Require that change plus a verified visible tail;
                # otherwise quarantine this batch and keep listening.
                if historical and session['messages'] and data.get('source') == 'ocr' and not initial_allowed:
                    if self.auto_policy['enabled'] and ident in self.auto_policy['session_ids']:
                        self._cancel_auto_work()
                        if data.get('sidebar_changed') is True and data.get('tail_baseline_verified') is True:
                            baseline = []
                            for row in data.get('messages', []):
                                message = dict(row)
                                message['text'] = str(message.get('text') or '')[:16000]
                                message['historical'] = True
                                baseline.append(message)
                            if baseline:
                                session['messages'] = baseline[-500:]
                                session['updated'] = time.time()
                                self.store.save_history([s for s in self.sessions.values() if s.get('source') != 'import'])
                                self.auto_detail = '左侧摘要已更新且聊天尾部可核验，已重建当前屏上下文。'
                                last = session['messages'][-1]
                                if (data.get('tail_verified') is True and last.get('side') == 'other' and
                                        self.store.secrets['api_key'] and not self.auto_paused):
                                    last['historical'] = False
                                    live_event = {**data, 'historical': False}
                                    delay = self._auto_observe(session, last, live_event)
                                    if delay is not None:
                                        self.pending = ident
                                        self.pending_manual = False
                                        self.timer.start(max(self.store.config['debounce_ms'], delay))
                        else:
                            self.auto_detail = '当前屏与最新摘要无法共同确认新来信，本批跳过并继续监听。'
                        self._save_auto_state()
                    self.status.update(capture='live', connected=True, last_error='')
                    self._sync_hub()
                    self.emit()
                    return
                known = {m['id'] for m in session['messages']}
                added = []
                for message in data.get('messages', []):
                    message = dict(message)
                    message.setdefault('id', hashlib.sha256(json.dumps(message, sort_keys=True).encode()).hexdigest())
                    if message['id'] in known:
                        continue
                    message['text'] = str(message.get('text', ''))[:16000]
                    known.add(message['id'])
                    added.append(message)
                session['messages'] = (session['messages'] + added)[-500:]
                if data.get('source') in ('weflow', 'wechat_db'):
                    session['messages'].sort(key=lambda m: m.get('timestamp') or 0)
                session['updated'] = time.time()
                # Imported archives already live in their own local index. Avoid
                # copying them into the optional transient history JSON as well.
                self.store.save_history([s for s in self.sessions.values() if s.get('source') != 'import'])
                cfg = self.store.full_config()
                if added and added[-1].get('side') == 'me' and self.pending == ident and not self.pending_manual:
                    self.pending = None
                    self.timer.stop()
                    self.auto_trigger = None
                auto_delay = None
                if added and added[-1].get('side') == 'other' and cfg['api_key'] and not historical:
                    auto_delay = self._auto_observe(session, added[-1], data)
                    if auto_delay is None and self.auto_trigger and self.auto_trigger['session_id'] == ident:
                        self.auto_trigger = None
                if (added and added[-1].get('side') == 'other' and cfg['api_key']
                        and ((cfg['auto_analyze'] and (not historical or initial_allowed)) or auto_delay is not None)):
                    self.pending = ident
                    self.pending_manual = False
                    self.timer.start(max(cfg['debounce_ms'], auto_delay or 0))
            self.status.update(capture='live', connected=True, last_error='')
            self._sync_hub()
        self.emit()

    def _run_pending(self):
        if self.inflight or not self.pending or self.closed:
            return
        if (self.auto_trigger and not self.pending_manual and
                self.auto_trigger['session_id'] == self.pending):
            remaining = self.auto_trigger['ready_at'] - time.time()
            if remaining > 0:
                self.timer.start(max(50, int(remaining * 1000)))
                return
        ident, self.pending = self.pending, None
        manual, self.pending_manual = self.pending_manual, False
        session = copy.deepcopy(self.sessions.get(ident))
        cfg = self.store.full_config()
        if not session or not session['messages'] or not cfg['api_key']:
            return
        if not manual and session['messages'][-1].get('side') != 'other':
            return
        fingerprint = self.fingerprint(session)
        self.auto_running_trigger = None
        if (not manual and self.auto_trigger and self.auto_trigger['session_id'] == ident and
                self.auto_trigger['message']['id'] == session['messages'][-1].get('id') and
                self.auto_trigger['epoch'] == self.auto_epoch and self.auto_policy['enabled'] and not self.auto_paused):
            self.auto_running_trigger = self.auto_trigger
            self.auto_trigger = None
        background, contact = self.store.background(session['title'], session['messages'])
        if self.auto_running_trigger:
            # Unattended replies use only this conversation. Private notes and
            # contact metadata must not be copied into an outgoing draft.
            background, contact = '', None
            cfg['style'] = (str(cfg.get('style') or '')[:2000] +
                            '\n自动回复请用不超过130字的自然短句，只回答当前消息；不编造事实、承诺或私聊之外的背景。'
                            '无需添加来源标识，应用会在发送前附加固定的知弦生成说明。')
        if contact and contact.get('relationship'):
            cfg['relationship'] = contact['relationship']
        self.job_serial += 1
        serial, revision = self.job_serial, self.revision
        self.inflight = (ident, serial)
        self.status.update(analysis='running', last_error='')
        self.emit()
        history = session['messages'][:-cfg['context_limit']][-30:] if cfg['save_history'] else None
        try:
            analysis_payload = {'messages': session['messages'], 'config': cfg,
                        'background': background, 'history': history, 'reply_to': cfg.get('reply_to') or None}
            if self.auto_running_trigger:
                future = self.pool.submit(self._prepare_live_auto_task, cfg, session, analysis_payload,
                                          copy.deepcopy(self.auto_running_trigger), self.auto_policy['group_mode'])
            else:
                future = self.models.submit('analyze', analysis_payload)
        except Exception as exc:
            self.inflight = None
            self.status.update(analysis='error', last_error=self.safe_error(exc))
            if self.auto_running_trigger:
                self.auto_running_trigger = None
                self.auto_paused = True
                self.auto_detail = '实时判断任务未能启动，自动回复已暂停。'
                self._auto_record(ident, 'failed', '实时判断任务启动失败')
            self.emit()
            return
        def work(completed):
            if self.closed or completed.cancelled():
                return
            try:
                result = completed.result()
                result['session_id'] = ident
                result['updated_at'] = time.time()
                self.analysisDone.emit(ident, revision, fingerprint, result, None)
            except Exception as exc:
                self.analysisDone.emit(ident, revision, fingerprint, None, self.safe_error(exc))
        future.add_done_callback(work)

    def _on_analysis(self, ident, revision, fingerprint, result, error):
        self.inflight = None
        auto_trigger, self.auto_running_trigger = self.auto_running_trigger, None
        if self.closed:
            return
        if revision == self.revision and ident in self.sessions:
            if error:
                self.status.update(analysis='error', last_error=error)
                self.toast.emit(error, 'error')
                if auto_trigger:
                    self.auto_paused = True
                    self.auto_detail = '模型分析失败，自动回复已暂停。'
                    self._auto_record(ident, 'failed', '模型分析失败')
            elif self.fingerprint(self.sessions[ident]) == fingerprint:
                self.status.update(analysis='idle', last_error='')
                if auto_trigger:
                    judged = result.pop('_auto_decision', None) if isinstance(result, dict) else None
                    self.results.pop(ident, None)
                    self.result_fingerprints.pop(ident, None)
                    if not isinstance(judged, dict):
                        self.auto_paused = True
                        self.auto_detail = '实时回复判断结果缺失，自动回复已暂停。'
                        self._auto_record(ident, 'failed', 'Jev 实时回复判断缺失')
                    elif judged.get('reason') == 'provider_or_protocol_failure':
                        self.auto_paused = True
                        self.auto_detail = 'Jev 实时回复判断服务暂不可用，自动回复已暂停。'
                        self._auto_record(ident, 'failed', 'Jev 实时回复判断服务不可用')
                    elif judged.get('should_reply') is not True or judged.get('target_message_id') != auto_trigger['message']['id']:
                        self._auto_record(ident, 'skipped', 'Jev 判断当前来信不需要回复')
                        self.auto_detail = 'Jev 判断这条新消息无需回复，继续等待。'
                    else:
                        self.results[ident] = result
                        self.result_fingerprints[ident] = fingerprint
                        self._auto_dispatch(auto_trigger, result, judged)
                else:
                    self.results[ident] = result
                    self.result_fingerprints[ident] = fingerprint
            else:
                self.status['analysis'] = 'idle'
                if ((self.store.config['auto_analyze'] or self.auto_policy['enabled']) and self.status['capture'] == 'live'
                        and self.sessions[ident]['messages'][-1].get('side') == 'other'):
                    self.pending = ident
                    self.pending_manual = False
        else:
            self.status['analysis'] = 'idle'
        self.emit()
        if self.pending:
            self.timer.start(400)

    def safe_error(self, exc):
        message = str(exc) or '操作未完成，请稍后重试。'
        for value in self.store.secrets.values():
            if value:
                message = message.replace(value, '[已隐藏]')
        return message[:1200]

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.timer.stop()
        try:
            self.capture.stop(wait=False)
        finally:
            self.hub.close(wait=False)
            self.models.close()
            self.pool.close()
