"""Track B: artificial PICK vs QUIET; never emits biological activity evidence.

Inputs are read-only, one streamed trial at a time. No waveform cache is written.
"""
import argparse
import csv
import hashlib
import json
import platform
from collections import deque
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import scipy
from scipy import signal


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024*1024), b''): h.update(block)
    return h.hexdigest()


def stream_trials(path, chunksize):
    """Support trial boundaries inside chunks; reject interleaved trial IDs."""
    current, parts, seen = None, [], set()
    with pd.read_csv(path, chunksize=chunksize) as reader:
        for chunk in reader:
            if list(chunk.columns) != ['source_file','Time (s)','Channel 1 (V)','Channel 2 (V)']:
                raise ValueError(f'Unexpected columns: {list(chunk.columns)}')
            if chunk.source_file.isna().any(): raise ValueError('Missing source_file')
            source_runs = chunk.source_file.ne(chunk.source_file.shift()).groupby(chunk.source_file).sum()
            for source, group in chunk.groupby('source_file', sort=False):
                if source_runs.loc[source] > 1: raise ValueError('Interleaved source_file')
                if source != current:
                    if parts: yield current, np.concatenate(parts)
                    if source in seen: raise ValueError(f'Repeated noncontiguous source: {source}')
                    seen.add(source); current, parts = source, []
                parts.append(group.iloc[:,1:].to_numpy(dtype=float))
    if parts: yield current, np.concatenate(parts)


def audit(a):
    if a.ndim != 2 or a.shape[1] != 3 or len(a)<10: raise ValueError('Invalid trial shape')
    if not np.isfinite(a).all(): raise ValueError('Non-finite samples; comparison stopped')
    dt = np.diff(a[:,0]); median = np.median(dt)
    if median <= 0 or np.any(dt<=0) or not np.allclose(dt,median,atol=1e-10,rtol=1e-5):
        raise ValueError('Irregular time axis; comparison stopped')
    fs = float(1/median)
    return dict(samples=len(a), fs_hz=fs, t_start_s=float(a[0,0]),
        t_end_s=float(a[-1,0]), duration_s=float(a[-1,0]-a[0,0]+median),
        dt_min_s=float(dt.min()), dt_max_s=float(dt.max()), nan_inf=0,
        channels=[dict(dc_V=float(x.mean()),minimum_V=float(x.min()),maximum_V=float(x.max()),
            repeated_min_count=int(sum(x==x.min())),repeated_max_count=int(sum(x==x.max())),
            clipping='UNVERIFIED: hardware rails unknown; extrema counts are diagnostics, not proof') for x in a[:,1:].T])


def make_sos(fs, cfg, band, notch=False):
    sos = signal.butter(cfg['butterworth_order'],band,fs=fs,btype='bandpass',output='sos')
    if notch:
        for f in range(50, min(cfg['notch_max_hz'],int(fs/2)-1)+1,50):
            b,a = signal.iirnotch(f, f/(2*cfg['notch_half_width_hz']),fs=fs)
            sos = np.vstack((sos,signal.tf2sos(b,a)))
    return sos


def filtered(x, sos, mode):
    if mode == 'zero_phase':
        return signal.sosfiltfilt(sos,signal.detrend(x,axis=0,type='constant'),axis=0)
    # Causal mode uses initial sample removal, not a future-dependent trial mean.
    centered = x-x[0]
    return signal.sosfilt(sos,centered,axis=0)


