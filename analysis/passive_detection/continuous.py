"""Frozen continuous evaluation against planned, not video-verified, intervals."""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import signal

from pipeline import digest, intervals, make_sos, robust_scores, write_csv


def extract(path,cfg,chunksize):
    """Continuous causal SOS; carry filter state and partial RMS windows across reads."""
    fs=100000;n=round(fs*.02);skip=round(cfg['trim_s']*fs)
    sos=make_sos(fs,cfg,cfg['primary_band_hz'],False)
    zi=np.zeros((len(sos),2,2));pending=np.empty((0,2));blocks=[]
    count=0;first=None;last=None;t0=None;dt_min=float('inf');dt_max=0
    minimum=np.full(2,np.inf);maximum=np.full(2,-np.inf)
    for frame in pd.read_csv(path,comment='#',chunksize=chunksize):
        if list(frame.columns)!=['Time (s)','Channel 1 (V)','Channel 2 (V)']:raise ValueError('Unexpected columns')
        a=frame.to_numpy(dtype=float)
        if not np.isfinite(a).all():raise ValueError('Nonfinite samples')
        t=a[:,0];x=a[:,1:]
        if t0 is None:t0=float(t[0]);first=x[0].copy()
        dt=np.diff(t if last is None else np.r_[last,t]);last=float(t[-1])
        if not np.allclose(dt,1/fs,rtol=0,atol=1e-9):raise ValueError('Gap or incorrect sample clock; stop rather than bridge')
        dt_min=min(dt_min,float(dt.min()));dt_max=max(dt_max,float(dt.max()))
        minimum=np.minimum(minimum,x.min(axis=0));maximum=np.maximum(maximum,x.max(axis=0))
        y,zi=signal.sosfilt(sos,x-first,axis=0,zi=zi)
        ignore=max(0,min(len(y),skip-count));count+=len(y);y=y[ignore:]
        pending=np.concatenate((pending,y))
        complete=len(pending)//n
        if complete:
            w=pending[:complete*n].reshape(complete,n,2)
            blocks.append(np.sqrt(np.mean(w*w,axis=1)))
            pending=pending[complete*n:]
    rms=np.concatenate(blocks);times=t0+cfg['trim_s']+(np.arange(len(rms))+.5)*.02
    # Discard final margin of the actual acquisition, not artificial 90-second cutoff.
    valid=times+.01<=last+1/fs-cfg['trim_s']+1e-8
    return times[valid]-t0,rms[valid],dict(samples=count,t0_s=t0,t_last_s=last,
        duration_s=last-t0+1/fs,dt_min_s=dt_min,dt_max_s=dt_max,
        minimum_V=minimum.tolist(),maximum_V=maximum.tolist(),clipping='UNVERIFIED hardware rails')


