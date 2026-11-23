"""Audit and visualize a frozen evaluation; no fitting, tuning, or relabelling."""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import signal

from pipeline import audit, digest, filtered, make_sos, robust_scores, stream_trials, window_features, write_csv


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--results',type=Path,required=True)
    args=ap.parse_args();out=args.results
    manifest=json.loads((out/'manifest.json').read_text(encoding='utf-8'))
    frozen=manifest['config'];cfg=frozen['preprocessing_config']
    ev=pd.read_csv(out/'evaluation_per_trial.csv');events=pd.read_csv(out/'candidate_events.csv')
    rows=[];audits=[];psds={};examples=[]
    for info in manifest['inputs']:
        path=Path(info['path']);assert digest(path)==info['sha256']
        seen=[];background=[];numbers=[];group_psd=[]
        for source,a in stream_trials(path,cfg['chunksize']):
            qc=audit(a);seen.append(source);primary='no_signal' not in source.lower()
            number=int(Path(source).stem.rsplit('_',1)[1]) if primary else None
            if primary:numbers.append(number)
            original=path.parent/Path(source).name
            header=[]
            with original.open(encoding='utf-8-sig') as f:
                for line in f:
                    if line.startswith('#'):header.append(line.strip())
                    elif line.strip():break
            raw=pd.read_csv(original,comment='#').to_numpy(dtype=float)
            np.testing.assert_allclose(raw,a,atol=1e-12,rtol=0)
            audits.append(dict(label=info['label'],source_file=source,primary=primary,header=header,**qc))
            y=filtered(a[:,1:],make_sos(qc['fs_hz'],cfg,cfg['primary_band_hz'],False),'causal')
            data=window_features(y,a[:,0],qc['fs_hz'],20,cfg['trim_s'])
            times,rms,peak,rise=data
            for ch in range(2):
                row=dict(source_file=source,label=info['label'],primary=primary,trial_number=number,channel=f'CH{ch+1}',
                    ac_rms_mV=float(np.std(a[:,ch+1])*1000),dc_V=float(a[:,ch+1].mean()),
                    peak_abs_ac_mV=float(max(abs(a[:,ch+1]-a[:,ch+1].mean()))*1000),
                    causal_band_rms_mV=float(np.sqrt(np.mean(rms[:,ch]**2))*1000),
                    window_rms_median_mV=float(np.median(rms[:,ch])*1000),window_rms_max_mV=float(rms[:,ch].max()*1000))
                rows.append(row)
            if primary:
                fs=qc['fs_hz'];frequencies,p=signal.welch(a[:,1:],fs=fs,nperseg=10000,noverlap=5000,axis=0)
                group_psd.append(p)
                is_positive=bool(ev[(ev.source_file==source)&ev.model.str.contains('_B_')].candidate_positive.iloc[0])
                # Fixed representative rule: first QUIET and first positive/negative PICK.
                key=(info['label'],is_positive)
                if key not in [item[0] for item in examples]:examples.append((key,source,times,rms[:,1]))
        assert sorted(numbers)==list(range(1,13)) and len(seen)==13
        psds[info['label']]=(frequencies,np.mean(group_psd,axis=0))
    write_csv(out/'features_per_trial.csv',rows)
    summary=[]
    for (model,label),g in ev[ev.primary].groupby(['model','label']):
        seconds=float(g.valid_duration_s.sum());count=int(g.candidates.sum())
        summary.append(dict(model=model,label=label,trials=len(g),valid_duration_s=seconds,candidate_count=count,
            positive_trials=int(g.candidate_positive.sum()),positive_trial_fraction=float(g.candidate_positive.mean()),
            quiet_false_alarms_per_s=count/seconds if label=='QUIET' else None))
    write_csv(out/'evaluation.csv',summary)
    (out/'audit.json').write_text(json.dumps(dict(trials=audits,analysis_code_sha256=digest(Path(__file__)),
        frozen_config_sha256=manifest['frozen_sha256'],note='No parameters fit. No signal source is audit-only. Hardware clipping limits unverified.'),indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    frame=pd.DataFrame(rows);main=frame[frame.primary]
    fig,axes=plt.subplots(1,2,figsize=(11,4),constrained_layout=True)
    for ch,ax in zip(['CH1','CH2'],axes):
        for i,label in enumerate(['QUIET','PICK']):
            g=main[(main.channel==ch)&(main.label==label)]
            ax.scatter(g.trial_number,g.ac_rms_mV,label=label)
        ax.set(title=f'Pair C {ch}: AC RMS',xlabel='Trial number',ylabel='mV RMS');ax.legend();ax.grid(alpha=.2)
    fig.savefig(out/'01_rms.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(12,4),constrained_layout=True)
    for ch,ax in enumerate(axes):
        for label,(f,p) in psds.items():
            mask=(f>=40)&(f<=5000);ax.semilogx(f[mask],10*np.log10(np.maximum(p[mask,ch],1e-30)),label=label)
        ax.set(title=f'Pair C CH{ch+1}: raw voltage PSD',xlabel='Hz',ylabel='dB V²/Hz');ax.legend();ax.grid(alpha=.2)
    fig.savefig(out/'02_psd.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(len(examples),1,figsize=(11,3*len(examples)),squeeze=False,constrained_layout=True)
    a_model=next(m for m in frozen['candidates'] if m['detector']=='A')
    for (_,source,times,rms),ax in zip(examples,axes[:,0]):
        ax.plot(times,rms*1000,label='CH2 causal 20 ms RMS')
        ax.axhline(a_model['parameters']['threshold']*1000,ls='--',color='red',label='Frozen A threshold')
        for _,e in events[(events.source_file==source)&events.model.str.contains('_B_')].iterrows():
            ax.axvspan(e.t_start_s,e.t_end_s,color='orange',alpha=.25)
        ax.set(title=source.split('dry_',1)[-1]+' (orange = B candidate)',xlabel='s',ylabel='mV RMS');ax.legend();ax.grid(alpha=.2)
    fig.savefig(out/'03_candidates.png',dpi=150);plt.close(fig)
    print(main.groupby(['label','channel'])[['ac_rms_mV','causal_band_rms_mV']].mean().to_string())
    print(pd.DataFrame(summary).to_string(index=False))
    print('B-negative PICK:',ev[(ev.label=='PICK')&ev.primary&ev.model.str.contains('_B_')&(~ev.candidate_positive)].source_file.tolist())


if __name__=='__main__':main()
