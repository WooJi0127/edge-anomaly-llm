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
from collections import deque

WINDOW = int(os.environ.get("ANOM_WINDOW", "30"))      # 滚动窗口
Z_THRESH = float(os.environ.get("ANOM_Z", "3.0"))      # Z 分数阈值
USE_LLM = os.environ.get("USE_LLM", "0") == "1"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:1.5b")

_METRIC_CN = {"temp": "温度", "hum": "湿度", "lux": "光照"}


class AnomalyDetector:
    """维护每个节点的滚动窗口，逐帧判定异常。"""

    def __init__(self, window=WINDOW, z_thresh=Z_THRESH):
        self.window = window
        self.z = z_thresh
        self.buffers = {}
        self.alerts = deque(maxlen=300)
        self.mode = "llm" if USE_LLM else "rule"

    def _buf(self, node_id):
        if node_id not in self.buffers:
            self.buffers[node_id] = {k: deque(maxlen=self.window) for k in ("temp", "hum", "lux")}
        return self.buffers[node_id]

    def update(self, readings):
        new_alerts = []
        for r in readings:
            buf = self._buf(r["id"])
            for k in ("temp", "hum", "lux"):
                buf[k].append(r[k])
            for k in ("temp", "hum", "lux"):
                series = list(buf[k])
                if len(series) < 8:
                    continue
                mean = sum(series) / len(series)
                var = sum((x - mean) ** 2 for x in series) / len(series)
                std = math.sqrt(var) or 1e-6
                val = r[k]
                z = (val - mean) / std
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
                    }
                    alert["rule"] = rule_text(alert)
                    if USE_LLM:
                        alert["llm"] = llm_interpret(alert)
                    self.alerts.append(alert)
                    new_alerts.append(alert)
        return new_alerts


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


def llm_interpret(alert):
    """调用本地 Ollama 生成中文告警解读；失败则回退规则文本。"""
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
            return json.load(resp).get("response", "").strip().replace("\n", " ")
    except Exception:
        return rule_text(alert)
