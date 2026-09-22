"""Read-only live WeChat check. Never persists screenshots, message text, or chat titles."""
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from desk.capture_service import CaptureService

report = {'window_detected': False, 'session_detected': False, 'message_count': 0,
          'sides': [], 'capture_states': [], 'errors': [], 'pause_verified': False,
          'resume_verified': False, 'stopped': False,
          'saved_screenshots': False, 'saved_message_text': False, 'filled_or_sent': False}
ready, paused, resumed = threading.Event(), threading.Event(), threading.Event()
resuming = False


def callback(kind, data):
    global report
    if kind == 'status':
        state = data.get('capture')
        if state and (not report['capture_states'] or report['capture_states'][-1] != state):
            report['capture_states'].append(state)
        if state == 'live':
            report['window_detected'] = True
            if resuming:
                resumed.set()
        if state == 'paused':
            paused.set()
    elif kind == 'session':
        report['session_detected'] = bool(data.get('title'))
    elif kind == 'messages':
        messages = data.get('messages', [])
        report['message_count'] += len(messages)
        report['sides'] = sorted(set(report['sides']) | {m.get('side') for m in messages if m.get('side')})
        if messages:
            ready.set()
    elif kind == 'error':
        report['errors'].append(data.get('message', 'Unknown capture error'))


service = CaptureService(callback)
started = time.monotonic()
try:
    service.start({'source': 'ocr'})
    report['messages_detected'] = ready.wait(25)
    service.pause()
    report['pause_verified'] = paused.wait(6)
    resuming = True
    service.start({'source': 'ocr'})
    report['resume_verified'] = resumed.wait(8)
finally:
    service.stop()
    report['stopped'] = True
report['elapsed_seconds'] = round(time.monotonic() - started, 2)
(ROOT / 'outputs/live-capture-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(report, ensure_ascii=False))
