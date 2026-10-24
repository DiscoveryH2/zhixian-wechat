"""Actual QtWebChannel UI → DPAPI store → isolated model process → local mock HTTP → UI.

All conversations and keys are synthetic. No WeChat or external model is used.
"""
import json
import io
import multiprocessing
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


class MockAPI(BaseHTTPRequestHandler):
    slow_work = False
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path.endswith('/chat/completions'):
            if request.get('tools') and type(self).slow_work:
                time.sleep(5)
            if request.get('tools') and not type(self).slow_work and not any(m.get('role') == 'tool' for m in request.get('messages', [])):
                arguments = json.dumps({'name': 'synthetic-plan.md', 'content': '# 合成项目计划\n- 周三确认页面\n- 周五交付原型',
                                        'mime_type': 'text/markdown'}, ensure_ascii=False)
                value = {'choices': [{'message': {'role': 'assistant', 'content': None,
                    'tool_calls': [{'id': 'call-synthetic', 'type': 'function',
                                    'function': {'name': 'write_artifact', 'arguments': arguments}}]}}]}
            else:
                content = ('合成 Work Agent 回答：任务已整理。' if request.get('tools') else
                           json.dumps(['模拟回应一，明天一起确认。', '模拟回应二，我先核对安排。', '模拟回应三，收到你的建议。']))
                value = {'choices': [{'message': {'role': 'assistant', 'content': content}}]}
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
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

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
    text_material = Path(temporary.name) / 'synthetic-brief.txt'
    text_material.write_text('合成项目计划：周五提交原型，周三确认页面清单。', encoding='utf-8')
    image_material = Path(temporary.name) / 'synthetic-color.png'
    canvas = io.BytesIO()
    Image.new('RGB', (32, 32), '#ff8c00').save(canvas, format='PNG')
    image_material.write_bytes(canvas.getvalue())
    chooser = patch('desk.controller.QFileDialog.getOpenFileNames',
                    return_value=([str(text_material), str(image_material)], ''))
    chooser.start()
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
    url = QUrl.fromLocalFile(str(ROOT / 'ui/index.html'))
    url.setQuery('intro=0')
    view.load(url)
    view.show()

    def fail(message):
        state['error'] = message
        view.grab().save(str(ROOT / 'work/ui-bridge-failed.png'))
        app.exit(1)

    def inspect(raw):
        if not raw:
            return QTimer.singleShot(100, tick)
        dom = json.loads(raw)
        state['last_js_error'] = dom.get('js_error') or ''
        if state['phase'] == 0 and dom['onboarding']:
            view.page().runJavaScript("window.addEventListener('error',e=>window.__smokeError=String(e.message))")
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
            controller.handle('save_config', {'config': {'reply_model': 'synthetic-draft', 'reply_base_url': base + '/v1',
                'work_base_url': base + '/v1', 'work_model': 'synthetic-work'}})
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
            view.page().runJavaScript("document.querySelector('#navigation button[aria-label=\"工作 Agent\"]')?.click()")
            state['phase'] = 4
        elif state['phase'] == 4 and dom['work_page']:
            view.page().runJavaScript("[...document.querySelectorAll('.work-tab')].find(b=>b.textContent.includes('会话巡检'))?.click()")
            state['phase'] = 5
        elif state['phase'] == 5 and dom['agent_page']:
            view.page().runJavaScript("document.querySelector('.agent-mission button')?.click()")
            state['phase'] = 6
        elif state['phase'] == 6 and dom['catalog_open']:
            view.page().runJavaScript("document.querySelector('#session-catalog [data-filter=collected]')?.click()")
            state['phase'] = 7
        elif state['phase'] == 7 and dom['catalog_row']:
            view.page().runJavaScript("document.querySelector('#session-catalog .catalog-row')?.click();setTimeout(()=>[...document.querySelectorAll('#session-catalog button')].find(b=>b.textContent.includes('应用选择'))?.click(),150)")
            state['phase'] = 8
        elif state['phase'] == 8 and dom['agent_selected']:
            view.page().runJavaScript("[...document.querySelectorAll('.agent-run-row button')].find(b=>b.textContent.includes('开始分诊'))?.click()")
            state['phase'] = 9
        elif state['phase'] == 9 and dom['agent_card']:
            if '模拟回应一' not in dom['text'] or not dom['agent_trace']:
                view.grab().save(str(ROOT / 'work/ui-agent-bridge-failed.png'))
                return fail('Agent bridge result, candidate, or trace missing')
            view.grab().save(str(ROOT / 'work/ui-agent-bridge-synthetic.png'))
            view.page().runJavaScript("[...document.querySelectorAll('.work-tab')].find(b=>b.textContent.includes('工作 Agent'))?.click()")
            state['phase'] = 10
        elif state['phase'] == 10 and dom['work_page'] and not dom['agent_page']:
            view.page().runJavaScript("[...document.querySelectorAll('.work-sidebar button')].find(b=>b.textContent.includes('新任务'))?.click()")
            state['phase'] = 11
        elif state['phase'] == 11 and dom['work_composer']:
            view.page().runJavaScript("[...document.querySelectorAll('.work-composer button')].find(b=>b.textContent.includes('添加文件或图片'))?.click()")
            state['phase'] = 12
        elif state['phase'] == 12 and dom['work_pending']:
            view.page().runJavaScript("(() => { const f=document.querySelector('.work-composer');f.querySelector('textarea').value='请整理一条合成项目计划';f.requestSubmit(); })()")
            state['phase'] = 13
        elif state['phase'] == 13 and dom['work_answer'] and dom['work_artifact']:
            if '合成 Work Agent 回答' not in dom['text']:
                return fail('Work Agent bridge answer missing')
            sessions = controller.work_agent.list_sessions()
            if len(sessions) != 1 or len(controller.work_agent.get_session(sessions[0]['id'])['attachments']) != 2:
                return fail('Work Agent selected text/image attachments were not stored')
            view.page().runJavaScript("[...document.querySelectorAll('.work-artifact button')].find(b=>b.textContent.includes('预览'))?.click()")
            state['phase'] = 14
        elif state['phase'] == 14 and dom['artifact_preview']:
            MockAPI.slow_work = True
            view.page().runJavaScript("(() => { document.querySelector('#editor .dialog-head button')?.click();const f=document.querySelector('.work-composer');f.querySelector('textarea').value='第二条合成慢任务';f.requestSubmit(); })()")
            state['phase'] = 15
        elif state['phase'] == 15 and dom['work_stop']:
            view.page().runJavaScript("[...document.querySelectorAll('.work-composer button')].find(b=>b.textContent.trim()==='停止')?.click()")
            state['phase'] = 16
        elif state['phase'] == 16 and dom['work_retry_ready']:
            sessions = controller.work_agent.list_sessions()
            stored = controller.work_agent.get_session(sessions[0]['id']) if len(sessions) == 1 else {}
            if len(sessions) != 1 or len(stored.get('messages', [])) != 2 or len(stored.get('artifacts', [])) != 1:
                return fail('Cancelled Work Agent turn changed persisted task history')
            state['success'] = True
            state['phase'] = 17
            QTimer.singleShot(400, lambda: (view.grab().save(str(ROOT / 'work/ui-work-agent-bridge-synthetic.png')), app.quit()))
            return
        QTimer.singleShot(100, tick)

    def tick():
        script = "JSON.stringify({onboarding:!!document.querySelector('#onboarding-form'),text:document.body.innerText,agent_page:!!document.querySelector('.agent-page'),work_page:!!document.querySelector('.work-page'),work_composer:!!document.querySelector('.work-composer'),work_pending:document.querySelectorAll('.work-pending-files .work-file-chip').length>=2,work_answer:[...document.querySelectorAll('.work-message-assistant')].some(e=>e.textContent.includes('合成 Work Agent 回答')),work_artifact:!!document.querySelector('.work-artifact'),artifact_preview:!!document.querySelector('#editor[open] .work-artifact-preview')?.textContent.includes('周三确认页面'),work_stop:[...document.querySelectorAll('.work-composer button')].some(b=>b.textContent.trim()==='停止'),work_retry_ready:!!document.querySelector('.work-composer textarea:not(:disabled)')?.value.includes('第二条合成慢任务')&&!document.querySelector('.work-working'),catalog_open:!!document.querySelector('#session-catalog[open]'),catalog_row:!!document.querySelector('#session-catalog .catalog-row'),agent_selected:!!document.querySelector('.agent-selected-row'),agent_card:!!document.querySelector('.agent-card'),agent_trace:!!document.querySelector('.agent-trace'),js_error:window.__smokeError||''})"
        view.page().runJavaScript(script, inspect)
    QTimer.singleShot(300, tick)
    QTimer.singleShot(45000, lambda: fail('UI bridge integration timed out at phase ' + str(state['phase']) + ('; JS: ' + state['last_js_error'] if state.get('last_js_error') else '')))
    app.exec()
    controller.close()
    chooser.stop()
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