def window_features(y,t,fs,ms,trim):
    start = int(round(trim*fs)); stop=len(y)-start
    n = int(round(ms*fs/1000)); count=max(0,(stop-start)//n)
    w=y[start:start+count*n].reshape(count,n,2)
    times=t[start]+(np.arange(count)+.5)*n/fs
    rms=np.sqrt(np.mean(w*w,axis=1)); peak=np.max(abs(w),axis=1)
    rise=np.maximum(0,np.diff(rms,axis=0,prepend=rms[:1]))
    return times,rms,peak,rise


def intervals(times,scores,threshold,ms,cfg,consecutive=None):
    """Threshold/hysteresis and merge/refractory; no event crosses trial boundary."""
    consecutive=consecutive or cfg['minimum_consecutive']
    active=None; run=0; events=[]; peak=-np.inf; peak_t=0
    for i,score in enumerate(scores):
        if active is None:
            run=run+1 if score>threshold else 0
            if run>=consecutive:
                active=i-run+1
                ix=active+int(np.argmax(scores[active:i+1]));peak=float(scores[ix]);peak_t=float(times[ix])
        else:
            if score>peak: peak=float(score);peak_t=float(times[i])
            if score<=threshold*cfg['hysteresis_fraction']:
                events.append(dict(t_start_s=float(times[active]-ms/2000),t_end_s=float(times[i]+ms/2000),
                    peak_score=peak,peak_time_s=peak_t,threshold=float(threshold)))
                active=None;run=0
    if active is not None:
        events.append(dict(t_start_s=float(times[active]-ms/2000),t_end_s=float(times[-1]+ms/2000),
            peak_score=peak,peak_time_s=peak_t,threshold=float(threshold)))
    merged=[]
    for e in events:
        if merged and e['t_start_s']-merged[-1]['t_end_s']<cfg['refractory_s']:
            old=merged[-1];old['t_end_s']=e['t_end_s']
            if e['peak_score']>old['peak_score']:old.update(peak_score=e['peak_score'],peak_time_s=e['peak_time_s'])
        else: merged.append(e.copy())
    return merged


def robust_scores(rms, prior, k, cfg):
    """Prior from training QUIET only. Update using past, non-candidate windows."""
    if len(prior)<cfg['robust_min_windows']: return None
    history=deque(np.quantile(prior,np.linspace(0,1,cfg['robust_history_windows'])),maxlen=cfg['robust_history_windows'])
    scores=[]
    for value in rms:
        bg=np.asarray(history);med=float(np.median(bg));scale=max(1.4826*float(np.median(abs(bg-med))),cfg['robust_scale_floor_fraction']*med,1e-12)
        score=(value-med)/scale;scores.append(score)
        # Freeze also in hysteresis/precursor region, not only above onset k.
        if score<=k*cfg['hysteresis_fraction']: history.append(float(value))
    return np.array(scores)


def detect(feature,ch,detector,param,cfg):
    times,rms,peak,rise=feature['data'];ms=feature['ms']
    if detector=='B':
        scores=robust_scores(rms[:,ch],param['prior'],param['threshold'],cfg)
        if scores is None:return [],'NOT_CALIBRATED'
    else:scores = rms[:,ch] if detector=='A' else rise[:,ch]
    return intervals(times,scores,param['threshold'],ms,cfg,1 if detector=='C' else None),'VALID'


def combine(left,right,tolerance,soft=False):
    used=set();out=[]
    for a in left:
        candidates=[(abs(a['peak_time_s']-b['peak_time_s']),i,b) for i,b in enumerate(right)
                    if i not in used and abs(a['peak_time_s']-b['peak_time_s'])<=tolerance+1e-12]
        if candidates:
            _,i,b=min(candidates);used.add(i)
            out.append(dict(t_start_s=min(a['t_start_s'],b['t_start_s']),t_end_s=max(a['t_end_s'],b['t_end_s']),
                peak_time_s=(a['peak_time_s']+b['peak_time_s'])/2,peak_score=2,threshold=1,
                agreement='BOTH'))
        elif soft:out.append(dict(a,peak_score=1,threshold=1,agreement='CH1_ONLY'))
    if soft:
        out.extend(dict(b,peak_score=1,threshold=1,agreement='CH2_ONLY') for i,b in enumerate(right) if i not in used)
    return sorted(out,key=lambda e:e['t_start_s'])


def select_params(train,features,variant,ch,detector,cfg):
    quiet=[f for f in features if f['session'] in train and f['label']=='QUIET' and f['variant']==variant and f['primary']]
    pick=[f for f in features if f['session'] in train and f['label']=='PICK' and f['variant']==variant and f['primary']]
    index=1 if detector in ('A','B') else 3
    values=np.concatenate([f['data'][index][:,ch] for f in quiet])
    choices=cfg['robust_k'] if detector=='B' else cfg['absolute_quantiles']
    trials=[]
    for choice in choices:
        th=float(choice if detector=='B' else max(np.quantile(values,choice),1e-12))
        param=dict(threshold=th,choice=choice,prior=values)
        qcount=sum(len(detect(f,ch,detector,param,cfg)[0]) for f in quiet)
        seconds=sum(f['valid_duration_s'] for f in quiet)
        positive=sum(bool(detect(f,ch,detector,param,cfg)[0]) for f in pick)
        rate=qcount/seconds
        trials.append((rate>cfg['training_quiet_budget_events_per_s'], -positive if rate<=cfg['training_quiet_budget_events_per_s'] else rate,rate,-choice,param))
    # Only training pair contributes to this rank; no test statistics enter selection.
    return min(trials,key=lambda r:r[:4])[-1]


def write_csv(path,rows):
    if not rows:path.write_text('',encoding='utf-8');return
    keys=list(dict.fromkeys(k for r in rows for k in r))
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=keys);w.writeheader();w.writerows(rows)


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--input-dir',type=Path,required=True)
    ap.add_argument('--output',type=Path,required=True)
    ap.add_argument('--config',type=Path,default=Path(__file__).with_name('config.json'))
    args=ap.parse_args();cfg=json.loads(args.config.read_text(encoding='utf-8'))
    args.output.mkdir(parents=True,exist_ok=True)
    manifest=dict(version=cfg['version'],config=cfg,config_sha256=digest(args.config),
        code_sha256=digest(Path(__file__)),versions=dict(python=platform.python_version(),numpy=np.__version__,scipy=scipy.__version__,pandas=pd.__version__,matplotlib=matplotlib.__version__),
        role='Artificial PICK condition candidates only; no biological activity claims',inputs=[],trials=[])
    features=[];feature_rows=[];representatives={};psds={};baselines={}
    for sid,label in cfg['sessions'].items():
        candidates=list(args.input_dir.rglob(f'*dry_{sid}_AXCL_6cm.csv'))
        if len(candidates)!=1:raise ValueError(f'Expected one merged CSV for {sid}: {candidates}')
        path=candidates[0];print(f'AUDIT {sid}: {path.stat().st_size} bytes',flush=True)
        info=dict(session=sid,label=label,path=str(path.resolve()),sha256=digest(path),size_bytes=path.stat().st_size,
            columns=list(pd.read_csv(path,nrows=0).columns),sources=[])
        manifest['inputs'].append(info)
        spectra_before=[];spectra_after=[];session_refs=[]
        for source,a in stream_trials(path,cfg['chunksize']):
            qc=audit(a);fs=qc['fs_hz'];primary='no_signal' not in source.lower()
            number=int(Path(source).stem.rsplit('_',1)[1]) if primary else None
            trial_id=f'{sid}:{number if primary else "no_signal"}'
            original=path.parent/Path(source).name
            header=[]
            if original.exists():
                with original.open(encoding='utf-8-sig') as f:
                    for line in f:
                        if line.startswith('#'):header.append(line.strip())
                        elif line.strip():break
                raw=pd.read_csv(original,comment='#').to_numpy(dtype=float)
                if raw.shape!=a.shape or not np.allclose(raw,a,atol=1e-12,rtol=0):raise ValueError(f'Merged/original mismatch: {source}')
            info['sources'].append(dict(source_file=source,trial_id=trial_id,primary=primary))
            manifest['trials'].append(dict(session=sid,trial_id=trial_id,source_file=source,primary=primary,original_header=header,**qc))
            base=dict(session=sid,label=label,trial_id=trial_id,trial_number=number,primary=primary)
            filtered_series={}
            for mode in cfg['filter_modes']:
                for notch in [False,True]:
                    y=filtered(a[:,1:],make_sos(fs,cfg,cfg['primary_band_hz'],notch),mode)
                    filtered_series[(mode,notch)]=y
                    for ms in cfg['window_ms']:
                        data=window_features(y,a[:,0],fs,ms,cfg['trim_s'])
                        variant=f'{mode}_notch{int(notch)}_{ms}ms'
                        features.append(dict(base,variant=variant,ms=ms,data=data,valid_duration_s=len(data[0])*ms/1000))
            for ch in range(2):
                x=a[:,ch+1];ac=x-x.mean();row=dict(base,channel=f'CH{ch+1}',ac_rms_mV=float(np.std(x)*1000),dc_V=float(x.mean()),peak_ac_mV=float(max(abs(ac))*1000),quality='VALID_CLIPPING_UNVERIFIED')
                for band in cfg['bands_hz']:
                    for notch in [False,True]:
                        y=filtered(a[:,ch+1:ch+2],make_sos(fs,cfg,band,notch),'zero_phase')[:,0]
                        valid=y[round(cfg['trim_s']*fs):-round(cfg['trim_s']*fs)]
                        key=f'band_{band[0]}_{band[1]}_notch{int(notch)}_mV'
                        row[key]=float(np.sqrt(np.mean(valid**2))*1000)
                        if band==cfg['primary_band_hz'] and not notch:
                            # Reference audit: whole trial vs common trimmed interval.
                            row['reference_band_whole_trial_mV']=float(np.sqrt(np.mean(y*y))*1000)
                for mode in cfg['filter_modes']:
                    for notch in [False,True]:
                        for ms in cfg['window_ms']:
                            f=next(f for f in reversed(features) if f['trial_id']==trial_id and f['variant']==f'{mode}_notch{int(notch)}_{ms}ms')
                            times,rms,peak,rise=f['data'];r=rms[:,ch]
                            row.update({f'{f["variant"]}_{key}':value for key,value in dict(rms_median_mV=float(np.median(r)*1000),rms_max_mV=float(max(r)*1000),max_t_s=float(times[np.argmax(r)]),peak_mV=float(peak[:,ch].max()*1000),rise_max_mV=float(rise[:,ch].max()*1000)).items()})
                feature_rows.append(row)
            if primary:
                psd_f,p=signal.welch(a[:,1:],fs=fs,nperseg=10000,noverlap=5000,axis=0)
                _,pn=signal.welch(filtered_series[('zero_phase',True)],fs=fs,nperseg=10000,noverlap=5000,axis=0)
                _,pb=signal.welch(filtered_series[('zero_phase',False)],fs=fs,nperseg=10000,noverlap=5000,axis=0)
                spectra_before.append(pb);spectra_after.append(pn)
                session_refs.append([float(np.std(a[:,i+1])*1000) for i in range(2)])
            if number==cfg['representative_trials'][sid]:
                representatives[sid]=(trial_id,a.copy(),filtered_series[('zero_phase',False)].copy())
        numbers=[s['trial_id'].split(':')[1] for s in info['sources'] if s['primary']]
        if sorted(map(int,numbers))!=list(range(1,13)):raise ValueError(f'{sid}: not exactly trials 1..12')
        psds[sid]=(psd_f,np.mean(spectra_before,axis=0),np.mean(spectra_after,axis=0))
        baselines[sid]=np.mean(session_refs,axis=0).tolist()
    print('FIT/EVALUATE: both pair directions, full and exclude291:7',flush=True)
    event_rows=[];eval_rows=[];params_rows=[]
    for exclude in [False,True]:
        selected=[f for f in features if not(exclude and f['trial_id']=='291:7')]
        for direction,train,test in [('A_to_B',['290','291'],['292','293']),('B_to_A',['292','293'],['290','291'])]:
            for variant in sorted({f['variant'] for f in selected}):
                trials=[f for f in selected if f['variant']==variant]
                for detector in ['A','B','C']:
                    parameters=[select_params(train,selected,variant,ch,detector,cfg) for ch in range(2)]
                    run_id=f'{direction}_{variant}_{detector}_exclude7{int(exclude)}'
                    for ch,param in enumerate(parameters):
                        params_rows.append(dict(run_id=run_id,channel=f'CH{ch+1}',training_sessions=';'.join(train),threshold=param['threshold'],choice=param['choice'],background_prior=param['prior'].tolist() if detector=='B' else None))
                    collections={}
                    for feature in trials:
                        sid=feature['session'];split='audit_no_signal' if not feature['primary'] else 'train' if sid in train else 'test'
                        results=[detect(feature,ch,detector,parameters[ch],cfg) for ch in range(2)]
                        channels={f'CH{ch+1}':(results[ch][0],results[ch][1],detector) for ch in range(2)}
                        for tolerance in cfg['coincidence_ms']:
                            for soft in [False,True]:
                                name=f'D_{detector}_{"SOFT" if soft else "AND"}_{tolerance}ms'
                                channels[f'CH1+CH2_{name}']=(combine(results[0][0],results[1][0],tolerance/1000,soft),'VALID',name)
                        for channel,(events,quality,method) in channels.items():
                            record=dict(run_id=run_id,trial_id=feature['trial_id'],session=sid,label=feature['label'],primary=feature['primary'],channel=channel,detector=method,variant=variant,exclude291_7=exclude,split=split,quality=quality,valid_duration_s=feature['valid_duration_s'],candidate_count=len(events),trial_positive=bool(events))
                            eval_rows.append(record)
                            for e in events:event_rows.append(dict(record,**e))
    write_csv(args.output/'features_per_trial.csv',feature_rows)
    write_csv(args.output/'candidate_events.csv',event_rows)
    per_trial=pd.DataFrame(eval_rows);per_trial.to_csv(args.output/'evaluation_per_trial.csv',index=False)
    aggregated=[]
    keys=['run_id','channel','detector','variant','exclude291_7','split','session','label','primary']
    for key,frame in per_trial.groupby(keys,dropna=False,sort=False):
        row=dict(zip(keys,key));seconds=float(frame.valid_duration_s.sum());count=int(frame.candidate_count.sum())
        row.update(trials=len(frame),valid_duration_s=seconds,candidate_count=count,
            quiet_false_alarms_per_s=count/seconds if row['label']=='QUIET' and row['primary'] else None,
            pick_trial_positive_fraction=float(frame.trial_positive.mean()) if row['label']=='PICK' and row['primary'] else None,
            candidate_rate_per_s=count/seconds,not_calibrated_trials=int(sum(frame.quality!='VALID')))
        aggregated.append(row)
    write_csv(args.output/'evaluation.csv',aggregated)
    (args.output/'thresholds.json').write_text(json.dumps(params_rows,indent=2)+'\n',encoding='utf-8')
    manifest['reference_ac_rms_mV']=baselines
    manifest['no_signal_policy']='audit_only, never training or primary test'
    manifest['clipping_policy']='No known rails/calibration; do not infer clipping solely from extrema. Nonfinite/time errors stop processing.'
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    (args.output/'config.json').write_text(json.dumps(cfg,indent=2)+'\n',encoding='utf-8')
    figures(args.output,feature_rows,per_trial,event_rows,psds,representatives,cfg)
    print(json.dumps(dict(reference_ac_rms_mV=baselines,features=len(feature_rows),candidates=len(event_rows),evaluations=len(aggregated)),indent=2),flush=True)


