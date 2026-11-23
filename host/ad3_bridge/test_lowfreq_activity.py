import unittest

import numpy as np

import lowfreq_activity as lf


def synthetic(seconds=60.0, burst=(40.0, 42.0), burst_mv=1.0, seed=1):
    """CH1/CH2 raw at 100 kHz: small 1-40 Hz noise, 50 Hz hum, and a 10 Hz burst on CH2."""
    rng = np.random.default_rng(seed)
    n = int(seconds * lf.FS)
    t = np.arange(n) / lf.FS
    slow = np.repeat(rng.normal(0, 3e-5, (n // 100, 2)), 100, axis=0)   # broadband floor
    x = slow + 1.5e-3 * np.sin(2 * np.pi * 50 * t)[:, None]              # mains hum on both
    on = (t >= burst[0]) & (t < burst[1])
    x[on, 1] += burst_mv * 1e-3 * np.sin(2 * np.pi * 10 * t[on])
    return x


def track(x, chunk=777_777):
    tracker = lf.LowTracker()
    for i in range(0, len(x), chunk):
        tracker.push(x[i:i + chunk])
    return tracker.arrays()


class LowFreqActivityTest(unittest.TestCase):
    def test_burst_is_marked_and_hum_is_rejected(self):
        rows = track(synthetic())
        res = lf.judge(rows)
        self.assertEqual(res['biological_activity'], 'UNKNOWN')
        spans = res['activity_like_spans']
        self.assertEqual(len(spans), 1)
        self.assertLessEqual(spans[0]['t0_s'], 40.3)
        self.assertGreaterEqual(spans[0]['t1_s'], 41.7)
        self.assertGreater(spans[0]['ch2_over_ch1'], 3)
        # 1.5 mV of 50 Hz hum must not dominate the 1-40 Hz floor
        t, ch1, ch2 = rows
        self.assertLess(np.median(ch2[(t > 15) & (t < 35)]), 1e-4)

    def test_chunking_does_not_change_the_numbers(self):
        x = synthetic(seconds=20.0, burst=(12.0, 13.0))
        a, b = track(x, 1_000_000), track(x, 123_457)
        for u, v in zip(a, b):
            np.testing.assert_allclose(u, v, rtol=1e-9, atol=1e-15)

    def test_arm_span_is_masked_and_early_windows_are_unknown(self):
        rows = track(synthetic())
        res = lf.judge(rows, arm_spans=[(39.5, 42.5)])
        self.assertEqual(res['activity_like_spans'], [])
        early = lf.judge(track(synthetic(seconds=8.0, burst=(5.0, 6.0))))
        self.assertEqual(early['activity_like_spans'], [])        # under 10 s of floor: unknown, not active
        self.assertGreater(early['unknown_s'], 7)


if __name__ == '__main__':
    unittest.main()
