"""Application orchestration. All session mutations run on the Qt application thread."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication

from .store import Store
from .version import VERSION
from .tasks import ModelTasks, HelperTasks


class Controller(QObject):
    stateChanged = Signal(dict)
    toast = Signal(str, str)
    captureEvent = Signal(str, dict)
    analysisDone = Signal(str, int, str, object, object)
    windowAction = Signal(str, object)

    def __init__(self, directory: Path):
        super().__init__()
        from .capture_service import CaptureService
        self.store = Store(directory)
        self.capture = CaptureService(lambda k, p: self.captureEvent.emit(k, p))
        self.pool = HelperTasks()
        self.models = ModelTasks()
        self.sessions = {s['id']: s for s in self.store.load_history() if isinstance(s, dict) and s.get('id')}
        self.results, self.result_fingerprints = {}, {}
        self.current_id = next(iter(self.sessions), None)
        self.live_id = None
        self.status = {'capture': 'idle', 'analysis': 'idle', 'detail': '配置接口后，开始读取微信当前会话。',
                       'last_error': self.store.error, 'source': 'ocr', 'connected': False}
        self.revision, self.job_serial = 0, 0
        self.inflight = None
        self.pending = None
        self.pending_manual = False
        self.closed = False
        self.allow_initial = False
        self.paused_by_user = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._run_pending)
        self.captureEvent.connect(self._on_capture)
        self.analysisDone.connect(self._on_analysis)

    def snapshot(self):
        current = copy.deepcopy(self.sessions.get(self.current_id))
        if current:
            current['active'] = self.current_id == self.live_id and self.status['capture'] == 'live'
        session_list = [{'id': s['id'], 'title': s['title'], 'count': len(s.get('messages', [])),
                         'preview': s.get('messages', [{}])[-1].get('text', '')[:100] if s.get('messages') else '',
                         'updated': s.get('updated', 0), 'source': s.get('source', 'ocr')}
                        for s in sorted(self.sessions.values(), key=lambda s: s.get('updated', 0), reverse=True)]
        return {'config': self.store.public_config(), 'status': dict(self.status), 'sessions': session_list,
                'current_session': current, 'analysis': copy.deepcopy(self.results.get(self.current_id)),
                'notes': copy.deepcopy(self.store.notes), 'contacts': copy.deepcopy(self.store.contacts),
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
        if method == 'save_config':
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
                raise ValueError('会话不存在。')
            self.current_id = ident
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
            self.revision += 1
            self.sessions.clear()
            self.results.clear()
            self.result_fingerprints.clear()
            self.store.clear_history()
            self.capture.reset_history()
            self.current_id = self.live_id = None
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
            elif self.status['capture'] == 'live':
                self.status['last_error'] = ''
        elif kind == 'error':
            self.status.update(capture='error', connected=False, last_error=data.get('message', '采集失败'))
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
                # Follow actual foreground chat changes; browsing history stays put between messages.
                if self.current_id is None or self.current_id == previous or kind == 'session':
                    self.current_id = ident
            session = self.sessions.setdefault(ident, {'id': ident, 'title': data.get('title', '当前会话'),
                                                       'source': data.get('source', 'ocr'), 'messages': [], 'updated': time.time()})
            session['title'] = data.get('title') or session['title']
            if kind == 'messages':
                historical = bool(data.get('historical', False))
                initial_allowed = self.allow_initial
                self.allow_initial = False
                # OCR cannot distinguish an entirely different viewport from scrolled-back
                # history. Never append such a batch as the newest conversation context.
                if historical and session['messages'] and data.get('source') == 'ocr' and not initial_allowed:
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
                if data.get('source') == 'weflow':
                    session['messages'].sort(key=lambda m: m.get('timestamp') or 0)
                session['updated'] = time.time()
                self.store.save_history(list(self.sessions.values()))
                cfg = self.store.full_config()
                if added and added[-1].get('side') == 'me' and self.pending == ident and not self.pending_manual:
                    self.pending = None
                    self.timer.stop()
                if (added and added[-1].get('side') == 'other' and cfg['auto_analyze'] and cfg['api_key']
                        and (not historical or initial_allowed)):
                    self.pending = ident
                    self.pending_manual = False
                    self.timer.start(cfg['debounce_ms'])
            self.status.update(capture='live', connected=True, last_error='')
        self.emit()

    def _run_pending(self):
        if self.inflight or not self.pending or self.closed:
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
        background, contact = self.store.background(session['title'], session['messages'])
        if contact and contact.get('relationship'):
            cfg['relationship'] = contact['relationship']
        self.job_serial += 1
        serial, revision = self.job_serial, self.revision
        self.inflight = (ident, serial)
        self.status.update(analysis='running', last_error='')
        self.emit()
        history = session['messages'][:-cfg['context_limit']][-30:] if cfg['save_history'] else None
        try:
            future = self.models.submit('analyze', {'messages': session['messages'], 'config': cfg,
                        'background': background, 'history': history, 'reply_to': cfg.get('reply_to') or None})
        except Exception as exc:
            self.inflight = None
            self.status.update(analysis='error', last_error=self.safe_error(exc))
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
        if self.closed:
            return
        if revision == self.revision and ident in self.sessions:
            if error:
                self.status.update(analysis='error', last_error=error)
                self.toast.emit(error, 'error')
            elif self.fingerprint(self.sessions[ident]) == fingerprint:
                self.results[ident] = result
                self.result_fingerprints[ident] = fingerprint
                self.status.update(analysis='idle', last_error='')
            else:
                self.status['analysis'] = 'idle'
                if (self.store.config['auto_analyze'] and self.status['capture'] == 'live'
                        and self.sessions[ident]['messages'][-1].get('side') == 'other'):
                    self.pending = ident
                    self.pending_manual = False
        else:
            self.status['analysis'] = 'idle'
        self.emit()
        if self.pending:
            self.timer.start(400)

    def safe_error(self, exc):
        message = str(exc)[:1200] or '操作未完成，请稍后重试。'
        for value in self.store.secrets.values():
            if value:
                message = message.replace(value, '[已隐藏]')
        return message

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.timer.stop()
        try:
            self.capture.stop(wait=False)
        finally:
            self.models.close()
            self.pool.close()
