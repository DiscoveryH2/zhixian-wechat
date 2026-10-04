"""Synthetic QtWebChannel regression for actions, archive search and appearance."""
import json
import multiprocessing
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))


def main():
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from desk.controller import Controller
    from desk.bridge import Bridge
    app = QApplication([])
    temporary = tempfile.TemporaryDirectory(prefix='zhixian-ui-')
    controller = Controller(Path(temporary.name))
    controller.store.secrets['api_key'] = 'synthetic-in-memory-only'
    controller.store.config['source'] = 'archive'
    class ModelStub(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            inputs = json.loads(payload['messages'][-1]['content'])['messages']
            message = next(m for m in inputs if m['text'] == '报价待确认')
            proposal = {'title': '核对报价', 'detail': '合成模型提案', 'evidence': [{'message_id': message['id'], 'quote': message['text']}]}
            body = json.dumps({'choices': [{'message': {'content': json.dumps({'proposals': [proposal]})}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), ModelStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    controller.store.config.update(base_url=f'http://127.0.0.1:{server.server_port}', reply_base_url=f'http://127.0.0.1:{server.server_port}', model_name='synthetic-judge', reply_model='synthetic-reply')
    controller.handle('manual_context', {'title': '合成客户', 'text': '我：我先核对报价\n对方：请核对客户方案'})
    payload = {'chatlab': {'version': '0.0.2'}, 'meta': {'name': '合成归档', 'type': 'private', 'ownerId': 'me'},
               'members': [{'platformId': 'me'}, {'platformId': 'customer'}],
               'messages': [{'platformMessageId': str(i), 'sender': 'customer', 'timestamp': 1700000000+i,
                             'type': 0, 'content': '报价待确认' if i == 2 else '合成消息'} for i in range(100)]}
    source = Path(temporary.name) / 'synthetic.json'
    source.write_text(json.dumps(payload), encoding='utf-8')
    controller.imports.import_files([str(source)])
    view = QWebEngineView()
    view.resize(1280, 850)
    channel = QWebChannel(view.page())
    bridge = Bridge(controller)
    channel.registerObject('bridge', bridge)
    view.page().setWebChannel(channel)
    view.load(QUrl.fromLocalFile(str(ROOT / 'ui/index.html')))
    view.show()
    state = {'phase': 0, 'success': False, 'error': '', 'intro_observed': False}
    screenshots = ROOT / 'work/upgrade-ui'
    screenshots.mkdir(parents=True, exist_ok=True)

    def fail(message):
        state['error'] = message
        view.grab().save(str(screenshots / 'failure.png'))
        app.exit(1)

    def execute(script):
        view.page().runJavaScript(script)

    def capture_then_execute(filename, script):
        def capture():
            view.grab().save(str(screenshots / filename))
            execute(script)
        QTimer.singleShot(350, capture)

    def inspect(raw):
        if not raw:
            return QTimer.singleShot(100, tick)
        dom = json.loads(raw)
        phase = state['phase']
        if phase == 0 and dom['intro'] and not state['intro_observed']:
            state['intro_observed'] = True
            QTimer.singleShot(2800, lambda: view.grab().save(str(screenshots / 'intro.png')))
        if phase == 0 and '合成客户' in dom['text'] and not dom['intro']:
            execute("[...document.querySelectorAll('#navigation button')].find(b=>b.textContent.includes('行动中心')).click()")
            state['phase'] = 1
        elif phase == 1 and dom['actions']:
            execute("[...document.querySelectorAll('button')].find(b=>b.textContent.includes('新增跟进')).click()")
            state['phase'] = 2
        elif phase == 2 and dom['action_editor']:
            execute("document.querySelector('#action-title').value='核对合成客户方案';document.querySelector('#action-detail').value='合成回归步骤';document.querySelector('#action-evidence').selectedIndex=1;[...document.querySelectorAll('#editor button')].find(b=>b.textContent.includes('确认保存')).click()")
            state['phase'] = 3
        elif phase == 3 and dom['action_card']:
            state['phase'] = 4
            capture_then_execute('actions.png', "[...document.querySelectorAll('.action-card button')].find(b=>b.textContent==='完成').click()")
        elif phase == 4 and not dom['action_card']:
            execute("[...document.querySelectorAll('.topbar button')].find(b=>b.textContent.includes('检索记录')).click()")
            state['phase'] = 5
        elif phase == 5 and dom['search_editor']:
            execute("const input=document.querySelector('#editor input[type=search]');input.value='报价待';input.dispatchEvent(new Event('input',{bubbles:true}))")
            state['phase'] = 6
        elif phase == 6 and dom['search_hit']:
            state['phase'] = 7
            capture_then_execute('search.png', "document.querySelector('.search-hit').click()")
        elif phase == 7 and dom['highlight']:
            if '报价待确认' not in dom['text']:
                return fail('Search opened a page without its cited message')
            view.grab().save(str(screenshots / 'search-context.png'))
            execute("[...document.querySelectorAll('#navigation button')].find(b=>b.textContent.includes('外观')).click()")
            state['phase'] = 8
        elif phase == 8 and dom['appearance']:
            if not dom['theme_aurora'] or not dom['background_slider']:
                return fail('Appearance controls missing')
            view.grab().save(str(screenshots / 'appearance.png'))
            if 'synthetic-in-memory-only' in str(controller.snapshot()):
                return fail('Credential leaked into public snapshot')
            execute("[...document.querySelectorAll('#navigation button')].find(b=>b.textContent.includes('行动中心')).click()")
            state['phase'] = 9
        elif phase == 9 and dom['actions']:
            execute("[...document.querySelectorAll('button')].find(b=>b.textContent==='扫描承诺').click()")
            state['phase'] = 10
        elif phase == 10 and dom['proposal']:
            if controller.actions.list()['items']:
                return fail('Model proposal created an action before confirmation')
            view.grab().save(str(screenshots / 'proposals.png'))
            execute("[...document.querySelectorAll('#editor button')].find(b=>b.textContent==='核对并建立跟进').click()")
            state['phase'] = 11
        elif phase == 11 and dom['action_editor']:
            execute("[...document.querySelectorAll('#editor button')].find(b=>b.textContent==='确认保存').click()")
            state['phase'] = 12
        elif phase == 12 and dom['action_card']:
            evidence = controller.actions.list()['items'][0]['evidence']
            if not evidence or evidence[0]['text'] != '报价待确认':
                return fail('Confirmed proposal lost its original evidence')
            state['success'] = True
            app.quit()
            return
        QTimer.singleShot(100, tick)

    def tick():
        script = "JSON.stringify({text:document.body.innerText,intro:!!document.querySelector('#cinema-intro'),actions:!!document.querySelector('.actions-page'),action_editor:!!document.querySelector('#action-title'),action_card:!!document.querySelector('.actions-page .action-card'),proposal:!!document.querySelector('#editor .action-card'),search_editor:!!document.querySelector('#editor input[type=search]'),search_hit:!!document.querySelector('.search-hit'),highlight:!!document.querySelector('.message-hit'),appearance:!!document.querySelector('.appearance-page'),theme_aurora:!!document.querySelector('.theme-aurora'),background_slider:!!document.querySelector('#background-dim')})"
        view.page().runJavaScript(script, inspect)
    QTimer.singleShot(500, tick)
    QTimer.singleShot(45000, lambda: fail('UI regression timed out at phase ' + str(state['phase'])))
    app.exec()
    controller.close()
    server.shutdown()
    server.server_close()
    view.close()
    temporary.cleanup()
    report = {**state, 'scope': 'Synthetic desktop UI + real bridge + local SQLite; no live WeChat or external API, credentials only in memory.'}
    (screenshots / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    return 0 if state['success'] else 1


if __name__ == '__main__':
    multiprocessing.freeze_support()
    raise SystemExit(main())
