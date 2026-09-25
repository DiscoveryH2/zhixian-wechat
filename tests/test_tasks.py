import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from desk.tasks import ModelTasks


class ModelProcessTests(unittest.TestCase):
    def setUp(self):
        self.received = threading.Event()
        received = self.received
        self.delay = 0
        parent = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.set()
                time.sleep(parent.delay)
                if 'reply_now' in request.get('questions', {}):
                    answers = {'reply_now': {'type': 'choice', 'choice': 'reply', 'confidence': 0.9}}
                elif 'next_step' in request.get('questions', {}):
                    answers = {}
                    for key, question in request['questions'].items():
                        if question['type'] == 'choice':
                            answers[key] = {'type': 'choice', 'choice': 'reply_now', 'confidence': .9}
                        elif question['type'] == 'score':
                            answers[key] = {'type': 'score', 'score': 1, 'confidence': .9}
                        else:
                            answers[key] = {'type': 'noul', 'noul': .9}
                else:
                    answers = {'connection': {'type': 'noul', 'noul': 1}}
                data = json.dumps({'answers': answers}).encode()
                try:
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.tasks = ModelTasks()
        self.config = {'base_url': f'http://127.0.0.1:{self.server.server_port}/v1',
                       'model_name': 'jev-synthetic', 'api_key': 'synthetic-test-only'}

    def tearDown(self):
        self.tasks.close()
        self.server.shutdown()
        self.server.server_close()

    def test_spawned_model_task_uses_actual_local_protocol(self):
        response = self.tasks.submit('test_connection', self.config).result(timeout=15)
        self.assertTrue(response['success'])
        self.assertFalse(response['generation_available'])

    def test_close_terminates_running_request_without_waiting_for_timeout(self):
        self.delay = 5
        future = self.tasks.submit('test_connection', self.config)
        self.assertTrue(self.received.wait(10))
        started = time.monotonic()
        self.tasks.close()
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(future.cancelled())

    def test_spawned_backlog_judgment_uses_typed_local_protocol(self):
        now = time.time()
        result = self.tasks.submit('judge_backlog', {'messages': [
            {'id': 'synthetic-out', 'side': 'me', 'text': '明天聊。', 'timestamp': now - 60},
            {'id': 'synthetic-in', 'side': 'other', 'text': '上午方便吗？', 'timestamp': now - 30}],
            'config': self.config, 'chat_type': 'private',
            'chat_name': 'Synthetic Contact'}).result(timeout=15)
        self.assertIs(result['should_reply'], True)
        self.assertEqual(result['target_message_id'], 'synthetic-in')

    def test_spawned_agent_uses_typed_protocol_without_send_tool(self):
        now = time.time()
        result = self.tasks.submit('run_agent', {'sessions': [{
            'id': 'synthetic-session', 'title': '合成联系人', 'type': 'private', 'source': 'manual',
            'messages': [
                {'id': 'm1', 'side': 'me', 'kind': 'text', 'text': '明天再说。', 'timestamp': now - 60},
                {'id': 'm2', 'side': 'other', 'kind': 'text', 'text': '可以定个时间吗？', 'timestamp': now - 30},
            ]}], 'config': self.config}).result(timeout=20)
        self.assertEqual(result['cards'][0]['action'], 'reply_now')
        self.assertEqual(result['cards'][0]['evidence'][-1]['message_id'], 'm2')
        self.assertIn('jev.triage', [step['tool'] for step in result['trace']])


if __name__ == '__main__':
    unittest.main()
