"""Evaluate a new pair with pre-frozen parameters. No threshold fitting."""
import argparse
import json
from pathlib import Path

import numpy as np

from pipeline import audit, detect, digest, filtered, make_sos, stream_trials, window_features, write_csv


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--quiet-csv',type=Path,required=True)
    ap.add_argument('--pick-csv',type=Path,required=True)
    ap.add_argument('--frozen',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args();frozen=json.loads(args.frozen.read_text(encoding='utf-8'))
    cfg=frozen['preprocessing_config'];args.output.mkdir(parents=True,exist_ok=True)
    records=[];events=[];inputs=[]
    for label,path in [('QUIET',args.quiet_csv),('PICK',args.pick_csv)]:
        inputs.append(dict(label=label,path=str(path.resolve()),sha256=digest(path),size_bytes=path.stat().st_size))
        for source,a in stream_trials(path,cfg['chunksize']):
            qc=audit(a);fs=qc['fs_hz']
            if not np.isclose(fs,100000):raise ValueError('Frozen experiment expects AD3 100 kHz; do not silently change hardware/sample rate')
            for model in frozen['candidates']:
                param=model['parameters'].copy();param['prior']=np.asarray(param.get('prior',[]))
                y=filtered(a[:,1:],make_sos(fs,cfg,cfg['primary_band_hz'],model['notch']),model['filter_mode'])
                data=window_features(y,a[:,0],fs,model['window_ms'],cfg['trim_s'])
                feature=dict(data=data,ms=model['window_ms'])
                found,quality=detect(feature,1,model['detector'],param,cfg)
                row=dict(model=model['name'],source_file=source,label=label,primary='no_signal' not in source.lower(),
                    quality=quality,candidates=len(found),candidate_positive=bool(found),valid_duration_s=len(data[0])*model['window_ms']/1000)
                records.append(row)
                events.extend(dict(row,**e) for e in found)
    write_csv(args.output/'evaluation_per_trial.csv',records)
    write_csv(args.output/'candidate_events.csv',events)
    (args.output/'manifest.json').write_text(json.dumps(dict(inputs=inputs,frozen_sha256=digest(args.frozen),config=frozen,
        scope='Artificial PICK condition only. No fitting; no biological detection or time-labelled accuracy.'),indent=2)+'\n',encoding='utf-8')
    for model in frozen['candidates']:
        for label in ['QUIET','PICK']:
            rows=[r for r in records if r['primary'] and r['model']==model['name'] and r['label']==label]
            print(model['name'],label,'trials',len(rows),'events',sum(r['candidates'] for r in rows),
                  'positive_trials',sum(r['candidate_positive'] for r in rows),'valid_seconds',sum(r['valid_duration_s'] for r in rows))


if __name__=='__main__':main()
