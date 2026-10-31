import unittest

from scripts.evaluate import compare, metrics, scenarios


class EvaluationTests(unittest.TestCase):
    def test_sample_confusion_and_metrics_hand_checked(self):
        self.assertEqual(metrics([False, True, True, False], [False, True, False, True]),
                         {'tp': 1, 'fp': 1, 'fn': 1, 'tn': 1,
                          'precision': 0.5, 'recall': 0.5, 'f1': 0.5, 'samples': 4})

    def test_same_warmup_and_multimetric_sample_count_once(self):
        rows = [dict(id='eval', ts=i, temp=20, hum=50, lux=300) for i in range(9)]
        rows[-1].update(temp=40, hum=100, lux=600)
        result = compare(rows, [False] * 8 + [True], window=8)
        self.assertEqual(result['history_only']['tp'], 1)
        self.assertEqual(result['original_inclusive']['fn'], 1)
        self.assertEqual(result['history_only']['samples'], 1)

    def test_seeded_dataset_and_labels_separate_from_features(self):
        data = scenarios(20261031)
        self.assertEqual(data, scenarios(20261031))
        self.assertEqual(set(data), {'normal_noise', 'isolated_spikes', 'sustained_shift'})
        for rows, labels in data.values():
            self.assertEqual(len(rows), len(labels))
            self.assertTrue(all(set(r) == {'id', 'ts', 'temp', 'hum', 'lux'} for r in rows))
        self.assertNotEqual(data, scenarios(1))


if __name__ == '__main__':
    unittest.main()
