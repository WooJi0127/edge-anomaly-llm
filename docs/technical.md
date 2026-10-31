# 技术说明

## 数据与算法

`/latest` 返回列表。单帧 `id` 为 1–128 字符字符串；`name` 可省略，存在时为最多 256 字符字符串；`ts/temp/hum/lux` 均为有限 int/float，拒绝 bool、数字字符串和非有限数。整帧验证后才创建/更新状态，坏帧不影响同批其他节点。额外字段包括 `anomaly` 不参与特征。

每节点每指标的历史 H：μ=mean(H)，σ=pstdev(H)（总体标准差，分母 len(H)），有效标准差=max(σ,std_floor)，z=(当前值-μ)/有效标准差。至少 8 个历史样本才评分，`abs(z)>=threshold` 告警，然后当前值进入窗口，包括异常值。告警给出 μ、原始 σ、有效标准差、样本数与阈值。极大数值相减溢出时先分别除以有效标准差再相减，保留仍可表示的 Z；只有最终 Z 仍超出浮点范围才饱和到最大有限值，避免 JSON NaN/Infinity 和中间溢出导致的误报。

`AnomalyDetector(window=30,z_thresh=3,std_floor=1e-6,max_nodes=256)` 的 `update(readings)` 返回新告警列表，核心无模型 I/O。检测器自身不保证线程安全；服务统一锁保护检测、写回模型结果和响应快照。命令行 `python run.py` 与包导入 `from detector.core import AnomalyDetector` 均可用。

每指标 deque 长度 window，最多 max_nodes 个节点，淘汰最久未接受新样本者，同时删除时间戳。重复/倒序/坏帧不刷新节点顺序。淘汰或重启后重新暖机，所以去重只覆盖驻留状态和本进程。告警 deque 最多 300 条。状态空间 O(max_nodes × window × 3 + 300)。

## 模型与并发

先发布规则，1 个 daemon worker 处理模型任务；待处理队列最多 32 项，另最多 1 项执行。队列满时保留规则并记 `queue_full`，不等待、不逐告警创建线程。网络请求在 HTTP 共享锁外，结果写回短暂持锁。timeout=20 秒是 socket 操作超时，不是强制取消任意第三方执行的墙钟截止时间。退出发停止信号并短暂 join；daemon 不阻挡退出，队列任务不保证完成。

实际来源为 `explanation_mode:rule/llm`；空白、缺失、非字符串响应回退并记 `empty_response`，异常记 `model_error:<类型>`。模型任务完成时 `llm` 保持字符串兼容性，回退内容为规则。`/status.mode` 来自最新保留告警；`configured_mode` 表示是否配置模型，`explanation_pending/fallback_reason` 说明等待与回退。没有告警时实际模式为 rule。

一手资料：[Ollama Generate](https://docs.ollama.com/api/generate)（使用 model/prompt/stream:false 和 response）；[Python http.server](https://docs.python.org/3/library/http.server.html)；[unittest](https://docs.python.org/3/library/unittest.html)。模型仅负责文字建议，不评估建议正确性。

## 评测复现

`python scripts/evaluate.py --seed 20261031` 或 `python -m scripts.evaluate`，无需网络。独立合成基准不复用测试夹具：400 次采样，高斯噪声温度 20±0.4、湿度 50±0.8、光照 300±5。三场景使用同一噪声：正常；索引 50/110/170/230/290/350 温度 +5、湿度 -8；索引 160 起持续相同偏移，所有后续样本标异常。

比较 append-then-score 原顺序与生产 history-only：窗口 30、阈值 3、下限 1e-6，共同排除前 8 暖机样本。任一指标命中即样本命中，多指标不重复计分。原方法在非零极小标准差时也统一使用相同下限，隔离窗口顺序差异；本基准噪声远大于下限，不影响这里结果。原实现第 8 点可评分的差异用共同暖机排除处理。

布尔标签单独存储，仅用于计分；帧只有 id/ts/temp/hum/lux。precision=TP/(TP+FP)，recall=TP/(TP+FN)，F1=2PR/(P+R)，零分母记 0。每场景计分 392 样本。2026-10-31，Windows Python 3.14.4 实测：

| 场景 | 方法 | TP | FP | FN | TN | Precision | Recall | F1 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| 正常 | original_inclusive | 0 | 0 | 0 | 392 | 0 | 0 | 0 |
| 正常 | history_only | 0 | 6 | 0 | 386 | 0 | 0 | 0 |
| 孤立突变 | original_inclusive | 6 | 0 | 0 | 386 | 1 | 1 | 1 |
| 孤立突变 | history_only | 6 | 4 | 0 | 382 | 0.6 | 1 | 0.75 |
| 持续偏移 | original_inclusive | 3 | 0 | 237 | 152 | 1 | 0.0125 | 0.024691 |
| 持续偏移 | history_only | 7 | 3 | 233 | 149 | 0.7 | 0.029167 | 0.056 |

不能得出整体提升：排除当前点后正常误报增加，孤立突变精度下降，持续偏移仍漏 233 点。所有有效值进入历史，基线会适应偏移；未加入去抖或按标签事后调参。真实应用仍需独立数据、噪声标定、漂移策略和报警成本分析。

## 验证边界

本地 19 项 unittest 覆盖暖机、整帧校验和不污染状态、节点隔离/容量、重复与倒序、常量与极端值（含中间溢出而 Z 可表示的正负两种情况）、模型空响应/失败、慢模型时真实 HTTP 读取、命令行服务轮询本地假传感器，以及评测样本计分。CI 配置 3.10/3.14，但未把远端运行当成本地已通过。未验证真实 Ollama、真实硬件、长期运行或生产容量。
