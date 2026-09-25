"""Actual QtWebChannel UI → DPAPI store → isolated model process → local mock HTTP → UI.

All conversations and keys are synthetic. No WeChat or external model is used.
"""
import json
import multiprocessing
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


class MockAPI(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path.endswith('/chat/completions'):
            value = {'choices': [{'message': {'content': json.dumps(['模拟回应一，明天一起确认。', '模拟回应二，我先核对安排。', '模拟回应三，收到你的建议。'])}}]}
        else:
            answers = {}
            for key, q in request['questions'].items():
                if q['type'] == 'choice':
                    keys = list(q['criteria'])
                    answers[key] = {'type': 'choice', 'choice': keys[0], 'confidence': .88,
                                    'probabilities': {k: .9 if i == 0 else .1 / max(1, len(keys) - 1) for i, k in enumerate(keys)}}
                elif q['type'] == 'score':
                    answers[key] = {'type': 'score', 'score': 1, 'confidence': .9}
                else:
                    answers[key] = {'type': 'noul', 'noul': .9}
            value = {'answers': answers}
        data = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def main():
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from desk.controller import Controller
    from desk.bridge import Bridge

    app = QApplication([])
    temporary = tempfile.TemporaryDirectory(dir=ROOT / 'work', prefix='bridge-test-')
    controller = Controller(Path(temporary.name))
    server = ThreadingHTTPServer(('127.0.0.1', 0), MockAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}/api'
    bridge = Bridge(controller)
    view = QWebEngineView()
    view.resize(1280, 850)
    channel = QWebChannel(view.page())
    channel.registerObject('bridge', bridge)
    view.page().setWebChannel(channel)
    state = {'phase': 0, 'success': False, 'error': ''}
    view.load(QUrl.fromLocalFile(str(ROOT / 'ui/index.html')))
    view.show()

    def fail(message):
        state['error'] = message
        app.exit(1)

    def inspect(raw):
        if not raw:
            return QTimer.singleShot(100, tick)
        dom = json.loads(raw)
        if state['phase'] == 0 and dom['onboarding']:
            view.grab().save(str(ROOT / 'work/ui-onboarding.png'))
            setup = """(() => { const f=document.querySelector('#onboarding-form');
            f.querySelector('[name=api_key]').value='synthetic-smoke-only';
            f.querySelector('[name=base_url]').value=BASE;
            f.querySelector('[name=model_name]').value='jev-synthetic';
            [...f.querySelectorAll('button')].find(b=>b.textContent.includes('进入工作台')).click(); })()""".replace('BASE', json.dumps(base))
            view.page().runJavaScript(setup)
            state['phase'] = 1
        elif state['phase'] == 1 and controller.store.secrets['api_key']:
            if dom['onboarding']:
                return QTimer.singleShot(100, tick)
            controller.handle('save_config', {'config': {'reply_model': 'synthetic-draft', 'reply_base_url': base + '/v1'}})
            controller.handle('manual_context', {'title': '合成桥接测试', 'text': '我：明天讨论方案\n对方：可以给我一个具体安排吗'})
            state['phase'] = 2
        elif state['phase'] == 2 and '合成桥接测试' in dom['text']:
            view.page().runJavaScript("[...document.querySelectorAll('button')].find(b=>b.textContent.includes('分析当前')).click()")
            state['phase'] = 3
        elif state['phase'] == 3 and controller.results:
            if '模拟回应一' not in dom['text']:
                return QTimer.singleShot(100, tick)
            result = next(iter(controller.results.values()))
            if len(result['candidates']) != 3 or len(result['answers']) != 7:
                return fail('Incorrect model result contract')
            if 'synthetic-smoke-only' in str(controller.snapshot()):
                return fail('Credential leaked into UI snapshot')
            view.grab().save(str(ROOT / 'work/ui-bridge-synthetic.png'))
            view.page().runJavaScript("[...document.querySelectorAll('#navigation button')].find(b=>b.textContent.includes('知弦 Agent'))?.click()")
            state['phase'] = 4
        elif state['phase'] == 4 and dom['agent_page']:
            view.page().runJavaScript("document.querySelector('.agent-mission button')?.click()")
            state['phase'] = 5
        elif state['phase'] == 5 and dom['catalog_open']:
            view.page().runJavaScript("document.querySelector('#session-catalog [data-filter=collected]')?.click()")
            state['phase'] = 6
        elif state['phase'] == 6 and dom['catalog_row']:
            view.page().runJavaScript("document.querySelector('#session-catalog .catalog-row')?.click();setTimeout(()=>[...document.querySelectorAll('#session-catalog button')].find(b=>b.textContent.includes('应用选择'))?.click(),150)")
            state['phase'] = 7
        elif state['phase'] == 7 and dom['agent_selected']:
            view.page().runJavaScript("[...document.querySelectorAll('.agent-run-row button')].find(b=>b.textContent.includes('开始分诊'))?.click()")
            state['phase'] = 8
        elif state['phase'] == 8 and dom['agent_card']:
            if '模拟回应一' not in dom['text'] or not dom['agent_trace']:
                view.grab().save(str(ROOT / 'work/ui-agent-bridge-failed.png'))
                return fail('Agent bridge result, candidate, or trace missing')
            view.grab().save(str(ROOT / 'work/ui-agent-bridge-synthetic.png'))
            state['success'] = True
            app.quit()
            return
        QTimer.singleShot(100, tick)

    def tick():
        script = "JSON.stringify({onboarding:!!document.querySelector('#onboarding-form'),text:document.body.innerText,agent_page:!!document.querySelector('.agent-page'),catalog_open:!!document.querySelector('#session-catalog[open]'),catalog_row:!!document.querySelector('#session-catalog .catalog-row'),agent_selected:!!document.querySelector('.agent-selected-row'),agent_card:!!document.querySelector('.agent-card'),agent_trace:!!document.querySelector('.agent-trace')})"
        view.page().runJavaScript(script, inspect)
    QTimer.singleShot(300, tick)
    QTimer.singleShot(60000, lambda: fail('UI bridge integration timed out at phase ' + str(state['phase'])))
    app.exec()
    controller.close()
    server.shutdown()
    server.server_close()
    view.close()
    temporary.cleanup()
    report = {'success': state['success'], 'phase': state['phase'], 'error': state['error'],
              'scope': 'Synthetic UI + real QWebChannel + DPAPI + spawned model process + local HTTP. No real WeChat/API.'}
    (ROOT / 'work/bridge-smoke.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if state['success'] else 1


if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
