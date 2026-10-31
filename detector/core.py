#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
edge-anomaly-llm · 核心检测逻辑

- 对传感器流做滚动窗口 Z-score 异常检测（纯标准库，零依赖）
- 命中异常时给出规则化中文告警
- 可选：调用本地 Ollama 大模型，生成「现象 → 原因 → 处置建议」解读
"""
import json
import math
import os
import time
import urllib.request
import statistics
import sys
from collections import OrderedDict, deque

WINDOW = int(os.environ.get("ANOM_WINDOW", "30"))      # 滚动窗口
Z_THRESH = float(os.environ.get("ANOM_Z", "3.0"))      # Z 分数阈值
STD_FLOOR = float(os.environ.get("ANOM_STD_FLOOR", "0.000001"))
USE_LLM = os.environ.get("USE_LLM", "0") == "1"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")

_METRIC_CN = {"temp": "温度", "hum": "湿度", "lux": "光照"}


class AnomalyDetector:
    """维护每个节点的滚动窗口，逐帧判定异常。"""

    def __init__(self, window=WINDOW, z_thresh=Z_THRESH, std_floor=STD_FLOOR, max_nodes=256):
        if type(window) is not int or window < 8:
            raise ValueError("window must be an integer >= 8")
        if type(max_nodes) is not int or max_nodes < 1:
            raise ValueError("max_nodes must be a positive integer")
        for name, value in (("z_thresh", z_thresh), ("std_floor", std_floor)):
            if not finite_number(value) or value <= 0:
                raise ValueError(name + " must be finite and positive")
        self.window = window
        self.z = z_thresh
        self.std_floor = std_floor
        self.max_nodes = max_nodes
        self.buffers = OrderedDict()
        self.last_ts = {}
        self.alerts = deque(maxlen=300)
        self.mode = "rule"

    def _buf(self, node_id):
        if node_id not in self.buffers:
            if len(self.buffers) >= self.max_nodes:
                evicted, _ = self.buffers.popitem(last=False)
                del self.last_ts[evicted]
            self.buffers[node_id] = {k: deque(maxlen=self.window) for k in ("temp", "hum", "lux")}
        self.buffers.move_to_end(node_id)
        return self.buffers[node_id]

    def update(self, readings):
        new_alerts = []
        if not isinstance(readings, list):
            return new_alerts
        for r in readings:
            # Validate the entire node frame before touching any history/timestamp.
            if not valid_frame(r):
                continue
            if r["id"] in self.last_ts and r["ts"] <= self.last_ts[r["id"]]:
                continue
            buf = self._buf(r["id"])
            for k in ("temp", "hum", "lux"):
                series = list(buf[k])
                if len(series) < 8:
                    continue
                mean = statistics.mean(series)
                try:
                    std = statistics.pstdev(series)
                except OverflowError:
                    # Python 3.10 materializes variance before sqrt; variance
                    # may overflow even when the standard deviation is finite.
                    scale = max(abs(value) for value in series)
                    std = statistics.pstdev([value / scale for value in series]) * scale
                effective_std = max(std, self.std_floor)
                val = r[k]
                try:
                    z = (val - mean) / effective_std
                except OverflowError:
                    z = val / effective_std - mean / effective_std
                # Opposite-sign finite values may overflow their difference,
                # while division by a large std would yield an ordinary Z.
                if not math.isfinite(z):
                    z = val / effective_std - mean / effective_std
                if not math.isfinite(z):
                    z = math.copysign(sys.float_info.max, z)
                if abs(z) >= self.z:
                    direction = "升高" if z > 0 else "降低"
                    alert = {
                        "ts": r["ts"],
                        "time": time.strftime("%H:%M:%S"),
                        "node": r["id"],
                        "name": r.get("name", r["id"]),
                        "metric": k,
                        "metric_cn": _METRIC_CN[k],
                        "value": round(val, 2),
                        "z": round(z, 2),
                        "direction": direction,
                        "baseline_mean": mean,
                        "baseline_std": std,
                        "effective_std": effective_std,
                        "baseline_count": len(series),
                        "threshold": self.z,
                        "explanation_mode": "rule",
                        "fallback_reason": None,
                    }
                    alert["rule"] = rule_text(alert)
                    self.alerts.append(alert)
                    new_alerts.append(alert)
            for k in _METRIC_CN:
                buf[k].append(r[k])
            self.last_ts[r["id"]] = r["ts"]
        return new_alerts


def finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def valid_frame(r):
    return (isinstance(r, dict)
            and isinstance(r.get("id"), str) and 0 < len(r["id"]) <= 128
            and isinstance(r.get("name", r["id"]), str)
            and len(r.get("name", r["id"])) <= 256
            and all(finite_number(r.get(k)) for k in ("ts", "temp", "hum", "lux")))


def rule_text(a):
    m = a["metric_cn"]
    if a["metric"] == "temp":
        cause = "设备发热或通风不良" if a["direction"] == "升高" else "环境温度骤降或探头受冷源影响"
        advice = "检查散热/空调，确认传感器不被直吹" if a["direction"] == "升高" else "排查空调/门窗，确认探头位置"
    elif a["metric"] == "hum":
        cause = "环境潮湿或存在漏水" if a["direction"] == "升高" else "环境过于干燥或通风过强"
        advice = "检查防水与除湿" if a["direction"] == "升高" else "适当加湿并核查通风"
    else:
        cause = "光照突变（开灯/遮挡/日出）" if a["direction"] == "升高" else "光照缺失（熄灯/遮挡/断电）"
        advice = "确认照明与遮挡状态" 
    return f"【{m}{a['direction']}】实测 {a['value']}（Z={a['z']}）。可能原因：{cause}。处置建议：{advice}。"


def explain_alert(alert):
    """Return text plus the actual explanation source; network timeout is 20 s."""
    prompt = (
        f"你是一名物联网运维助手。传感器节点「{alert['name']}」({alert['node']}) 出现"
        f"{alert['metric_cn']}{alert['direction']}异常，实测值 {alert['value']}，"
        f"Z 分数 {alert['z']}。请用中文分三点简要说明：1) 现象 2) 可能原因 3) 处置建议，每点不超过 25 字。"
    )
    try:
        payload = json.dumps({"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}).encode()
        req = urllib.request.Request(
            OLLAMA_URL, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = json.load(resp).get("response", "")
            if isinstance(text, str) and text.strip():
                return {"llm": text.strip().replace("\n", " "),
                        "explanation_mode": "llm", "fallback_reason": None}
            reason = "empty_response"
    except Exception as exc:
        reason = "model_error:" + type(exc).__name__
    return {"llm": rule_text(alert), "explanation_mode": "rule", "fallback_reason": reason}


def llm_interpret(alert):
    """Backward compatible string API, including rule fallback."""
    return explain_alert(alert)["llm"]
