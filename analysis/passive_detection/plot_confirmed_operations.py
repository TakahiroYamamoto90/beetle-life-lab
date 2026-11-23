"""Plot operator-confirmed approximate starts; do not infer operation ends or refit."""
import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--results',type=Path,required=True)
    args=ap.parse_args();out=args.results
    notes=json.loads((out/'operator_annotations.json').read_text(encoding='utf-8'))
    manifest=json.loads((out/'manifest.json').read_text(encoding='utf-8'))
    model=next(m for m in manifest['frozen_config']['candidates'] if m['detector']=='A')
    f=pd.read_csv(out/'features_20ms.csv');events=pd.read_csv(out/'candidate_events.csv')
    fig,axes=plt.subplots(3,1,figsize=(13,9),sharex=True,constrained_layout=True)
    axes[0].plot(f.t_center_s,f.CH2_band_rms_mV,lw=.6)
    axes[0].axhline(model['parameters']['threshold']*1000,color='red',ls='--')
    axes[0].set(ylabel='CH2 band RMS (mV)')
    axes[1].plot(f.t_center_s,f.B_score,lw=.6)
    axes[1].axhline(6,color='red',ls='--');axes[1].set(ylabel='B robust score')
    for _,e in events.iterrows():
        level=0 if '_A_' in e.model else 1
        axes[2].plot([e.t_start_s,e.t_end_s],[level,level],lw=5)
    axes[2].set(yticks=[0,1],yticklabels=['A','B'],ylim=(-.5,1.5),xlabel='Elapsed seconds from first CSV sample')
    for ax in axes:
        for start in notes['operation_start_s_approx']:
            ax.axvline(start,color='darkorange',ls='--',alpha=.8)
        ax.grid(alpha=.2)
    fig.suptitle('trial296: user-confirmed four digging operations; approximate starts only, ends unknown')
    fig.savefig(out/'timeline_confirmed_operations.png',dpi=150)


if __name__=='__main__':main()
