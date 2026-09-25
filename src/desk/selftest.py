"""Portable-build diagnostic using only synthetic input and localhost HTTP."""
import json
import io
import threading
import sys
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
        import windows_capture
        import sqlcipher3
        import zstandard
        from pypdf import PdfReader, PdfWriter
        import PySide6.QtWebEngineWidgets
        with sqlcipher3.connect(':memory:') as cipher_check:
            if not cipher_check.execute('PRAGMA cipher_version').fetchone():
                raise RuntimeError('Bundled SQLCipher driver is unavailable')
        report['checks'].append('sqlcipher3_import')
        if zstandard.ZstdDecompressor().decompress(zstandard.ZstdCompressor().compress(b'synthetic')) != b'synthetic':
            raise RuntimeError('Bundled Zstandard decoder is unavailable')
        report['checks'].append('zstandard_import')
        pdf = io.BytesIO()
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(pdf)
        if len(PdfReader(io.BytesIO(pdf.getvalue())).pages) != 1:
            raise RuntimeError('Bundled PDF reader is unavailable')
        report['checks'].append('pypdf_import')
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
                if self.path.endswith('/chat/completions'):
                    body = json.dumps({'choices': [{'message': {'role': 'assistant',
                        'content': '合成 Work Agent 已完成。'}}]}).encode()
                else:
                    answers = {k: {'type': 'noul', 'noul': 1} for k in data['questions']}
                    body = json.dumps({'answers': answers}).encode()
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
        work = tasks.submit('work_turn', {'messages': [
            {'role': 'user', 'content': '用一句话确认合成任务完成。', 'attachment_ids': []}],
            'attachments': [], 'config': {
                'work_base_url': f'http://127.0.0.1:{server.server_port}/v1',
                'work_model': 'synthetic-work', 'work_api_key': ''}}).result(timeout=25)
        if work.get('answer') != '合成 Work Agent 已完成。':
            raise RuntimeError('Isolated Work Agent did not complete')
        report['checks'] += ['frozen_model_worker', 'local_typed_http', 'qt_webengine_import', 'windows_capture_import']
        report['checks'] += ['work_agent_model_worker', 'local_chat_http']
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