def figures(out,rows,evaluation,events,psds,reps,cfg):
    df=pd.DataFrame(rows);df=df[df.primary]
    fig,axes=plt.subplots(1,2,figsize=(11,4),constrained_layout=True)
    for ch,ax in zip(['CH1','CH2'],axes):
        for i,sid in enumerate(cfg['sessions']):
            v=df[(df.session==sid)&(df.channel==ch)].ac_rms_mV
            ax.scatter(np.full(len(v),i),v,s=20)
        ax.set(title=f'{ch}: per-trial AC RMS',ylabel='mV RMS',xticks=range(4),xticklabels=list(cfg['sessions']))
    fig.savefig(out/'01_rms.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(12,4),constrained_layout=True)
    for ch,ax in enumerate(axes):
        for sid,(f,p,pn) in psds.items():
            mask=(f>=40)&(f<=3000)
            line=ax.semilogx(f[mask],10*np.log10(np.maximum(p[mask,ch],1e-30)),label=sid)[0]
            ax.semilogx(f[mask],10*np.log10(np.maximum(pn[mask,ch],1e-30)),ls='--',color=line.get_color())
        ax.set(title=f'CH{ch+1} band PSD: solid plain / dashed notch',xlabel='Hz',ylabel='dB V²/Hz');ax.legend()
    fig.savefig(out/'02_psd.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(4,2,figsize=(13,10),constrained_layout=True)
    run='A_to_B_zero_phase_notch0_10ms_A_exclude70'
    for (sid,(trial_id,a,y)),pair in zip(reps.items(),axes):
        for ch,ax in enumerate(pair):
            ax.plot(a[::20,0],y[::20,ch]*1000,lw=.5,label='bandpassed waveform (display stride 20)')
            selected=[e for e in events if e['run_id']==run and e['trial_id']==trial_id and e['channel']==f'CH{ch+1}']
            for e in selected:ax.axvspan(e['t_start_s'],e['t_end_s'],alpha=.25,color='orange')
            ax.set(title=f'{trial_id} CH{ch+1}; A baseline candidate intervals',xlabel='s',ylabel='mV')
    fig.savefig(out/'03_waveform_candidates.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(4,1,figsize=(11,7),constrained_layout=True)
    for (sid,(trial_id,a,y)),ax in zip(reps.items(),axes):
        for ch in ['CH1','CH2','CH1+CH2_D_A_AND_20ms']:
            for e in events:
                if e['run_id']==run and e['trial_id']==trial_id and e['channel']==ch:
                    level={'CH1':0,'CH2':1,'CH1+CH2_D_A_AND_20ms':2}[ch]
                    ax.plot([e['t_start_s'],e['t_end_s']],[level,level],lw=5)
        ax.set(title=trial_id,xlabel='s',xlim=(a[0,0],a[-1,0]));ax.set_yticks([0,1,2],['CH1','CH2','AND20'])
    fig.savefig(out/'04_timeline.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(12,4),constrained_layout=True)
    base=evaluation[(evaluation.split=='test')&evaluation.primary&(~evaluation.exclude291_7)]
    method_names=['A','B','C','D_A_AND_20ms','D_B_AND_20ms','D_C_AND_20ms','D_A_SOFT_20ms']
    colors=dict(zip(method_names,plt.get_cmap('tab10').colors))
    for direction,ax in zip(['A_to_B','B_to_A'],axes):
        subset=base[base.run_id.str.startswith(direction)]
        for method in method_names:
            part=subset[subset.detector==method]
            for (run,channel),g in part.groupby(['run_id','channel']):
                q=g[g.label=='QUIET'];p=g[g.label=='PICK']
                if len(q) and len(p):ax.scatter(q.candidate_count.sum()/q.valid_duration_s.sum(),p.trial_positive.mean(),s=22,label=method,color=colors[method],marker='x' if channel=='CH1' else 'o',alpha=.75)
        handles,labels=ax.get_legend_handles_labels();unique=dict(zip(labels,handles));ax.legend(unique.values(),unique.keys(),fontsize=7)
        ax.set(title=direction,xlabel='QUIET candidates / valid second',ylabel='PICK trial candidate-positive fraction',ylim=(-.05,1.05))
    fig.savefig(out/'05_pair_transfer.png',dpi=150);plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(12,4),constrained_layout=True)
    for ch,ax in zip(['CH1','CH2'],axes):
        sub=base[(base.channel==ch)&(base.detector=='A')&(base.variant=='zero_phase_notch0_10ms')]
        for ex in [False,True]:
            s=evaluation[(evaluation.split=='test')&evaluation.primary&(evaluation.channel==ch)&(evaluation.detector=='A')&(evaluation.variant=='zero_phase_notch0_10ms')&(evaluation.exclude291_7==ex)]
            g=s.groupby('session').candidate_count.sum();ax.plot(g.index,g.values,marker='o',label=f'exclude291:7={ex}')
        ax.set(title=f'{ch} A event count sensitivity (bidirectional test)',xlabel='session',ylabel='candidate count');ax.legend(fontsize=8)
    fig.savefig(out/'06_sensitivity.png',dpi=150);plt.close(fig)
    # Explicit transient audit, not a retrospectively labelled digging event.
    trial,a,y=reps['291'];fig,axes=plt.subplots(2,2,figsize=(12,7),constrained_layout=True)
    for ch in range(2):
        axes[0,ch].plot(a[::10,0],(a[::10,ch+1]-a[:,ch+1].mean())*1000,lw=.5)
        f,t,p=signal.spectrogram(a[:,ch+1],fs=100000,nperseg=2048,noverlap=1536)
        mask=(f>=40)&(f<=3000);mesh=axes[1,ch].pcolormesh(t+a[0,0],f[mask],10*np.log10(np.maximum(p[mask],1e-30)),shading='auto')
        fig.colorbar(mesh,ax=axes[1,ch],label='dB V²/Hz')
        axes[0,ch].set(title=f'{trial} CH{ch+1}: transient audit',ylabel='AC mV');axes[1,ch].set(xlabel='s',ylabel='Hz')
    fig.savefig(out/'07_trial291_7_spectrogram.png',dpi=150);plt.close(fig)


if __name__=='__main__':main()
