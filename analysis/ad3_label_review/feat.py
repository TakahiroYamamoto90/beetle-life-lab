import csv, json, math
import numpy as np
from common import *
sys.path.insert(0, str(ROOT / 'analysis/passive_detection'))
from improved import ContinuousCandidate
CFG = json.loads((ROOT / 'analysis/passive_detection/improved_config.json').read_text())
BN = ['lo20-300', 'mid300-1500', 'hi1.5-5k', 'vhi5-20k']

def features_csv(run):
    with (OUT / run / 'features.csv').open(encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    t = np.array([float(r['t_center_s']) for r in rows]); x = np.array([float(r['rms_V']) for r in rows])
    s = np.array([float(r['score']) if r['score'] else np.nan for r in rows])
    return t, x, s

def replay(t, x, cfg=CFG, model='B2_guarded_mad', gate=None):
    """Re-run the detector on the 20 ms CH2 RMS, resetting after MAX_DURATION like processing.py."""
    det = ContinuousCandidate(cfg, cfg['models'][model]); ev = []; scores = []
    for ti, xi in zip(t, x):
        det.update(float(ti), float(xi)); tr = det.trace.pop(); scores.append(tr['score'])
        ev += det.events; det.events.clear()
        if det.state == 'FAULT':
            det = ContinuousCandidate(cfg, cfg['models'][model])
    det.finish(t[-1] + .01); ev += det.events
    return ev, np.array([np.nan if s is None else s for s in scores])

def load_bands(run):
    z = np.load(WORK / 'bands' / (run + '.npz')); return z['t'], z['rms']

def seg_features(tb, rms, a, b, busy, base_s=5.0, gap=.3):
    """Rises vs the median of the base_s before (spans in busy left out), per band and channel."""
    inside = (tb >= a - .01) & (tb <= max(b, a + .02) + .01)
    base = (tb >= a - gap - base_s) & (tb < a - gap)
    for (u, v) in busy:
        base &= ~((tb >= u - gap) & (tb <= v + gap))
    if base.sum() < 25 or not inside.any():
        return None
    med = np.median(rms[base], axis=0)  # (bands, ch)
    mean_in = rms[inside].mean(axis=0); peak_in = rms[inside].max(axis=0)
    f = {}
    for i, n in enumerate(BN):
        for c, cn in ((1, 'ch2'), (0, 'ch1')):
            f[f'{cn}_{n}_x'] = float(mean_in[i, c] / med[i, c])
            f[f'{cn}_{n}_peakx'] = float(peak_in[i, c] / med[i, c])
    f['ch1_over_ch2'] = f['ch1_mid300-1500_x'] / f['ch2_mid300-1500_x']
    # spectral shape of the CH2 excess energy: share of each band in (inside^2 - base^2)
    ex = np.clip(mean_in[:, 1] ** 2 - med[:, 1] ** 2, 0, None); tot = ex.sum() or 1e-30
    for i, n in enumerate(BN): f[f'ch2_share_{n}'] = float(ex[i] / tot)
    ex1 = np.clip(mean_in[:, 0] ** 2 - med[:, 0] ** 2, 0, None); tot1 = ex1.sum() or 1e-30
    for i, n in enumerate(BN): f[f'ch1_share_{n}'] = float(ex1[i] / tot1)
    # burstiness of CH2 mid band inside the span: peak / mean
    f['ch2_crest'] = float(peak_in[1, 1] / mean_in[1, 1])
    f['dur_s'] = b - a
    return f

def overlap(a, b, c, d, tol=0.0):
    return min(b, d) + tol >= max(a, c) - tol
