"""Portable-build diagnostic using only synthetic input and localhost HTTP."""
import json
import threading
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def run_self_test(report_file):
    report = {'success': False, 'checks': [], 'uses_real_wechat': False, 'uses_external_api': False}
    server = tasks = None
    try:
        from PIL import Image, ImageDraw, ImageFont
        import numpy as np
        from app.ocr import read_title
        from desk.tasks import ModelTasks
        if sys.platform == 'win32':
            import windows_capture
            report['checks'].append('windows_capture_import')
        elif sys.platform == 'darwin':
            import keyring.backends.macOS
            report['checks'].append('keychain_backend_import')
        import sqlcipher3
        import zstandard
        import PySide6.QtWebEngineWidgets
        with sqlcipher3.connect(':memory:') as cipher_check:
            if not cipher_check.execute('PRAGMA cipher_version').fetchone():
                raise RuntimeError('Bundled SQLCipher driver is unavailable')
        report['checks'].append('sqlcipher3_import')
        if zstandard.ZstdDecompressor().decompress(zstandard.ZstdCompressor().compress(b'synthetic')) != b'synthetic':
            raise RuntimeError('Bundled Zstandard decoder is unavailable')
        report['checks'].append('zstandard_import')
        image = Image.new('RGB', (440, 80), '#f5f5f5')
        draw = ImageDraw.Draw(image)
        assets = Path(sys._MEIPASS) if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[2]
        font = ImageFont.truetype(str(assets / 'ui/fonts/NotoSansSC-Variable.ttf'), 28)
        draw.text((20, 16), '合成测试会话', font=font, fill='#222222')
        if '合成测试' not in read_title(np.asarray(image)):
            raise RuntimeError('Bundled offline OCR did not recognize the synthetic title')
        report['checks'].append('bundled_chinese_ocr')
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if 'questions' in data:
                    value = {'answers': {k: {'type': 'noul', 'noul': 1} for k in data['questions']}}
                else:
                    state = json.loads(data['messages'][-1]['content'])
                    record = state['records'][0]
                    result = {'reply': '合成模拟回应', 'evidence': [{'id': record['id'], 'quote': record['text']}]}
                    value = {'choices': [{'message': {'content': json.dumps(result)}}]}
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        tasks = ModelTasks()
        config = {'base_url': f'http://127.0.0.1:{server.server_port}/v1',
                  'model_name': 'jev-synthetic', 'api_key': 'synthetic-diagnostic-only'}
        value = tasks.submit('test_connection', config).result(timeout=25)
        if not value['success']:
            raise RuntimeError('Isolated model worker did not complete')
        report['checks'] += ['frozen_model_worker', 'local_typed_http', 'qt_webengine_import']
        from desk.imports import ChatLabImporter
        from desk.relationships import RelationshipStore
        with tempfile.TemporaryDirectory(prefix='zhixian-synthetic-') as temp:
            directory = Path(temp)
            source = directory / 'synthetic.json'
            source.write_text(json.dumps({'chatlab': {'version': '0.0.2'},
                'meta': {'name': '合成联系人', 'type': 'private', 'ownerId': 'synthetic-self'},
                'members': [{'platformId': 'synthetic-self'}, {'platformId': 'synthetic-friend'}],
                'messages': [{'sender': 'synthetic-friend', 'type': 0, 'content': '合成来源记忆', 'timestamp': 1700000000, 'platformMessageId': '1'}]}), encoding='utf-8')
            archives = ChatLabImporter(directory / 'index')
            archives.import_files([str(source)])
            memory = RelationshipStore(archives)
            persona = memory.create_persona(memory.contacts()['items'][0]['id'])
            context = memory.chat_context(persona['id'], '合成记忆')
            chat_config = {**config, 'reply_model': 'synthetic-chat',
                           'reply_base_url': f'http://127.0.0.1:{server.server_port}/v1'}
            reply = tasks.submit('chat_persona', {'context': context, 'config': chat_config}).result(timeout=25)
            memory.append_turn(persona['id'], context['revision'], context['message'], reply)
            if not memory.persona(persona['id'])['turns'] or not reply['simulation']:
                raise RuntimeError('Source-backed synthetic persona failed')
            report['checks'] += ['relationship_schema', 'frozen_persona_worker', 'source_citations', 'durable_persona_turn']
        report['success'] = True
    except Exception as exc:
        report['error'] = str(exc)[:1000]
    finally:
        if tasks:
            tasks.close()
        if server:
            server.shutdown()
            server.server_close()
    target = Path(report_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['success'] else 1
