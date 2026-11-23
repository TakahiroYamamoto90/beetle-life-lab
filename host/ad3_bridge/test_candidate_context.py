"""Per-candidate CH2 / CH1 / sound context: reference numbers only, never in candidates.jsonl."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import candidate_context as cc  # noqa: E402
import disturbance as dz  # noqa: E402
from manager import BridgeManager  # noqa: E402
from review import ReviewStore  # noqa: E402
from test_review import RUN, recorded_run  # noqa: E402


class RatioTest(unittest.TestCase):
    def test_ch2_only_and_ch1_only_rises_are_told_apart(self):
        t = np.arange(0, 20, .02)
        ch2 = np.full_like(t, 1e-5)
        ch1 = np.full_like(t, 2e-5)
        dig = (t >= 10) & (t <= 10.5)
        ch2[dig] = 8e-5                       # in the case only
        outside = (t >= 15) & (t <= 15.4)
        ch1[outside] = 4e-4                   # reference sensor only
        a = cc.band_ratios(t, ch2, ch1, 10, 10.5)
        self.assertEqual((a['ch2_x'], a['ch1_x']), (8.0, 1.0))
        b = cc.band_ratios(t, ch2, ch1, 15, 15.4, other=[(10, 10.5)])
        self.assertEqual((b['ch2_x'], b['ch1_x'], b['ch1_over_ch2']), (1.0, 20.0, 20.0))

    def test_missing_baseline_or_channel_is_unknown_not_one(self):
        t = np.arange(0, 3, .02)
        r = cc.band_ratios(t, np.full_like(t, 1e-5), None, .2, .4)  # under 0.5 s of baseline
        self.assertEqual((r['ch2_x'], r['ch1_x'], r['ch1_over_ch2']), (None, None, None))
        zero = cc.band_ratios(t, np.full_like(t, 1e-5), np.zeros_like(t), 2.5, 2.8)
        self.assertIsNone(zero['ch1_x'])  # a dead reference channel gives no ratio

    def test_sound_ratio_uses_the_quiet_floor(self):
        rate = 1000
        audio = np.full(rate * 10, .01, np.float32)
        audio[5 * rate:5 * rate + 200] = .5
        floor = cc.audio_floor(audio, rate)
        self.assertAlmostEqual(cc.audio_ratio(audio, rate, 5.0, 5.2, floor), 50.0, places=1)
        self.assertIsNone(cc.audio_ratio(audio, rate, 9.9, 11, floor))  # outside the recording


def synthetic(seconds=10.0, seed=1):
    """CH1 / CH2 noise floor plus three 0.4 s events: a 100 Hz contact-like burst on CH2 at 3 s,
    an 800 Hz sound-like rise on CH2 at 6 s (low band stays at the hum), and a knock with a huge 3 kHz CH1 burst at 8.5 s."""
    rng = np.random.default_rng(seed)
    n = int(seconds * dz.FS)
    t = np.arange(n) / dz.FS
    ch1 = rng.normal(0, 1e-5, n)
    ch2 = rng.normal(0, 1e-5, n) + 1e-5 * np.sin(2 * np.pi * 50 * t)  # mains hum sets the low-band floor
    for a, f, amp in ((3.0, 100, 2e-4), (6.0, 800, 3e-5), (8.5, 120, 2e-4)):
        on = (t >= a) & (t < a + .4)
        ch2[on] += amp * np.sin(2 * np.pi * f * t[on])
    knock = (t >= 8.5) & (t < 8.9)
    ch1[knock] += 1e-3 * np.sin(2 * np.pi * 3000 * t[knock])
    return np.column_stack((ch1, ch2))


def flat_features(seconds, bumps=()):
    ft = np.arange(.11, seconds, .02)
    ch2 = np.full_like(ft, 1e-5)
    for a, b, level in bumps:
        ch2[(ft >= a) & (ft <= b)] = level
    return ft, ch2


class DisturbanceTest(unittest.TestCase):
    def test_block_size_does_not_change_the_numbers(self):
        data = synthetic(3.0)
        one = dz.BandTracker(); one.rows = __import__('collections').deque()
        one.push(data)
        many = dz.BandTracker(); many.rows = __import__('collections').deque()
        for start in range(0, len(data), 4999):
            many.push(data[start:start + 4999])
        for x, y in zip(one.arrays(), many.arrays()):
            np.testing.assert_allclose(x, y, rtol=1e-9, atol=1e-15)

    def test_contact_sound_and_knock_are_told_apart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'samples.f64'
            synthetic().astype('<f8').tofile(path)
            bands = dz.from_samples(path)
        features = flat_features(10, [(3.0, 3.1, 8e-5), (3.1, 3.4, 2e-5), (6.0, 6.4, 3e-5), (8.5, 8.9, 5e-5)])
        contact = dz.judge(bands, features, 3.0, 3.4)
        self.assertGreater(contact['ch2_low_peak_x'], 5)
        self.assertIs(contact['disturbance_like'], False)
        sound = dz.judge(bands, features, 6.0, 6.4, other=[(3.0, 3.4)])
        self.assertLess(sound['ch2_low_peak_x'], dz.LOW_MIN_X)
        self.assertLess(sound['ch2_crest'], dz.CREST_MIN)
        self.assertEqual((sound['disturbance_like'], sound['reasons']), (True, ['NO_LOW_BAND_RISE_AND_NOT_SHARP']))
        knock = dz.judge(bands, features, 8.5, 8.9, other=[(6.0, 6.4)])
        self.assertGreater(knock['ch2_low_peak_x'], dz.LOW_MIN_X)  # it shakes the case too
        self.assertEqual((knock['disturbance_like'], knock['reasons']), (True, ['CH1_HIGH_BAND_EXTREME']))
        self.assertEqual(knock['status'], dz.STATUS)

    def test_short_baseline_or_dead_reference_is_unknown(self):
        data = synthetic(3.0)
        data[:, 0] = 0
        tracker = dz.BandTracker()
        tracker.push(data)
        early = dz.judge(tracker.arrays(), flat_features(3), .2, .4)
        self.assertEqual((early['disturbance_like'], early['reasons']), (None, ['BASELINE_TOO_SHORT']))
        dead = dz.judge(tracker.arrays(), flat_features(3), 2.5, 2.8)
        self.assertIsNone(dead['ch1_high_peak_x'])  # zero CH1 gives no ratio, not "quiet"


class ReviewContextTest(unittest.TestCase):
    def test_review_table_reads_saved_files_and_xiao_answers(self):
        with recorded_run() as output:
            run = output / RUN
            (run / 'candidates.jsonl').write_text(json.dumps(dict(
                candidate_seq=1, t_start_s=2.0, t_end_s=2.4, peak_score=9.0)) + '\n', encoding='utf-8')
            (run / 'delivery.jsonl').write_text(json.dumps(dict(
                candidate_seq=1, status='acknowledged', observation=dict(
                    action='WAIT', reason_codes=['PASSIVE_CANDIDATE_NEW', 'SELF_ACTUATION_RECENT']))) + '\n',
                encoding='utf-8')
            result = ReviewStore(output).candidate_context(RUN)
            [item] = result['candidates']
            self.assertEqual((item['ch2_x'], item['ch1_x']), (1.0, 1.0))  # flat fixture features
            self.assertEqual(item['xiao'], dict(action='WAIT', reason_codes=['SELF_ACTUATION_RECENT']))
            self.assertIsNone(item['sound_x'])
            self.assertEqual(item['disturbance']['source'], 'COMPUTED_IN_REVIEW')  # replayed from samples.f64
            self.assertEqual(item['disturbance']['schema'], dz.SCHEMA)
            # A value saved during acquisition wins over the replay.
            (run / 'candidate_context.jsonl').write_text(json.dumps(dict(
                candidate_seq=1, disturbance=dict(schema=dz.SCHEMA, disturbance_like=False, reasons=[]))) + '\n',
                encoding='utf-8')
            [again] = ReviewStore(output).candidate_context(RUN)['candidates']
            self.assertEqual((again['disturbance']['source'], again['disturbance']['disturbance_like']),
                             ('LIVE_RECORD', False))
            self.assertFalse(result['sound']['available'])
            self.assertEqual(result['biological_activity'], 'UNKNOWN')


class LiveContextTest(unittest.TestCase):
    def test_live_context_is_a_separate_file_and_candidates_stay_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = BridgeManager(Path(folder))
            # 7-9 s: 700 Hz burst (B2 only); 12-14 s: 10 Hz + 700 Hz (1-40 Hz). Since 2026-11-24 both
            # start an observation trigger; the B2-started one is sent as B2_guarded_mad.
            manager.start(dict(mode='mock', duration_s=16))  # CH1 all zero
            manager.thread.join(60)
            state = manager.snapshot()
            run = Path(folder) / ('ad3-' + state['run_id'])
            candidates = [json.loads(line) for line in (run / 'candidates.jsonl').read_text().splitlines()]
            first = candidates[0]
            self.assertTrue(7 <= first['t_start_s'] < 9)
            self.assertEqual((first['detector'], first['triggered_by']), ('B2_guarded_mad', ['B2']))
            self.assertTrue(any(c['detector'] == 'LOWFREQ_FOREST_RATIO' and c['t_start_s'] >= 12 for c in candidates))
            b2 = [json.loads(line) for line in (run / 'b2_candidates.jsonl').read_text().splitlines()]
            self.assertEqual([c['trigger_seq'] for c in b2 if 7 <= c['t_start_s'] < 9][0], first['candidate_seq'])
            manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['observation_trigger']['detector'], 'LOWFREQ_FOREST_RATIO_OR_B2')
            self.assertEqual(manifest['observation_trigger']['b2_triggers'], 1)
            # the trigger's own values per window: live snapshot, saved file, review graph (2026-11-23)
            self.assertTrue(state['trigger_windows'] and state['trigger_threshold'] > 0)
            saved = (run / 'trigger_windows.csv').read_text().splitlines()
            self.assertEqual(saved[0], 't_center_s,forest_p,ratio,active,masked')
            self.assertEqual(len(saved) - 1, len(state['trigger_windows']))
            series = ReviewStore(Path(folder)).overview(run.name)['trigger_series']
            self.assertEqual(series['source'], 'LIVE_RECORD')
            self.assertEqual(len(series['t']), len(saved) - 1)
            hit = [r for t, r in zip(series['t'], series['ratio']) if r is not None and 12 <= t <= 14]
            self.assertTrue(hit and max(hit) >= 3)
            self.assertTrue(all('context' not in c for c in candidates))
            context = [json.loads(line) for line in (run / 'candidate_context.jsonl').read_text().splitlines()]
            self.assertEqual([c['candidate_seq'] for c in context], [c['candidate_seq'] for c in candidates])
            self.assertGreater(context[0]['ch2_x'], 2)
            self.assertIsNone(context[0]['ch1_x'])  # mock CH1 is zero: no ratio, not "normal"
            self.assertEqual(state['candidates'][0]['context']['ch2_x'], context[0]['ch2_x'])
            z = context[0]['disturbance']
            self.assertEqual((z['schema'], z['status']), (dz.SCHEMA, dz.STATUS))
            self.assertIn(z['disturbance_like'], (True, False))
            self.assertIsNone(z['ch1_high_peak_x'])  # mock CH1 is zero
            # The reference numbers equal a replay of the saved samples (same causal filters).
            bands = dz.from_samples(run / 'samples.f64')
            rows = [r.split(',') for r in (run / 'features.csv').read_text().splitlines()[1:]]
            ft = np.array([float(r[0]) for r in rows]); ch2 = np.array([float(r[1]) for r in rows])
            replay = dz.judge(bands, (ft, ch2), candidates[0]['t_start_s'], candidates[0]['t_end_s'])
            self.assertEqual(replay['ch2_low_peak_x'], z['ch2_low_peak_x'])


if __name__ == '__main__':
    unittest.main()
