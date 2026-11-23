"""Synthetic unit tests only; not measured detector-performance evidence."""
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from pipeline import audit, combine, filtered, intervals, make_sos, robust_scores, stream_trials, window_features, select_params


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.cfg=json.loads(Path(__file__).with_name('config.json').read_text())

    def test_stream_boundary_and_unique_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'input.csv'
            pd.DataFrame({'source_file':['a']*7+['b']*9,'Time (s)':list(range(7))+list(range(9)),
                          'Channel 1 (V)':[1]*16,'Channel 2 (V)':[2]*16}).to_csv(p,index=False)
            trials=list(stream_trials(p,5))
            self.assertEqual([(s,len(a)) for s,a in trials],[('a',7),('b',9)])

    def test_interleaved_source_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'input.csv'
            p.write_text('source_file,Time (s),Channel 1 (V),Channel 2 (V)\na,0,1,2\nb,0,1,2\na,1,1,2\n')
            with self.assertRaises(ValueError):list(stream_trials(p,20))

    def test_invalid_time_and_missing_samples_fail(self):
        a=np.column_stack((np.arange(20)/100000,np.ones((20,2))))
        self.assertAlmostEqual(audit(a)['fs_hz'],100000)
        a[10,0]+=1e-6
        with self.assertRaises(ValueError):audit(a)
        a[10,0]=a[9,0]
        with self.assertRaises(ValueError):audit(a)
        a[10,0]=np.nan
        with self.assertRaises(ValueError):audit(a)

    def test_filter_rejects_dc_and_preserves_inband_signal(self):
        fs=10000;t=np.arange(fs)/fs;x=np.sin(2*np.pi*700*t)
        for mode in ['causal','zero_phase']:
            y=filtered(np.column_stack((x+1.7,x)),make_sos(fs,self.cfg,[300,1500]),mode)
            np.testing.assert_allclose(y[:,0],y[:,1],atol=1e-10)
            self.assertGreater(np.std(y[2000:-2000,0]),.6)
            notch=filtered(np.column_stack((x,x)),make_sos(fs,self.cfg,[300,1500],True),mode)
            self.assertLess(np.std(notch[2000:-2000]),np.std(y[2000:-2000]))

    def test_window_trim_and_no_trial_stitching(self):
        fs=10000;t=np.arange(fs)/fs
        times,rms,peak,rise=window_features(np.ones((fs,2)),t,fs,20,.1)
        self.assertEqual(len(times),40)
        np.testing.assert_allclose(rms,1)
        np.testing.assert_allclose(rise,0)
        self.assertAlmostEqual(times[0],.11)

    def test_hysteresis_and_refractory_merge(self):
        t=np.arange(12)*.01
        e=intervals(t,np.array([0,2,2,1,0,0,2,2,0,0,0,0]),1,.01*1000,self.cfg)
        self.assertEqual(len(e),1)
        self.assertGreater(e[0]['t_end_s'],.07)

    def test_robust_background_never_learns_sustained_candidate(self):
        prior=np.linspace(.9,1.1,100)
        scores=robust_scores(np.array([1]*10+[10]*50+[1]*10),prior,6,self.cfg)
        self.assertTrue(np.all(scores[10:60]>6))
        self.assertIsNone(robust_scores(np.ones(10),np.ones(3),6,self.cfg))

    def test_coincidence_is_one_to_one_and_soft_preserves_unmatched(self):
        def e(t):return dict(t_start_s=t,t_end_s=t+.01,peak_time_s=t,peak_score=2,threshold=1)
        self.assertEqual(len(combine([e(0),e(.005)],[e(.003)],.01)),1)
        self.assertEqual(len(combine([e(0),e(.1)],[e(.005),e(.3)],.01,True)),3)
        self.assertEqual(len(combine([e(.1)],[e(.12+1e-14)],.02)),1)

    def test_test_pair_does_not_affect_threshold_selection(self):
        def f(sid,label,value):
            a=np.full((20,2),value)
            return dict(session=sid,label=label,variant='v',primary=True,ms=10,valid_duration_s=.2,
                        data=(np.arange(20)*.01,a,a,a))
        train=[f('290','QUIET',1),f('291','PICK',5)]
        a=select_params(['290','291'],train+[f('292','QUIET',1e6)],'v',0,'A',self.cfg)
        b=select_params(['290','291'],train+[f('292','QUIET',1e-6)],'v',0,'A',self.cfg)
        self.assertEqual(a['threshold'],b['threshold'])


if __name__=='__main__':unittest.main()
