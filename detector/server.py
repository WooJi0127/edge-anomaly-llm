#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
edge-anomaly-llm · 服务 + Web 时间线

- 轮询 iot-sensor-node 的 /latest 快照，喂给 AnomalyDetector
- 提供 Web 时间线（/）、告警列表（/alerts）、状态（/status）
- 同时演示「边缘异常检测 + 大模型告警解读」的端-边-云联动
"""
import json
import os
import math
import queue
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .core import AnomalyDetector, USE_LLM, explain_alert

PORT = int(os.environ.get("ANOM_PORT", "8001"))
SENSOR_URL = os.environ.get("SENSOR_URL", "http://localhost:8000/latest")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))
if not math.isfinite(POLL_INTERVAL) or POLL_INTERVAL <= 0:
    raise ValueError("POLL_INTERVAL must be finite and positive")

detector = AnomalyDetector()
_sensor_ok = {"ok": False, "last": "从未连接"}
_alert_lock = threading.Lock()


class ExplanationWorker:
    """One worker and a bounded pending queue; HTTP locks never cover model I/O."""

    def __init__(self, detector, lock, interpreter=explain_alert, capacity=32):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self.detector, self.lock, self.interpreter = detector, lock, interpreter
        self.queue = queue.Queue(maxsize=capacity)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name="alert-explanation")

    @property
    def pending(self):
        return self.queue.qsize()

    def start(self):
        self.thread.start()

    def submit(self, alert):
        # Caller holds the shared lock. put_nowait never waits for the model.
        alert["explanation_pending"] = True
        try:
            self.queue.put_nowait(alert)
        except queue.Full:
            alert.update(explanation_pending=False, fallback_reason="queue_full")

    def _run(self):
        while not self.stop.is_set():
            try:
                alert = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                result = self.interpreter(dict(alert))
            except Exception as exc:
                result = {"llm": alert["rule"], "explanation_mode": "rule",
                          "fallback_reason": "model_error:" + type(exc).__name__}
            with self.lock:
                alert.update(result, explanation_pending=False)
            self.queue.task_done()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=0.2)


def process_readings(readings, worker=None):
    with _alert_lock:
        alerts = detector.update(readings)
        if worker is not None:
            for alert in alerts:
                worker.submit(alert)
    return alerts


def _poll_loop(stop=None, worker=None):
    stop = stop or threading.Event()
    while not stop.is_set():
        try:
            with urllib.request.urlopen(SENSOR_URL, timeout=5) as r:
                readings = json.load(r)
            if not isinstance(readings, list):
                raise ValueError("sensor payload must be a list")
            process_readings(readings, worker)
            with _alert_lock:
                _sensor_ok["ok"] = True
                _sensor_ok["last"] = time.strftime("%H:%M:%S")
        except Exception:
            with _alert_lock:
                _sensor_ok["ok"] = False
        stop.wait(POLL_INTERVAL)


def _html():
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "..", "dashboard", "timeline.html")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, _html(), "text/html; charset=utf-8")
        elif parsed.path == "/alerts":
            with _alert_lock:
                data = [dict(a) for a in detector.alerts]
            self._send(200, json.dumps(data, ensure_ascii=False))
        elif parsed.path == "/status":
            with _alert_lock:
                latest = detector.alerts[-1] if detector.alerts else {}
                data = {"sensor_ok": _sensor_ok["ok"], "sensor_last": _sensor_ok["last"],
                        "mode": latest.get("explanation_mode", "rule"),
                        "configured_mode": "llm" if USE_LLM else "rule",
                        "explanation_pending": latest.get("explanation_pending", False),
                        "fallback_reason": latest.get("fallback_reason"),
                        "alert_count": len(detector.alerts)}
            self._send(200, json.dumps(data, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    stop = threading.Event()
    worker = ExplanationWorker(detector, _alert_lock) if USE_LLM else None
    if worker:
        worker.start()
    poller = threading.Thread(target=_poll_loop, args=(stop, worker), daemon=True)
    poller.start()
    print(f"[edge-anomaly-llm] Started (configured mode: {'LLM' if USE_LLM else 'rule'})")
    print(f"  Timeline: http://localhost:{PORT}/")
    print(f"  Alerts:   http://localhost:{PORT}/alerts")
    print(f"  Status:   http://localhost:{PORT}/status")
    print(f"  Sensor:   {SENSOR_URL}")
    print("  Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        stop.set()
        server.server_close()
        poller.join(timeout=0.2)
        if worker:
            worker.close()


if __name__ == "__main__":
    main()
