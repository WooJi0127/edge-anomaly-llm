import importlib
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from detector import core
from test_core import frame


class ModelTests(unittest.TestCase):
    def alert(self):
        d = core.AnomalyDetector(window=8)
        for ts in range(8):
            d.update([frame(ts)])
        return d.update([frame(8, temp=40)])[0]

    def test_empty_and_failed_model_fall_back_with_actual_source(self):
        a = self.alert()
        for payload in [{'response': '  '}, {'response': 3}, {}]:
            with patch('detector.core.urllib.request.urlopen', return_value=io.BytesIO(json.dumps(payload).encode())):
                result = core.explain_alert(a)
                self.assertEqual(result['explanation_mode'], 'rule')
                self.assertEqual(result['llm'], a['rule'])
                self.assertEqual(result['fallback_reason'], 'empty_response')
        with patch('detector.core.urllib.request.urlopen', side_effect=OSError('offline')):
            result = core.explain_alert(a)
            self.assertEqual(result['explanation_mode'], 'rule')
            self.assertEqual(result['fallback_reason'], 'model_error:OSError')

    def test_model_success_and_legacy_wrapper(self):
        a = self.alert()
        with patch('detector.core.urllib.request.urlopen', side_effect=lambda *a, **k: io.BytesIO(b'{"response":"done"}')):
            self.assertEqual(core.explain_alert(a), {'llm': 'done', 'explanation_mode': 'llm', 'fallback_reason': None})
            self.assertEqual(core.llm_interpret(a), 'done')


class ServiceTests(unittest.TestCase):
    def server_module(self):
        try:
            return importlib.import_module('detector.server')
        except ImportError as exc:
            self.fail('package import must work: ' + str(exc))

    def test_package_entry_imports(self):
        result = subprocess.run([sys.executable, '-c', 'import run; import detector.server'], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_slow_model_does_not_block_http_and_queue_is_bounded(self):
        server = self.server_module()
        d = core.AnomalyDetector(window=8)
        lock = threading.Lock()
        started, release = threading.Event(), threading.Event()
        def slow(a):
            started.set()
            release.wait(3)
            return {'llm': 'done', 'explanation_mode': 'llm', 'fallback_reason': None}
        worker = server.ExplanationWorker(d, lock, interpreter=slow, capacity=1)
        worker.start()
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.multiple(server, detector=d, _alert_lock=lock):
                for ts in range(8):
                    server.process_readings([frame(ts)], worker)
                server.process_readings([frame(8, temp=40)], worker)
                self.assertTrue(started.wait(1))
                # No new thread per alert; extra alerts become immediate rule fallback.
                a = d.alerts[0]
                with lock:
                    worker.submit(dict(a))
                    overflow = dict(a)
                    worker.submit(overflow)
                self.assertEqual(overflow['fallback_reason'], 'queue_full')
                self.assertLessEqual(worker.pending, 1)
                begin = time.monotonic()
                for path in ('/status', '/alerts', '/'):
                    with urllib.request.urlopen(f'http://127.0.0.1:{httpd.server_port}{path}', timeout=0.8) as response:
                        body = response.read()
                    if path == '/alerts':
                        self.assertEqual(json.loads(body)[0]['explanation_mode'], 'rule')
                self.assertLess(time.monotonic() - begin, 0.8)
                release.set()
                deadline = time.monotonic() + 2
                while d.alerts[0]['explanation_mode'] != 'llm' and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(d.alerts[0]['explanation_mode'], 'llm')
        finally:
            release.set()
            worker.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(2)

    def test_command_line_service_polls_local_sensor_and_deduplicates_snapshot(self):
        class Sensor(BaseHTTPRequestHandler):
            count = 0
            def do_GET(self):
                ts = min(Sensor.count, 8)
                Sensor.count += 1
                body = json.dumps([frame(ts, temp=40 if ts == 8 else 20)]).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        sensor = ThreadingHTTPServer(('127.0.0.1', 0), Sensor)
        sensor_thread = threading.Thread(target=sensor.serve_forever, daemon=True)
        sensor_thread.start()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        env = dict(os.environ, ANOM_PORT=str(port), POLL_INTERVAL='0.02', USE_LLM='0',
                   ANOM_WINDOW='8', ANOM_Z='3', ANOM_STD_FLOOR='0.000001',
                   SENSOR_URL=f'http://127.0.0.1:{sensor.server_port}/latest')
        process = subprocess.Popen([sys.executable, 'run.py'], env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            deadline = time.monotonic() + 6
            alerts = []
            while time.monotonic() < deadline and not alerts:
                self.assertIsNone(process.poll(), 'command line service exited early')
                try:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}/alerts', timeout=0.5) as r:
                        alerts = json.load(r)
                except OSError:
                    pass
                time.sleep(0.02)
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0]['baseline_count'], 8)
            time.sleep(0.1)
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/alerts', timeout=1) as r:
                self.assertEqual(len(json.load(r)), 1)
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/status', timeout=1) as r:
                status = json.load(r)
                self.assertTrue(status['sensor_ok'])
                self.assertEqual(status['mode'], 'rule')
        finally:
            process.terminate()
            process.communicate(timeout=3)
            sensor.shutdown()
            sensor.server_close()
            sensor_thread.join(2)


if __name__ == '__main__':
    unittest.main()
