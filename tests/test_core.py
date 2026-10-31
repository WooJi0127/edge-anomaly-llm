import copy
import math
import statistics
import unittest
from unittest.mock import patch

from detector.core import AnomalyDetector


def frame(ts, temp=20, node='n1', **extra):
    return dict(id=node, name='实验节点', ts=ts, temp=temp, hum=50, lux=300, **extra)


class DetectorTests(unittest.TestCase):
    def warm(self, detector, node='n1', count=8):
        for ts in range(count):
            self.assertEqual(detector.update([frame(ts, node=node)]), [])

    def test_history_only_eight_samples_and_evidence(self):
        d = AnomalyDetector(window=8)
        self.warm(d)
        alerts = d.update([frame(8, temp=40)])
        self.assertEqual(len(alerts), 1, 'current spike must not dilute its own baseline')
        a = alerts[0]
        self.assertEqual(a['baseline_mean'], 20)
        self.assertEqual(a['baseline_std'], 0)
        self.assertEqual(a['baseline_count'], 8)
        self.assertEqual(a['threshold'], 3)
        self.assertEqual(a['explanation_mode'], 'rule')
        self.assertEqual(list(d.buffers['n1']['temp']), [20] * 7 + [40])

    def test_eighth_frame_is_still_warmup(self):
        d = AnomalyDetector(z_thresh=1)
        self.warm(d, count=7)
        self.assertEqual(d.update([frame(7, temp=100)]), [])

    def test_duplicate_out_of_order_and_nodes(self):
        d = AnomalyDetector()
        self.warm(d)
        before = copy.deepcopy(d.buffers)
        self.assertEqual(d.update([frame(7, 999), frame(3, 999)]), [])
        self.assertEqual(d.buffers, before)
        self.assertEqual(d.update([frame(8, 999, node='n2')]), [])
        self.assertEqual(len(d.buffers['n2']['temp']), 1)

    def test_invalid_whole_frame_never_mutates_but_valid_sibling_survives(self):
        bad = [dict(frame(9), hum=float('nan')), dict(frame(9), temp=True),
               dict(frame(9), lux=float('inf')), dict(frame(9), ts=False),
               dict(frame(9), id=[]), dict(frame(9), name=12),
               dict(frame(9), temp='20'), {'id': 'n1', 'ts': 9, 'temp': 20}, None]
        for item in bad:
            with self.subTest(item=item):
                d = AnomalyDetector()
                self.warm(d)
                before = copy.deepcopy(d.buffers['n1'])
                self.assertEqual(d.update([item, frame(10, node='valid')]), [])
                self.assertEqual(d.buffers['n1'], before)
                self.assertIn('valid', d.buffers)
                self.assertEqual(d.update([frame(9, 40)])[0]['baseline_count'], 8)

    def test_configuration_validation(self):
        for kwargs in [{'window': 7}, {'window': True}, {'window': 8.5},
                       {'z_thresh': 0}, {'z_thresh': math.nan}, {'z_thresh': True},
                       {'std_floor': 0}, {'std_floor': math.inf}, {'max_nodes': 0}]:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    AnomalyDetector(**kwargs)

    def test_constant_series_has_no_alert_and_state_is_bounded(self):
        d = AnomalyDetector(window=8, max_nodes=2)
        self.warm(d)
        self.assertEqual(d.update([frame(8)]), [])
        d.update([frame(0, node='n2'), frame(0, node='n3')])
        self.assertEqual(set(d.buffers), {'n2', 'n3'})
        self.assertEqual(set(d.last_ts), set(d.buffers))

    def test_non_list_payload_is_ignored(self):
        d = AnomalyDetector()
        self.assertEqual(d.update({'id': 'n1'}), [])
        self.assertEqual(d.buffers, {})

    def test_extreme_finite_values_stay_json_serializable(self):
        import json
        d = AnomalyDetector()
        for ts in range(8):
            d.update([frame(ts, temp=-(10 ** 308))])
        alerts = d.update([frame(8, temp=10 ** 308)])
        self.assertEqual(len(alerts), 1)
        json.dumps(alerts, allow_nan=False)

    def test_std_floor_limits_sensitivity_for_quiet_nonconstant_history(self):
        d = AnomalyDetector(std_floor=1)
        for ts in range(8):
            d.update([frame(ts, temp=0.1 * (ts % 2))])
        self.assertEqual(d.update([frame(8, temp=1)]), [])

    def test_large_finite_difference_preserves_representable_z_score(self):
        for sign in (-1, 1):
            with self.subTest(sign=sign):
                d = AnomalyDetector(z_thresh=3)
                history = [-sign * 1.7e308] * 7 + [sign * 1.7e308]
                for ts, value in enumerate(history):
                    d.update([frame(ts, temp=value)])
                self.assertEqual(d.update([frame(8, temp=sign * 1.7e308)]), [])

                # The exact Z here is sign * sqrt(7), even though val-mean
                # overflows a binary64 intermediate. At threshold 2 it alerts.
                d = AnomalyDetector(z_thresh=2)
                for ts, value in enumerate(history):
                    d.update([frame(ts, temp=value)])
                alert = d.update([frame(8, temp=sign * 1.7e308)])[0]
                self.assertAlmostEqual(alert['z'], round(sign * math.sqrt(7), 2))

    def test_python310_variance_overflow_uses_scaled_standard_deviation(self):
        # Python 3.10 computes sqrt(pvariance), materializing variance as a
        # float before taking its square root. Reproduce that real CI path.
        def legacy_pstdev(series):
            return math.sqrt(statistics.pvariance(series))
        d = AnomalyDetector(z_thresh=2)
        for ts, value in enumerate([-1.7e308] * 7 + [1.7e308]):
            d.update([frame(ts, temp=value)])
        with patch('detector.core.statistics.pstdev', side_effect=legacy_pstdev):
            alert = d.update([frame(8, temp=1.7e308)])[0]
        self.assertAlmostEqual(alert['baseline_std'] / 1.7e308, math.sqrt(7) / 4)
        self.assertEqual(alert['z'], round(math.sqrt(7), 2))


if __name__ == '__main__':
    unittest.main()