def overlap(event,start,end):
    return max(0,min(event['t_end_s'],end)-max(event['t_start_s'],start))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input',type=Path,required=True)
    ap.add_argument('--frozen',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--chunksize',type=int,default=50000)
    args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    frozen=json.loads(args.frozen.read_text());cfg=frozen['preprocessing_config']
    times,rms,audit=extract(args.input,cfg,args.chunksize)
    plan=[dict(label=label,start_s=start,end_s=end) for label,start,end in
          [('QUIET',0,30),('PICK',30,35),('QUIET',35,60),('PICK',60,65),('QUIET',65,90)]]
    if audit['duration_s']>90:plan.append(dict(label='UNANNOTATED',start_s=90,end_s=audit['duration_s']))
    if audit['duration_s']<90-1e-6:raise ValueError('Capture shorter than annotated plan')
    header=[]
    with args.input.open(encoding='utf-8-sig') as f:
        for line in f:
            if line.startswith('#'):header.append(line.strip())
            elif line.strip():break
    count_line=next(h for h in header if h.startswith('#Samples:'))
    if int(count_line.split(':',1)[1])!=audit['samples']:raise ValueError('Header sample count mismatch')
    rows=[];event_rows=[];features=[];scores_by_model={};events_by_model={}
    for model in frozen['candidates']:
        assert model['filter_mode']=='causal' and model['window_ms']==20 and not model['notch']
        param=model['parameters'];threshold=param['threshold']
        scores=rms[:,1] if model['detector']=='A' else robust_scores(rms[:,1],np.array(param['prior']),threshold,cfg)
        if scores is None:raise ValueError('NOT_CALIBRATED')
        scores_by_model[model['detector']]=scores
        found=intervals(times,scores,threshold,20,cfg);events_by_model[model['detector']]=found
        event_rows.extend(dict(model=model['name'],**e) for e in found)
        for i,segment in enumerate(plan):
            start=max(segment['start_s'],cfg['trim_s']);end=min(segment['end_s'],audit['duration_s']-cfg['trim_s'])
            mask=(times-.01>=start-1e-8)&(times+.01<=end+1e-8)
            seconds=float(mask.sum()*.02)
            spans=[overlap(e,start,end) for e in found];occupancy=sum(spans)
            row=dict(model=model['name'],segment=i,label=segment['label'],start_s=start,end_s=end,
                valid_duration_s=seconds,onsets=int(sum(start<=e['t_start_s']<end for e in found)),
                overlapping_candidates=int(sum(v>1e-9 for v in spans)),candidate_duration_s=occupancy,
                candidate_occupancy_fraction=occupancy/seconds if seconds else None,
                window_rms_CH2_median_mV=float(np.median(rms[mask,1])*1000) if mask.any() else None,
                mean_band_rms_CH1_mV=float(np.sqrt(np.mean(rms[mask,0]**2))*1000) if mask.any() else None,
                mean_band_rms_CH2_mV=float(np.sqrt(np.mean(rms[mask,1]**2))*1000) if mask.any() else None)
            rows.append(row)
    for i,t in enumerate(times):
        label=next((s['label'] for s in plan if t-.01>=max(s['start_s'],cfg['trim_s'])-1e-8 and t+.01<=s['end_s']+1e-8),'TRANSITION')
        features.append(dict(t_center_s=t,label=label,CH1_band_rms_mV=rms[i,0]*1000,CH2_band_rms_mV=rms[i,1]*1000,
            A_score_V=scores_by_model['A'][i],B_score=scores_by_model['B'][i]))
    write_csv(args.output/'features_20ms.csv',features)
    write_csv(args.output/'candidate_events.csv',event_rows)
    write_csv(args.output/'evaluation_segments.csv',rows)
    totals=[]
    for model in frozen['candidates']:
        for label in ['QUIET','PICK','UNANNOTATED']:
            sub=[r for r in rows if r['model']==model['name'] and r['label']==label]
            if not sub:continue
            sec=sum(r['valid_duration_s'] for r in sub);onsets=sum(r['onsets'] for r in sub);dur=sum(r['candidate_duration_s'] for r in sub)
            totals.append(dict(model=model['name'],label=label,valid_duration_s=sec,onsets=onsets,
                quiet_onsets_per_min=onsets/sec*60 if label=='QUIET' else None,
                candidate_duration_s=dur,candidate_occupancy_fraction=dur/sec,
                overlapping_segments=sum(r['overlapping_candidates']>0 for r in sub),segments=len(sub)))
    write_csv(args.output/'evaluation.csv',totals)
    manifest=dict(input=str(args.input.resolve()),input_sha256=digest(args.input),input_size=args.input.stat().st_size,
        frozen_sha256=digest(args.frozen),frozen_config=frozen,analysis_code_sha256=digest(Path(__file__)),header=header,
        audit=audit,plan=plan,annotation_quality='USER_REPORTED_PLANNED_TIMES_NOT_VIDEO_VERIFIED',
        time_alignment='First CSV sample is 0. Scheduled boundaries assumed relative to this; actual operator timing uncertain.',
        processing='Continuous causal filter and past-only robust background. No resets at chunks, seconds, or scheduled boundaries. No refit. Tail beyond 90 seconds UNANNOTATED. Candidates are not biological activity.',
        chunksize=args.chunksize)
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    fig,axes=plt.subplots(3,1,figsize=(13,9),constrained_layout=True,sharex=True)
    axes[0].plot(times,rms[:,1]*1000,lw=.6,label='CH2 20 ms band RMS')
    a_model=next(m for m in frozen['candidates'] if m['detector']=='A')
    axes[0].axhline(a_model['parameters']['threshold']*1000,color='red',ls='--',label='Frozen A threshold');axes[0].set(ylabel='mV RMS');axes[0].legend()
    axes[1].plot(times,scores_by_model['B'],lw=.6);axes[1].axhline(6,color='red',ls='--');axes[1].set(ylabel='B robust score')
    for detector,level in [('A',0),('B',1)]:
        for e in events_by_model[detector]:axes[2].plot([e['t_start_s'],e['t_end_s']],[level,level],lw=5,color='blue' if detector=='A' else 'green')
    axes[2].set(yticks=[0,1],yticklabels=['A','B'],ylim=(-.5,1.5),xlabel='Elapsed seconds from first sample')
    for ax in axes:
        for s in plan:
            if s['label']=='PICK':ax.axvspan(s['start_s'],s['end_s'],color='orange',alpha=.2)
            if s['label']=='UNANNOTATED':ax.axvspan(s['start_s'],s['end_s'],color='gray',alpha=.2)
        ax.grid(alpha=.2)
    fig.suptitle('trial296 continuous frozen evaluation; orange = scheduled PICK, gray = unannotated')
    fig.savefig(args.output/'timeline.png',dpi=150);plt.close(fig)
    print(json.dumps(audit,indent=2));print(pd.DataFrame(totals).to_string(index=False));print(pd.DataFrame(rows).to_string(index=False))


if __name__=='__main__':main()
