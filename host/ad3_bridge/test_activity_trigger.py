"""Observation trigger from 1-40 Hz activity (activity_trigger.py, 2026-11-23)."""
import unittest

import numpy as np

import activity_trigger as at
import lowfreq_activity as lf
import lowfreq_forest as lff
from protocol import candidate_payload

FS = lf.FS


def signal(seconds, bursts, seed=1):
    """CH1 quiet, CH2 noise plus 10 Hz bursts [(t0, t1, amplitude)]."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * FS)) / FS
    ch2 = rng.normal(0, 2e-5, len(t))
    for a, b, amp in bursts:
        ch2 += np.where((t >= a) & (t < b), amp * np.sin(2 * np.pi * 10 * t), 0)
    ch1 = rng.normal(0, 2e-5, len(t))
    return np.column_stack((ch1, ch2))


def feed(trigger, values, block=7000):
    events = []
    for i in range(0, len(values), block):
        events += trigger.push(values[i:i + block])
    return events


class ActivityTriggerTest(unittest.TestCase):
    def test_same_windows_as_the_offline_forest_and_ratio(self):
        values = signal(60, [(20, 23, 3e-4), (40, 41, 2e-4)])
        trigger = at.ActivityTrigger()
        events = feed(trigger, values)
        rows = trigger.tracker.arrays()
        forest = lff.judge(rows, (), trigger.model)
        ratio = lf.judge(rows)
        # each trigger is the start of a span of the union of the two offline span lists
        union = sorted([(s['t0_s'], s['t1_s']) for s in forest['activity_like_spans']] +
                       [(s['t0_s'], s['t1_s']) for s in ratio['activity_like_spans']])
        merged = []
        for a, b in union:
            if merged and a - merged[-1][1] <= at.MERGE_GAP_S:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        self.assertEqual([round(e['t_start_s'], 3) for e in events], [round(a, 3) for a, _ in merged])
        self.assertTrue(any(19.5 <= e['t_start_s'] <= 21 for e in events))
        for e in events:
            self.assertEqual((e['detector'], e['quality']), (at.DETECTOR, 'DEVELOPMENT_CANDIDATE'))
            self.assertAlmostEqual(e['t_end_s'] - e['t_start_s'], lf.WINDOW_S, places=3)
            self.assertTrue(e['triggered_by'])

    def test_one_trigger_per_activity_and_none_while_busy(self):
        values = signal(60, [(20, 26, 3e-4), (40, 43, 3e-4)])
        trigger = at.ActivityTrigger()
        events = feed(trigger, values[:int(35 * FS)])
        self.assertEqual(len([e for e in events if 19 <= e['t_start_s'] <= 27]), 1)  # 6 s burst -> one trigger
        trigger.busy_begin(36.0)                       # an episode is running from 36 s ...
        later = feed(trigger, values[int(35 * FS):int(50 * FS)])
        self.assertFalse([e for e in later if 36 <= e['t_start_s']])
        trigger.busy_end(44.0)                          # ... until 44 s (+ settle)
        self.assertEqual(trigger.busy[-1][1], 44.0 + at.SETTLE_AFTER_BUSY_S)
        self.assertGreater(trigger.summary()['masked_windows'], 0)

    def test_b2_starts_a_trigger_unless_busy_or_inside_a_span(self):
        values = signal(60, [(40, 43, 3e-4)])
        trigger = at.ActivityTrigger(b2_config_sha256='c' * 64)
        b2 = lambda t: dict(t_start_s=t, confirmed_at_s=t + .1, peak_rms_V=1e-4, peak_score=6.0,
                            quality='DEVELOPMENT_CANDIDATE')
        events = feed(trigger, values[:int(20 * FS)])
        events += trigger.push(values[int(20 * FS):int(20.5 * FS)], [b2(20.1), dict(b2(20.3), quality='INCOMPLETE')])
        [first] = [e for e in events if 19 <= e['t_start_s'] <= 21]
        self.assertEqual((first['detector'], first['triggered_by'], first['b2_t_start_s']), (at.B2_DETECTOR, ['B2'], 20.1))
        self.assertEqual(candidate_payload('a' * 32, 1, first, trigger.fingerprint).count(' '), 6)  # B2 7-token form
        # a second B2 within MERGE_GAP_S of the span extends it instead of triggering again
        self.assertEqual(trigger.push(values[int(20.5 * FS):int(21 * FS)], [b2(20.6)]), [])
        trigger.busy_begin(22.0)
        later = feed(trigger, values[int(21 * FS):int(25 * FS)]) + trigger.push(values[int(25 * FS):int(25.2 * FS)], [b2(25.0)])
        self.assertEqual(later, [])
        self.assertEqual((trigger.b2_used, trigger.b2_masked), (1, 1))
        # the 1-40 Hz path still triggers by itself after the episode, as LOWFREQ_FOREST_RATIO
        trigger.busy_end(26.0)
        rest = feed(trigger, values[int(25.2 * FS):])
        self.assertTrue(any(e['detector'] == at.DETECTOR and 39.5 <= e['t_start_s'] <= 41 for e in rest))
        summary = trigger.summary()
        self.assertEqual((summary['detector'], summary['b2_triggers']), (at.TRIGGER, 1))
        self.assertNotEqual(trigger.fingerprint, at.ActivityTrigger().fingerprint)  # B2 config is part of it

    def test_wire_payload_names_the_detector(self):
        trigger = at.ActivityTrigger()
        event = dict(t_start_s=20.0, t_end_s=20.25, peak_rms_V=3e-5, peak_score=3.4,
                     detector=at.DETECTOR, quality='DEVELOPMENT_CANDIDATE')
        payload = candidate_payload('a' * 32, 1, event, trigger.fingerprint)
        self.assertTrue(payload.endswith(' ' + trigger.fingerprint + ' LOWFREQ_FOREST_RATIO'))
        self.assertEqual(payload.split()[1:6], ['1', '20000', '20250', '30', '3400'])
        # B2 keeps the 7-token form and its score floor of 4.0
        b2 = dict(event, detector='B2_guarded_mad')
        with self.assertRaises(ValueError):
            candidate_payload('a' * 32, 1, b2, 'b' * 64)
        b2['peak_score'] = 5.0
        self.assertEqual(len(candidate_payload('a' * 32, 1, b2, 'b' * 64).split()), 7)
        with self.assertRaises(ValueError):
            candidate_payload('a' * 32, 1, dict(event, detector='OTHER'), 'b' * 64)


if __name__ == '__main__':
    unittest.main()
