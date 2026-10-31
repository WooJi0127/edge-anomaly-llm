#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
edge-anomaly-llm 启动入口

用法：
    python run.py                 # 规则模式（默认，零依赖）
    USE_LLM=1 OLLAMA_MODEL=qwen2.5:1.5b python run.py   # 启用本地大模型告警解读
    SENSOR_URL=http://host:8000/latest python run.py     # 指向真实/其他传感器源
"""
from detector.server import main

if __name__ == "__main__":
    main()
