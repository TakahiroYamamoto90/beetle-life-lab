"""Synthetic chunk-invariance test; not measured performance evidence."""
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
import pandas as pd

from continuous import extract, overlap
from pipeline import filtered, make_sos, window_features


class ContinuousTests(unittest.TestCase):
    def test_chunked_filter_matches_batch_and_preserves_windows(self):
        cfg=json.loads(Path(__file__).with_name('config.json').read_text())
        fs=100000;t=np.arange(100000)/fs
        x=np.column_stack((1.65+.001*np.sin(2*np.pi*700*t),.005*np.sin(2*np.pi*500*t)))
        x[30000:35000,1]+=.01*np.sin(2*np.pi*900*t[30000:35000])
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'synthetic.csv'
            pd.DataFrame(np.column_stack((t,x)),columns=['Time (s)','Channel 1 (V)','Channel 2 (V)']).to_csv(p,index=False)
            times,rms,qc=extract(p,cfg,1379)
            y=filtered(x,make_sos(fs,cfg,cfg['primary_band_hz'],False),'causal')
            bt,br,_,_=window_features(y,t,fs,20,.1)
            np.testing.assert_allclose(times,bt,atol=1e-12)
            np.testing.assert_allclose(rms,br,atol=1e-11)
            self.assertEqual(qc['samples'],len(t))

    def test_long_candidate_counts_occupancy_in_both_intervals(self):
        e=dict(t_start_s=25,t_end_s=40)
        self.assertEqual(overlap(e,0,30),5)
        self.assertEqual(overlap(e,30,35),5)
        self.assertEqual(overlap(e,35,60),5)


if __name__=='__main__':unittest.main()
