"""Low-frequency activity-likeness by a small random forest: PC-only reference (2026-11-20).

A step towards running a learned model on the MCU. The forest is small (flat arrays of
feature / threshold / child / value, see ml/lowfreq_forest/) and its six inputs are causal and
cheap: they need only the 250 ms CH1/CH2 1-40 Hz RMS rows of lowfreq_activity.LowTracker, a
trailing 30 s 20th percentile of each channel and a few running max/means. The same arrays are
written as a C header by analysis/ad3_label_review/train_forest.py and run, with the same features,
by firmware/xiao_esp32s3/include/lowfreq_forest_core.h (the MCU core, not wired in yet); nothing here is sent to the XIAO and the detector, B2 and candidates are unchanged.

Trained on the labelled runs of 2026-11-18 and 2026-11-19 (LARVA/DIG = 1). Runs it was trained on
are in-sample; across days the earlier test (analysis/ad3_label_review/a9.py) did not transfer,
so it is provisional and not a detection: knocks and other vibration also raise it.
"""
import json
from pathlib import Path

import numpy as np

import lowfreq_activity as lf

SCHEMA = 'bll.lowfreq_forest.v1'
MODEL_DIR = Path(__file__).resolve().parents[2] / 'ml' / 'lowfreq_forest'
MODEL_FILE = MODEL_DIR / 'lowfreq_forest_v1.json'
# the same model as C arrays for the MCU core (firmware/xiao_esp32s3/include/lowfreq_forest_core.h)
HEADER_FILE = Path(__file__).resolve().parents[2] / 'firmware' / 'xiao_esp32s3' / 'include' / 'lowfreq_forest_model.h'
FEATURES = ['ch2_x', 'ch1_x', 'ch2_over_ch1', 'ch2_x_max1s', 'ch2_x_max3s', 'ch2_x_mean3s']
NOTE = ('Reference only: a small random forest (MCU-sized) on the CH1/CH2 1-40 Hz 250 ms rows, trained '
        'on the labelled runs of 2026-11-18/19. Provisional; not a detection, not sent to the XIAO; '
        'runs used for training are in-sample.')


def trailing_floor(x, arm):
    """judge()'s floor: 20th percentile of the previous 120 non-arm windows (NaN until 40 exist)."""
    n_floor, n_min = round(lf.FLOOR_S / lf.WINDOW_S), round(lf.MIN_FLOOR_S / lf.WINDOW_S)
    hist = np.concatenate((np.full(n_floor, np.nan), np.where(arm, np.nan, x)))
    win = np.lib.stride_tricks.sliding_window_view(hist, n_floor)[:len(x)]
    ok = np.sum(~np.isnan(win), axis=1) >= n_min
    out = np.full(len(x), np.nan)
    if ok.any():
        out[ok] = np.nanpercentile(win[ok], lf.FLOOR_PERCENTILE, axis=1)
    return out


def _past(x, n, fn):
    hist = np.concatenate((np.full(n, np.nan), x))
    win = np.lib.stride_tricks.sliding_window_view(hist, n + 1)[:len(x)]   # windows i-n .. i
    with np.errstate(all='ignore'):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            return fn(win, axis=1)


def features(rows, arm_spans=()):
    """(t, X[n, 6], usable[n]) from lowfreq rows. Causal: row i uses rows <= i only."""
    t, ch1, ch2 = (np.asarray(x, dtype=float) for x in rows)
    arm = lf._arm_mask(t, arm_spans)
    with np.errstate(all='ignore'):
        f2, f1 = trailing_floor(ch2, arm), trailing_floor(ch1, arm)
        r2 = np.log(ch2 / f2)
        r1 = np.log(ch1 / f1)
        ratio = np.log(ch2 / ch1)
    X = np.column_stack((r2, r1, ratio, _past(r2, 4, np.nanmax), _past(r2, 12, np.nanmax), _past(r2, 12, np.nanmean)))
    usable = ~arm & np.all(np.isfinite(X), axis=1)
    return t, X, usable


def load_model(path=MODEL_FILE):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def predict(model, X):
    """Mean leaf value over the trees; each tree is flat arrays (feature -1 = leaf), as in C."""
    out = np.zeros(len(X))
    for tree in model['trees']:
        feat, thr, left, right, value = (np.asarray(tree[k]) for k in ('feature', 'threshold', 'left', 'right', 'value'))
        node = np.zeros(len(X), int)
        for _ in range(model['max_depth'] + 1):
            f = feat[node]
            inner = f >= 0
            if not inner.any():
                break
            go_left = X[np.arange(len(X)), np.where(inner, f, 0)] <= thr[node]
            node = np.where(inner, np.where(go_left, left[node], right[node]), node)
        out += value[node]
    return out / len(model['trees'])


def judge(rows, arm_spans=(), model=None, run=None):
    """Forest probability per window and merged spans at the model's threshold (display only)."""
    model = model or load_model()
    if model is None:
        return dict(schema=SCHEMA, status='NO_MODEL', activity_like_spans=[], biological_activity='UNKNOWN')
    t, X, usable = features(rows, arm_spans)
    p = np.full(len(t), np.nan)
    if usable.any():
        p[usable] = predict(model, X[usable])
    active = usable & (p >= model['threshold'])
    spans = []
    for i in np.flatnonzero(active):
        a, b = t[i] - lf.WINDOW_S / 2, t[i] + lf.WINDOW_S / 2
        if spans and a - spans[-1]['t1_s'] <= lf.MERGE_GAP_S:
            spans[-1]['t1_s'] = b; spans[-1]['_i'].append(i)
        else:
            spans.append(dict(t0_s=a, t1_s=b, _i=[i]))
    for s in spans:
        idx = np.array(s.pop('_i'))
        s['t0_s'], s['t1_s'] = round(float(s['t0_s']), 3), round(float(s['t1_s']), 3)
        s['peak_p'] = round(float(np.max(p[idx])), 2)
    return dict(schema=SCHEMA, status=model['status'], model=model['name'], note=NOTE,
                threshold=model['threshold'], trees=len(model['trees']), max_depth=model['max_depth'],
                in_training=run in set(model.get('training_runs', [])),
                activity_like_spans=spans, active_s=round(float(active.sum() * lf.WINDOW_S), 2),
                usable_s=round(float(usable.sum() * lf.WINDOW_S), 2), biological_activity='UNKNOWN')
