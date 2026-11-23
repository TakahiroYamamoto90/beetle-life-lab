"""Reference audit and frozen prospective configurations; no result-driven refit."""
import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import signal

from pipeline import digest, write_csv


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--results',type=Path,required=True)
    args=ap.parse_args();out=args.results
    manifest=json.loads((out/'manifest.json').read_text(encoding='utf-8'));cfg=manifest['config']
    params=json.loads((out/'thresholds.json').read_text());candidates=[]
    # Explicitly prospective selection after looking at both pairs; neither remains a blind test.
    for detector in ['B','A']:
        run=f'B_to_A_causal_notch0_20ms_{detector}_exclude70'
        p=next(r for r in params if r['run_id']==run and r['channel']=='CH2')
        candidates.append(dict(name=f'CH2_{detector}_causal_plain_20ms',detector=detector,filter_mode='causal',notch=False,window_ms=20,
            channel='CH2',parameters=dict(threshold=p['threshold'],prior=p['background_prior'] or [],choice=p['choice']),
            parameter_training_pair='B:292/293',role='prospective_candidate' if detector=='B' else 'absolute_threshold_comparator'))
    frozen=dict(schema='bll.passive_experiment.frozen.v1',preprocessing_config=cfg,candidates=candidates,
        status='EXPLORATORY_NOT_FOR_FIRMWARE_OR_LARVA_DETECTION',
        selection='Variants selected after inspecting both directions. Both A/B are now development data. Evaluate frozen settings on new pair C without refitting.',
        training_manifest_sha256=digest(out/'manifest.json'))
    (out/'frozen_unseen_C.json').write_text(json.dumps(frozen,indent=2)+'\n',encoding='utf-8')
    # Independently audit ambiguity in the old band-RMS reference using explicit orders/edges.
    rows=[]
    for info in manifest['inputs']:
        parent=Path(info['path']).parent;totals={}
        for source in info['sources']:
            if not source['primary']:continue
            a=pd.read_csv(parent/source['source_file'],comment='#').to_numpy(dtype=float)
            x=a[:,1:]-a[:,1:].mean(axis=0)
            for order in [2,3,4,5]:
                y=signal.sosfiltfilt(signal.butter(order,[300,1500],fs=100000,btype='bandpass',output='sos'),x,axis=0)
                for trim in [0,.1]:
                    z=y if trim==0 else y[10000:-10000]
                    totals.setdefault((order,trim),[]).append(np.sqrt(np.mean(z*z,axis=0))*1000)
        for (order,trim),v in totals.items():
            avg=np.mean(v,axis=0)
            rows.append(dict(session=info['session'],order=order,trim_s=trim,CH1_mV=avg[0],CH2_mV=avg[1]))
    write_csv(out/'reference_filter_audit.csv',rows)
    print('Frozen candidates:',[(c['name'],c['parameters']['threshold']) for c in candidates])


if __name__=='__main__':main()
