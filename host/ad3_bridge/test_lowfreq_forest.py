import importlib.util
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import numpy as np

import lowfreq_forest as lff

MODEL = lff.load_model()


def rows(n=400, burst=(200, 212), seed=3):
    rng = np.random.default_rng(seed)
    t = (np.arange(n) + .5) * .25
    ch1 = 1e-4 * (1 + .1 * rng.random(n))
    ch2 = 1e-4 * (1 + .1 * rng.random(n))
    ch2[burst[0]:burst[1]] *= 12
    return t, ch1, ch2


@unittest.skipIf(MODEL is None, 'ml/lowfreq_forest/lowfreq_forest_v1.json missing')
class LowFreqForestTest(unittest.TestCase):
    def test_model_shape_is_mcu_sized(self):
        self.assertEqual(MODEL['features'], lff.FEATURES)
        nodes = sum(len(t['feature']) for t in MODEL['trees'])
        self.assertLess(nodes, 2000)
        for tree in MODEL['trees']:
            n = len(tree['feature'])
            for f, l, r in zip(tree['feature'], tree['left'], tree['right']):
                if f >= 0:
                    self.assertTrue(0 < l < n and 0 < r < n and f < len(lff.FEATURES))

    def test_features_are_causal(self):
        t, ch1, ch2 = rows()
        _, X_full, u_full = lff.features((t, ch1, ch2))
        _, X_part, u_part = lff.features((t[:250], ch1[:250], ch2[:250]))
        np.testing.assert_array_equal(u_full[:250], u_part)
        np.testing.assert_allclose(X_full[:250][u_part], X_part[u_part])

    def test_judge_marks_a_burst_and_masks_the_arm(self):
        r = rows()
        res = lff.judge(r, model=MODEL, run='x')
        self.assertEqual(res['biological_activity'], 'UNKNOWN')
        self.assertFalse(res['in_training'])
        hit = [s for s in res['activity_like_spans'] if s['t0_s'] <= 53 and s['t1_s'] >= 50]
        self.assertTrue(hit)
        masked = lff.judge(r, [(49.0, 54.0)], model=MODEL)
        self.assertFalse([s for s in masked['activity_like_spans'] if s['t0_s'] < 54.3 and s['t1_s'] > 48.7])
        self.assertEqual(lff.judge(r, model=MODEL, run=MODEL['training_runs'][0])['in_training'], True)

    def test_no_model(self):
        self.assertIsNone(lff.load_model(Path(tempfile.gettempdir()) / 'no_such_forest.json'))
        with mock.patch.object(lff, 'load_model', return_value=None):
            res = lff.judge(rows())
        self.assertEqual((res['status'], res['activity_like_spans']), ('NO_MODEL', []))

    def test_header_is_generated_from_the_json(self):
        text = lff.HEADER_FILE.read_text(encoding='utf-8')
        self.assertIn(f"#define LFF_N_TREES {len(MODEL['trees'])}", text)
        self.assertIn(f"#define LFF_THRESHOLD {MODEL['threshold']:.4f}f", text)

    @unittest.skipIf(importlib.util.find_spec('ziglang') is None, 'zig compiler not installed')
    def test_mcu_core_matches_python(self):
        """The MCU core (lowfreq_forest_core.h, float32) replays the same windows as judge()."""
        root = Path(__file__).resolve().parents[2]
        t, ch1, ch2 = rows(n=800, burst=(300, 316), seed=5)
        ch2[520:530] *= 6
        arm_spans = [(150.0, 152.0)]
        arm = lff.lf._arm_mask(t, arm_spans)
        want = lff.judge((t, ch1, ch2), arm_spans, MODEL)
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / 'core.exe'
            subprocess.run([sys.executable, '-m', 'ziglang', 'c++', '-std=c++17', '-O1', '-Wno-nullability-completeness',
                            '-I', str(root / 'firmware/xiao_esp32s3/include'),
                            str(root / 'firmware/xiao_esp32s3/test/host/test_lowfreq_forest_core.cpp'), '-o', str(exe)], check=True)
            self.assertEqual(subprocess.run([str(exe)], capture_output=True, text=True).returncode, 0)  # self-checks
            text = '\n'.join(f'{round(a * 1000)} {b:.9g} {c:.9g} {int(d)}' for a, b, c, d in zip(t, ch1, ch2, arm))
            out = subprocess.run([str(exe), '--replay'], input=text, capture_output=True, text=True, check=True).stdout
        spans = [tuple(int(v) / 1000 for v in line.split()[1:3]) for line in out.splitlines() if line.startswith('S ')]
        self.assertEqual(len([l for l in out.splitlines() if l.startswith('W ')]), len(t))
        self.assertEqual(spans, [(s['t0_s'], s['t1_s']) for s in want['activity_like_spans']])
        self.assertTrue(spans)

if __name__ == '__main__':
    unittest.main()
