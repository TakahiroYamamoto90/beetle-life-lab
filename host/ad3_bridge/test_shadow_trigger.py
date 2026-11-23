import unittest

import shadow_trigger as st


class ShadowTriggerTest(unittest.TestCase):
    def test_triggers_are_span_starts_known_after_one_window(self):
        [x] = st.triggers([dict(t0_s=10.0, t1_s=11.0, peak_x=4.2)], 'peak_x')
        self.assertEqual((x['t_s'], x['peak'], x['windows']), (10.25, 4.2, 4))

    def test_compare_counts_b2_matches_arm_and_unavailable_sources(self):
        b2 = [dict(t_start_s=5.0, t_end_s=5.5), dict(t_start_s=40.0, t_end_s=40.2)]
        spans = [dict(t0_s=5.8, t1_s=6.3, peak_x=3.5),     # within 1 s of the first B2 candidate
                 dict(t0_s=20.0, t1_s=20.25, peak_x=3.1),  # away from B2, 1 s after an arm span
                 dict(t0_s=50.0, t1_s=51.0, peak_x=5.0)]   # away from B2 and the arm
        out = st.compare(120.0, b2, dict(LOWFREQ_RATIO_3=(spans, 'peak_x'), FOREST_XIAO=None), [(17.0, 19.25)])
        s = out['sources']['LOWFREQ_RATIO_3']
        self.assertEqual((s['count'], s['per_min'], s['with_b2'], s['without_b2'], s['after_arm']), (3, 1.5, 1, 2, 1))
        self.assertEqual(s['b2_not_covered'], 1)  # the 40 s B2 candidate has no shadow trigger near it
        self.assertEqual(out['sources']['FOREST_XIAO'], dict(status='UNAVAILABLE'))
        self.assertEqual((out['b2']['count'], out['b2']['per_min']), (2, 1.0))
        self.assertEqual(out['biological_activity'], 'UNKNOWN')

    def test_nothing_is_a_trigger_without_spans(self):
        out = st.compare(30.0, [], dict(FOREST_PC=([], 'peak_p')))
        self.assertEqual((out['sources']['FOREST_PC']['count'], out['sources']['FOREST_PC']['b2_not_covered']), (0, 0))


if __name__ == '__main__':
    unittest.main()
