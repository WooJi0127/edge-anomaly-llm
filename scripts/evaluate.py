"""Offline, seeded sample-level comparison. No model or sensor server needed."""
import argparse
import json
import random
import statistics
import sys
from collections import deque
from pathlib import Path

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from detector.core import AnomalyDetector


def scenarios(seed=20261031):
    """Independent synthetic benchmark; labels live outside feature dictionaries."""
    rng = random.Random(seed)
    base = [dict(id='evaluation', ts=i * 1000,
                 temp=20 + rng.gauss(0, 0.4), hum=50 + rng.gauss(0, 0.8),
                 lux=300 + rng.gauss(0, 5)) for i in range(400)]
    result = {}
    for name in ('normal_noise', 'isolated_spikes', 'sustained_shift'):
        rows = [dict(r) for r in base]
        labels = [False] * len(rows)
        for i, r in enumerate(rows):
            spike = name == 'isolated_spikes' and i in (50, 110, 170, 230, 290, 350)
            shift = name == 'sustained_shift' and i >= 160
            if spike or shift:
                r['temp'] += 5
                r['hum'] -= 8
                labels[i] = True
        result[name] = rows, labels
    return result


def metrics(labels, predictions):
    if len(labels) != len(predictions):
        raise ValueError('labels/predictions length mismatch')
    tp = sum(bool(y) and bool(p) for y, p in zip(labels, predictions))
    fp = sum(not y and bool(p) for y, p in zip(labels, predictions))
    fn = sum(bool(y) and not p for y, p in zip(labels, predictions))
    tn = len(labels) - tp - fp - fn
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return dict(tp=tp, fp=fp, fn=fn, tn=tn, precision=precision, recall=recall,
                f1=2 * precision * recall / (precision + recall) if precision + recall else 0.0,
                samples=len(labels))


def compare(rows, labels, window=30, z_thresh=3.0, std_floor=1e-6):
    if len(rows) != len(labels):
        raise ValueError('rows/labels length mismatch')
    detector = AnomalyDetector(window=window, z_thresh=z_thresh, std_floor=std_floor)
    inclusive = {k: deque(maxlen=window) for k in ('temp', 'hum', 'lux')}
    predictions = {'original_inclusive': [], 'history_only': []}
    for i, row in enumerate(rows):
        historical_alert = bool(detector.update([row]))
        for k, buf in inclusive.items():
            buf.append(row[k])
        # Original ordering (append, then score), with matched floor and warmup.
        inclusive_alert = any(abs((row[k] - statistics.mean(buf)) /
                                 max(statistics.pstdev(buf), std_floor)) >= z_thresh
                              for k, buf in inclusive.items())
        if i >= 8:
            predictions['original_inclusive'].append(inclusive_alert)
            predictions['history_only'].append(historical_alert)
    return {method: metrics(labels[8:], values) for method, values in predictions.items()}


def evaluate(seed=20261031):
    return {'seed': seed, 'parameters': {'window': 30, 'z_thresh': 3.0,
                                       'std_floor': 1e-6, 'warmup_excluded': 8},
            'scenarios': {name: compare(rows, labels) for name, (rows, labels) in scenarios(seed).items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seed', type=int, default=20261031)
    args = parser.parse_args()
    print(json.dumps(evaluate(args.seed), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
