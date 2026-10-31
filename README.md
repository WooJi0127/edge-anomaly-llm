# edge-anomaly-llm · 传感器异常检测与可选模型解读

轮询 [iot-sensor-node](https://github.com/WooJi0127/iot-sensor-node) 的 `/latest`，用每节点历史窗口检测温度、湿度和光照突变，再展示规则告警。可选 Ollama 只解释告警，不参与数值判定。Python 3.10+，默认只需标准库。

## 三分钟启动

在另一个终端启动同级传感器项目的 `python run.py`（端口 8000），再在本仓库执行：

```powershell
# Windows PowerShell
python run.py
Invoke-RestMethod http://localhost:8001/status
Invoke-RestMethod http://localhost:8001/alerts
```

```sh
# POSIX shell
python3 run.py
curl http://localhost:8001/status
curl http://localhost:8001/alerts
```

浏览器打开 [告警时间线](http://localhost:8001/)。`sensor_ok: true` 表示最近一次轮询得到 JSON 列表，不代表每帧都通过校验。每节点前 8 个有效、时间递增的样本仅暖机，第 9 个开始评分。没有异常时 `/alerts` 返回 `[]` 是正常结果；随机模拟不保证几十秒内必有告警。

可选模式需要自行安装运行 Ollama 并准备模型：

```powershell
$env:USE_LLM='1'
$env:OLLAMA_MODEL='qwen2.5:1.5b'
$env:SENSOR_URL='http://localhost:8000/latest'
python run.py
```

```sh
USE_LLM=1 OLLAMA_MODEL=qwen2.5:1.5b SENSOR_URL=http://localhost:8000/latest python3 run.py
```

`/status.configured_mode` 表示请求模型的配置；`mode` 表示最新保留告警的实际解释来源（无告警时为 rule）。等待模型时仍使用规则；失败、空响应或队列满时显示 `fallback_reason`。解释中的原因是待核实建议。

## 配置与接口

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `SENSOR_URL` | `http://localhost:8000/latest` | JSON 列表数据源 |
| `ANOM_PORT` | `8001` | 时间线、`/alerts`、`/status` |
| `ANOM_WINDOW` | `30` | 每指标历史数，整数且至少 8 |
| `ANOM_Z` | `3.0` | 有限正数；`abs(z)>=threshold` 告警 |
| `ANOM_STD_FLOOR` | `0.000001` | 有限正数，标准差下限 |
| `POLL_INTERVAL` | `1.0` | 秒，有限正数 |
| `USE_LLM` | `0` | `1` 启用单个后台解释线程 |
| `OLLAMA_URL` | `http://localhost:11434/api/generate` | 模型接口 |
| `OLLAMA_MODEL` | `qwen2.5:1.5b` | 已准备的模型 |

输入示例：`[{"id":"node-1","name":"实验室","ts":1000,"temp":24.5,"hum":55,"lux":300}]`。`ts` 沿用源的毫秒时间基准，必须按节点递增；模拟标签 `anomaly` 不参与判断。坏帧整帧忽略，同批其他有效节点继续处理。

原有告警字段保留，新增 `baseline_mean/baseline_std/effective_std/baseline_count/threshold/explanation_mode/fallback_reason`，模型任务另有 `explanation_pending`。最多保留 300 告警、256 节点，每指标最多 window 个值；淘汰最久未接收新样本的节点，重新出现时重新暖机。

## 验证与结果

```powershell
python -m unittest discover -s tests -v
python scripts/evaluate.py --seed 20261031
```

POSIX 使用 `python3`。2026-10-31 在 Windows、Python 3.14.4 上通过 19 项测试，含实际命令行启动、本地假传感器轮询、HTTP 与慢模型并发、队列满和空响应回退。CI 配置 Python 3.10/3.14，远端结果须另查。

固定种子离线评测每场景 400 样本，暖机前 8 样本排除，窗口 30、阈值 3、下限 `1e-6`。完整指标和混淆矩阵见 [技术说明](docs/technical.md)。

| 场景 | 当前值参与基线 F1 | 仅历史基线 F1 |
|---|---:|---:|
| 正常噪声 | 0（误报 0） | 0（误报 6） |
| 孤立突变 | 1.000 | 0.750 |
| 持续偏移 | 0.02469 | 0.05600 |

历史基线修正了当前点稀释自身偏差的问题，但本组数据误报更多，孤立突变 F1 更低。持续偏移召回率仍仅约 2.92%：所有有效样本包括异常都会进入历史，基线逐渐适应偏移。评测只代表合成场景，没有人为去抖隐藏误报，无正样本时 precision/recall/F1 按约定记 0。

## 资料与限制

[数据契约、算法与评测](docs/technical.md) · [60–90 秒演示、可核实项目措辞、面试问题](docs/demo.md)

未验证真实硬件、长期稳定性、生产负载及真实模型生成质量；模型分支通过受控替身验证。标准库 HTTP 服务用于本地演示，默认监听所有网卡。重启清空状态；快照轮询可能漏掉两次轮询间读数；设备时钟回拨后，直到超过已接受时间或节点淘汰才会接收。单个标准差下限在各指标原单位中含义不同，真实部署前需按噪声标定。

MIT License，见 [LICENSE](LICENSE)。

## 第一次接触这个项目，可以这样理解

**主要做什么？** 持续查看温度、湿度和光照，把与近期历史差别很大的读数列成告警并给检查建议。一个异常值不能直接证明设备坏了。

**怎样执行？** 定时取最新数据，先检查格式、跳过重复或倒序时间。每节点积累 8 次历史后，用已有历史判断新读数，再把新读数加入窗口。默认立即给中文规则说明；开启模型后后台补充解释，网页仍可读取告警。

**怎样启动？** 安装 Python 3.10 或更新版本，在传感器项目先运行 `python run.py`，然后在本项目运行同一命令，打开 `http://localhost:8001/`。不启用模型也能完成数值检测。

**如何判断成功？** `/status` 的 `sensor_ok` 为 `true`，页面显示已连接；暖机后出现突变时，告警显示节点、指标、Z 分数与中文建议。没有突变时空列表正常。运行 `python -m unittest discover -s tests -v` 看到 `OK`，表示包含本地假传感器和网页读取的自动检查通过。
