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
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from core import AnomalyDetector, USE_LLM

PORT = int(os.environ.get("ANOM_PORT", "8001"))
SENSOR_URL = os.environ.get("SENSOR_URL", "http://localhost:8000/latest")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "1.0"))

detector = AnomalyDetector()
_sensor_ok = {"ok": False, "last": "从未连接"}
_alert_lock = threading.Lock()


def _poll_loop():
    while True:
        try:
            with urllib.request.urlopen(SENSOR_URL, timeout=5) as r:
                readings = json.load(r)
            if isinstance(readings, list):
                with _alert_lock:
                    detector.update(readings)
                _sensor_ok["ok"] = True
                _sensor_ok["last"] = time.strftime("%H:%M:%S")
        except Exception:
            _sensor_ok["ok"] = False
        time.sleep(POLL_INTERVAL)


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
                data = list(detector.alerts)
            self._send(200, json.dumps(data, ensure_ascii=False))
        elif parsed.path == "/status":
            self._send(200, json.dumps({
                "sensor_ok": _sensor_ok["ok"],
                "sensor_last": _sensor_ok["last"],
                "mode": detector.mode,
                "alert_count": len(detector.alerts),
            }, ensure_ascii=False))
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *args):
        pass


def main():
    threading.Thread(target=_poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[edge-anomaly-llm] 异常检测服务已启动（模式: {'LLM' if USE_LLM else '规则'}）")
    print(f"  时间线: http://localhost:{PORT}/")
    print(f"  告警:   http://localhost:{PORT}/alerts")
    print(f"  状态:   http://localhost:{PORT}/status")
    print(f"  数据源: {SENSOR_URL}")
    print("  按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
