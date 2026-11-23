"""Development-only continuous candidates; never biological activity evidence.

State is past-only. Startup/rearm are calibration assumptions, not quiet labels.
"""
import argparse
from collections import deque
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from pipeline import digest, write_csv
from continuous import extract


class ContinuousCandidate:
    def __init__(self,cfg,model):
        self.cfg=cfg;self.model=model;self.state='WARMUP'
        self.background=deque(maxlen=round(cfg['background_s']/cfg['window_s']))
        self.rearm=deque(maxlen=round(cfg['rearm_s']/cfg['window_s']))
        self.smoothed=None;self.previous_t=None;self.high_count=0;self.low_count=0
        self.active=None;self.events=[];self.trace=[];self.last_score=None;self.fault_reason=None

    def update(self,t,value):
        c=self.cfg;m=self.model;step=c['window_s']
        if not math.isfinite(value) or value<0 or (self.previous_t is not None and not math.isclose(t-self.previous_t,step,abs_tol=1e-7)):
            self._fault(t,'INVALID_OR_MISSING_WINDOW');return
        self.previous_t=t
        alpha=1-math.exp(-step/c['smoothing_tau_s'])
        self.smoothed=value if self.smoothed is None else self.smoothed+alpha*(value-self.smoothed)
        x=self.smoothed;baseline=None;score=None;reason=''
        if self.state=='WARMUP':
            self.background.append(x)
            if len(self.background)>=round(c['warmup_s']/step):
                if np.median(self.background)<=1e-12:self._fault(t,'CALIBRATION_NO_SIGNAL')
                else:self.state='MONITORING'
            reason='INITIAL_BACKGROUND_ASSUMED_UNSTIMULATED'
        elif self.state=='REARMING':
            self.rearm.append(x)
            if len(self.rearm)==self.rearm.maxlen:
                a=np.asarray(self.rearm);med=float(np.median(a))
                if (np.quantile(a,.9)-np.quantile(a,.1))/max(med,1e-12)<=c['rearm_stability_fraction']:
                    self.background.clear();self.background.extend(self.rearm)
                    self.state='MONITORING';self.high_count=0;self.rearm.clear()
            reason='REARM_BASELINE_NOT_PROOF_OF_QUIET'
        elif self.state!='FAULT':
            b=np.asarray(self.background);baseline=float(np.median(b))
            scale=max(1.4826*float(np.median(abs(b-baseline))),c['scale_floor_fraction']*baseline,1e-12)
            score=x/max(baseline,1e-12) if m['kind']=='ratio' else (x-baseline)/scale
            if self.state=='MONITORING':
                self.high_count=self.high_count+1 if score>m['on'] else 0
                if self.high_count>=round(c['confirm_s']/step):
                    self.state='CANDIDATE';self.low_count=0
                    self.active=dict(t_start_s=t-(self.high_count-1)*step-step/2,
                        confirmed_at_s=t+step/2,peak_rms_V=x,peak_score=score,
                        baseline_at_start_V=baseline,threshold=m['on'])
                elif score<=m['update_limit']:
                    self.background.append(x)
            elif self.state=='CANDIDATE':
                self.active['peak_rms_V']=max(x,self.active['peak_rms_V'])
                self.active['peak_score']=max(score,self.active['peak_score'])
                low=score<=m['off'];drop=x<=self.active['peak_rms_V']*(1-c['peak_drop_fraction'])
                self.low_count=self.low_count+1 if low or drop else 0
                if self.low_count>=round(c['release_confirm_s']/step):
                    self._close(t+step/2,'BASELINE_RETURN' if low else 'FALL_FROM_PEAK','DEVELOPMENT_CANDIDATE')
                    self.state='REARMING';self.rearm.clear();reason='NEEDS_REARM'
                elif t+step/2-self.active['t_start_s']>=c['max_candidate_s']:
                    self._fault(t,'MAX_DURATION_MANUAL_RESET_REQUIRED')
            self.last_score=score
        self.trace.append(dict(t_center_s=t,rms_V=value,smoothed_V=x,baseline_V=baseline,
            score=score,state=self.state,reason=self.fault_reason if self.state=='FAULT' else reason))

    def _close(self,end,reason,quality):
        if self.active is not None:
            self.events.append(dict(self.active,t_end_s=end,end_reason=reason,quality=quality))
            self.active=None

    def _fault(self,t,reason):
        self._close(t,'FAULT:'+reason,'INCOMPLETE')
        self.state='FAULT';self.fault_reason=reason

    def finish(self,t):
        self._close(t,'CAPTURE_ENDED','INCOMPLETE')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    inputs=ap.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--features',type=Path)
    inputs.add_argument('--input',type=Path)
    ap.add_argument('--source-manifest',type=Path)
    ap.add_argument('--preprocessing-frozen',type=Path)
    ap.add_argument('--annotations',type=Path)
    ap.add_argument('--config',type=Path,default=Path(__file__).with_name('improved_config.json'))
    ap.add_argument('--output',type=Path,required=True)
    args=ap.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    cfg=json.loads(args.config.read_text())
    if args.features:
        if not args.source_manifest:ap.error('--features requires --source-manifest')
        frame=pd.read_csv(args.features)
        source=json.loads(args.source_manifest.read_text(encoding='utf-8'))
        source_hash=digest(args.source_manifest)
    else:
        if not args.preprocessing_frozen:ap.error('--input requires --preprocessing-frozen')
        preprocessing=json.loads(args.preprocessing_frozen.read_text())
        raw_times,raw_rms,audit=extract(args.input,preprocessing['preprocessing_config'],50000)
        frame=pd.DataFrame(dict(t_center_s=raw_times,CH2_band_rms_mV=raw_rms[:,1]*1000))
        source=dict(input_sha256=digest(args.input),input_path=str(args.input.resolve()),audit=audit,
            preprocessing_config=preprocessing['preprocessing_config'],preprocessing_sha256=digest(args.preprocessing_frozen))
        source_hash=None
    notes=json.loads(args.annotations.read_text(encoding='utf-8')) if args.annotations else dict(operation_start_s_approx=[],annotation_quality='UNANNOTATED')
    frame[['t_center_s','CH2_band_rms_mV']].to_csv(args.output/'input_features_20ms.csv',index=False)
    # Never consume the old, retracted QUIET/PICK labels from the features CSV.
    times=frame.t_center_s.to_numpy();rms=frame.CH2_band_rms_mV.to_numpy()/1000
    rows=[];traces=[];summary=[];detectors={}
    for name,model in cfg['models'].items():
        d=ContinuousCandidate(cfg,model)
        for t,x in zip(times,rms):d.update(float(t),float(x))
        d.finish(float(times[-1]+cfg['window_s']/2));detectors[name]=d
        rows.extend(dict(model=name,**e) for e in d.events)
        traces.extend(dict(model=name,**r) for r in d.trace)
        summary.append(dict(model=name,candidate_count=len(d.events),candidate_duration_s=sum(e['t_end_s']-e['t_start_s'] for e in d.events),
            incomplete_count=sum(e['quality']=='INCOMPLETE' for e in d.events),final_state=d.state,fault_reason=d.fault_reason))
    write_csv(args.output/'candidate_events.csv',rows);write_csv(args.output/'trace_20ms.csv',traces)
    write_csv(args.output/'summary.csv',summary)
    manifest=dict(config=cfg,config_sha256=digest(args.config),analysis_code_sha256=digest(Path(__file__)),
        input_features_sha256=digest(args.features) if args.features else digest(args.output/'input_features_20ms.csv'),source_manifest_sha256=source_hash,
        original_input_sha256=source['input_sha256'],source=source,annotations_sha256=digest(args.annotations) if args.annotations else None,
        annotations=notes,configuration_developed_on='trial296',
        scope='Configuration developed on trial296. No automatic fit on this input. Annotation quality controls permissible evaluation claims. Prior frozen files unchanged.')
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
    # A prospective config, selected on this development capture; not validated on new data.
    (args.output/'prospective_config.json').write_text(json.dumps(cfg,indent=2)+'\n',encoding='utf-8')
    fig,axes=plt.subplots(3,1,figsize=(13,9),sharex=True,constrained_layout=True)
    axes[0].plot(times,rms*1000,lw=.5,label='CH2 band RMS')
    for name,d in detectors.items():
        tr=pd.DataFrame(d.trace)
        axes[0].plot(tr.t_center_s,tr.baseline_V*1000,lw=1,label=name+' background')
    axes[0].set(ylabel='mV RMS');axes[0].legend(fontsize=8)
    for ax,(name,d) in zip(axes[1:],detectors.items()):
        tr=pd.DataFrame(d.trace);ax.plot(tr.t_center_s,tr.score,lw=.6)
        ax.axhline(cfg['models'][name]['on'],color='red',ls='--')
        for e in d.events:ax.axvspan(e['t_start_s'],e['t_end_s'],color='green',alpha=.15)
        ax.set(ylabel=name)
    for ax in axes:
        for start in notes['operation_start_s_approx']:ax.axvline(start,color='orange',ls='--')
        ax.grid(alpha=.2)
    axes[-1].set(xlabel='Elapsed seconds; orange = operator-confirmed approximate starts, ends unknown')
    record_name=args.input.stem if args.input else Path(source.get('input','cached_features')).stem
    fig.suptitle(f'{record_name}: development configuration; relative thresholds, guarded background, peak-fall release')
    fig.savefig(args.output/'comparison.png',dpi=150);plt.close(fig)
    print(pd.DataFrame(rows).to_string(index=False));print(pd.DataFrame(summary).to_string(index=False))


if __name__=='__main__':main()
