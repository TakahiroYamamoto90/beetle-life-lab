"""Synthetic behavioral tests; separate from the measured development evaluation."""
import json
from pathlib import Path
import unittest

import numpy as np

from improved import ContinuousCandidate


class ImprovedTests(unittest.TestCase):
    def setUp(self):self.cfg=json.loads(Path(__file__).with_name('improved_config.json').read_text())

    def run_signal(self,x,model):
        d=ContinuousCandidate(self.cfg,self.cfg['models'][model])
        for t,v in zip(np.arange(len(x))*.02,x):d.update(float(t),float(v))
        d.finish(len(x)*.02);return d

    def test_constant_baseline_never_becomes_candidate(self):
        for name in self.cfg['models']:
            d=self.run_signal(np.full(3000,.0012),name)
            self.assertEqual(d.events,[])

    def test_gradual_rises_freeze_background_and_rearm_after_changed_floor(self):
        t=np.arange(3000)*.02;x=np.full(len(t),.0012)
        for start in [10,30]:
            m=(t>=start)&(t<start+2);x[m]+=.0004*(t[m]-start)/2
            x[(t>=start+2)&(t<start+5)]+=.0004
            x[(t>=start+5)]+=.00004
        for name in self.cfg['models']:
            d=self.run_signal(x,name)
            self.assertEqual(len(d.events),2,name)
            self.assertTrue(all(e['end_reason']=='FALL_FROM_PEAK' or e['end_reason']=='BASELINE_RETURN' for e in d.events))
            self.assertTrue(all(e['quality']!='INCOMPLETE' for e in d.events))

    def test_persistent_candidate_faults_without_repeated_false_rearms(self):
        x=np.full(2000,.001);x[500:]=.002
        for name in self.cfg['models']:
            d=self.run_signal(x,name)
            self.assertEqual(d.state,'FAULT')
            self.assertEqual(len(d.events),1)
            self.assertEqual(d.events[0]['quality'],'INCOMPLETE')

    def test_no_future_samples_affect_prefix(self):
        x=np.full(2000,.001);x[1000:]=.002
        for name in self.cfg['models']:
            d=self.run_signal(x,name);short=self.run_signal(x[:900],name)
            self.assertEqual(d.trace[:900],short.trace)

    def test_gap_is_a_fault_not_quiet(self):
        d=ContinuousCandidate(self.cfg,self.cfg['models']['A2_relative_rms'])
        d.update(0,.001);d.update(.2,.001)
        self.assertEqual(d.state,'FAULT')

    def test_zero_input_is_not_successful_calibration(self):
        for name in self.cfg['models']:
            d=self.run_signal(np.zeros(500),name)
            self.assertEqual(d.state,'FAULT')
            self.assertEqual(d.fault_reason,'CALIBRATION_NO_SIGNAL')


if __name__=='__main__':unittest.main()
