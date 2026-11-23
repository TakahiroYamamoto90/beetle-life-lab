# 2026-11-20: can a small tree / random forest on the 250 ms low-frequency activity features
# (host/ad3_bridge/lowfreq_activity.py) plus the 20 ms band RMS separate the labelled LARVA/DIG
# ranges better than the frozen ratio >= 3.0 threshold? Analysis only: nothing here changes the
# detector, B2, the XIAO or any label. Models are plain numpy (no scikit-learn in the venv).
#
# Frames: 250 ms windows of the lowfreq tracker. y = 1 inside LARVA/DIG, 0 elsewhere (NOISE frames
# are hard negatives, unlabelled frames are weak negatives: unlabelled larva movement may exist).
# Arm windows and windows without a 10 s floor are left out. Evaluation is per held-out run
# (leave-one-run-out), never by random frames, and across days (11/19 <-> 11/18).
# Limitation: the band RMS cache is zero-phase (sosfiltfilt), the lowfreq features are causal.
import json

import numpy as np
from common import OUT, WORK, runs, labels
from review import actuation_spans
import lowfreq_activity as lf


FLOOR_N, MIN_N = 120, 40


def trailing(x, valid, q):
    """Per-row q-th percentile of the FLOOR_N previous valid values (NaN when < MIN_N)."""
    hist = np.concatenate((np.full(FLOOR_N, np.nan), np.where(valid, x, np.nan)))
    win = np.lib.stride_tricks.sliding_window_view(hist, FLOOR_N)[:len(x)]
    ok = np.sum(~np.isnan(win), axis=1) >= MIN_N
    out = np.full(len(x), np.nan)
    if ok.any():
        out[ok] = np.nanpercentile(win[ok], q, axis=1)
    return out


def past_stat(x, n, fn):
    hist = np.concatenate((np.full(n, np.nan), x))
    win = np.lib.stride_tricks.sliding_window_view(hist, n + 1)[:len(x)]  # rows i-n..i
    with np.errstate(all='ignore'):
        return fn(win, axis=1)


def run_frames(run):
    manifest = json.loads((OUT / run / 'manifest.json').read_text(encoding='utf-8'))
    arm_spans = [(s['t0_s'], s['t1_s']) for s in actuation_spans(OUT / run, manifest)]
    t, ch1, ch2 = lf.cached_rows(OUT / run / 'samples.f64', WORK.parent / 'review_cache/lowfreq')
    arm = lf._arm_mask(t, arm_spans)
    ratio = lf.judge((t, ch1, ch2), arm_spans)
    r2 = np.log(ch2 / trailing(ch2, ~arm, 20))
    r1 = np.log(ch1 / trailing(ch1, ~arm, 20))
    z = np.load(WORK / 'bands' / (run + '.npz'))
    rms = z['rms'].astype(float)                       # (n20, 4 bands, 2 ch), 20 ms frames
    g = np.floor(z['t'] / lf.WINDOW_S).astype(int)     # the 250 ms window each 20 ms frame falls in
    n = min(len(t), g.max())                           # last window may be partial: dropped
    t, ch1, ch2, arm, r1, r2 = t[:n], ch1[:n], ch2[:n], arm[:n], r1[:n], r2[:n]
    sel = g < n; g, rms = g[sel], rms[sel]
    cnt = np.bincount(g, minlength=n)[:, None, None]
    bm = np.zeros((n, 4, 2)); np.add.at(bm, g, rms); bm /= cnt
    bp = np.zeros((n, 4, 2)); np.maximum.at(bp, g, rms)
    feats = {'lf_ch2_x': r2, 'lf_ch1_x': r1, 'lf_ch2_over_ch1': np.log(ch2 / ch1),
             'lf_ch2_x_max1s': past_stat(r2, 4, np.nanmax), 'lf_ch2_x_max3s': past_stat(r2, 12, np.nanmax),
             'lf_ch2_x_mean3s': past_stat(r2, 12, np.nanmean)}
    for i, name in enumerate(['20-300', '300-1500', '1.5-5k', '5-20k']):
        for c, cn in ((1, 'ch2'), (0, 'ch1')):
            feats[f'b_{cn}_{name}_x'] = np.log(bm[:, i, c] / trailing(bm[:, i, c], ~arm, 50))
    feats['b_ch2_300-1500_crest'] = np.log(bp[:, 1, 1] / bm[:, 1, 1])
    X = np.column_stack(list(feats.values()))
    labs, _ = labels(run)
    y = np.zeros(n, int); noise = np.zeros(n, bool)
    for x in labs:
        a, bb = (x['a'] - .5, x['b'] + .5) if x['kind'] == 'NOISE_POINT' else (x['a'], x['b'])
        m = (t + .125 >= a) & (t - .125 <= bb)
        if x['kind'] in ('LARVA', 'DIG'): y[m] = 1
        elif x['kind'].startswith('NOISE'): noise |= m
    keep = ~arm & np.all(np.isfinite(X), axis=1)
    larva = [(x['a'], x['b']) for x in labs if x['kind'] in ('LARVA', 'DIG')]
    nz = [(x['a'], x['b']) for x in labs if x['kind'] == 'NOISE'] + \
         [(x['a'] - .5, x['b'] + .5) for x in labs if x['kind'] == 'NOISE_POINT']
    base = np.log(np.nan_to_num(lf_ratio(ratio, t), nan=0) + 1e-9)
    return dict(run=run, t=t, X=X, y=y, noise=noise, keep=keep, larva=larva, nz=nz, base=base,
                names=list(feats))


def lf_ratio(res, t):
    """judge() keeps only spans; recompute the frame ratio from its spans' complement is not
    possible, so rebuild the ratio the same way judge() does (exposed here for the baseline)."""
    return res['_ratio'][:len(t)]


# judge() returns spans only; wrap it once to also expose the per-window ratio (same code path)
_orig_judge = lf.judge
def _judge_with_ratio(rows, arm_spans=()):
    t, ch1, ch2 = (np.asarray(x, float) for x in rows)
    res = _orig_judge(rows, arm_spans)
    # ratio = ch2 / floor where floor is judge's trailing p20; recompute identically
    arm = lf._arm_mask(t, arm_spans)
    n_floor, n_min = round(lf.FLOOR_S / lf.WINDOW_S), round(lf.MIN_FLOOR_S / lf.WINDOW_S)
    hist = np.concatenate((np.full(n_floor, np.nan), np.where(arm, np.nan, ch2)))
    win = np.lib.stride_tricks.sliding_window_view(hist, n_floor)[:len(t)]
    ok = np.sum(~np.isnan(win), axis=1) >= n_min
    ratio = np.full(len(t), np.nan)
    if ok.any():
        fl = np.nanpercentile(win[ok], lf.FLOOR_PERCENTILE, axis=1)
        ratio[ok] = np.where(fl > 0, ch2[ok] / fl, np.nan)
    res['_ratio'] = ratio
    return res
lf.judge = _judge_with_ratio


# ---------------- numpy CART / random forest ----------------
def gini_split(x, y, w, cands):
    best = (np.inf, None)
    W_ = w.sum(); P = (w * y).sum()
    for c in cands:
        m = x <= c
        wl = w[m].sum(); wr = W_ - wl
        if wl <= 0 or wr <= 0: continue
        pl = (w[m] * y[m]).sum() / wl; pr = (P - (w[m] * y[m]).sum()) / wr
        g = wl * pl * (1 - pl) + wr * pr * (1 - pr)
        if g < best[0]: best = (g, c)
    return best


def grow(X, y, w, depth, max_depth, min_leaf, mtry, rng, nq=24):
    p = (w * y).sum() / w.sum()
    if depth >= max_depth or len(y) < 2 * min_leaf or p in (0, 1):
        return ('leaf', p, len(y))
    feats = rng.choice(X.shape[1], mtry, replace=False) if mtry < X.shape[1] else range(X.shape[1])
    best = (np.inf, None, None)
    parent = w.sum() * p * (1 - p)
    for f in feats:
        cands = np.unique(np.quantile(X[:, f], np.linspace(.02, .98, nq)))
        g, c = gini_split(X[:, f], y, w, cands)
        if c is not None and g < best[0]:
            m = X[:, f] <= c
            if min_leaf <= m.sum() <= len(y) - min_leaf: best = (g, f, c)
    if best[1] is None or best[0] >= parent - 1e-12:
        return ('leaf', p, len(y))
    _, f, c = best; m = X[:, f] <= c
    return ('split', f, c, grow(X[m], y[m], w[m], depth + 1, max_depth, min_leaf, mtry, rng, nq),
            grow(X[~m], y[~m], w[~m], depth + 1, max_depth, min_leaf, mtry, rng, nq), p, len(y))


def predict_tree(node, X):
    if node[0] == 'leaf': return np.full(len(X), node[1])
    _, f, c, L, R, *_ = node; m = X[:, f] <= c; out = np.empty(len(X))
    out[m] = predict_tree(L, X[m]); out[~m] = predict_tree(R, X[~m]); return out


class Forest:
    def __init__(self, n_trees=60, max_depth=7, min_leaf=20, mtry='sqrt', seed=0, bootstrap=True):
        self.k = dict(n_trees=n_trees, max_depth=max_depth, min_leaf=min_leaf, mtry=mtry, seed=seed, bootstrap=bootstrap)

    def fit(self, X, y, w):
        k = self.k; rng = np.random.default_rng(k['seed'])
        mtry = max(1, int(np.sqrt(X.shape[1]))) if k['mtry'] == 'sqrt' else X.shape[1]
        self.trees = []
        for _ in range(k['n_trees']):
            idx = rng.integers(0, len(y), len(y)) if k['bootstrap'] else np.arange(len(y))
            self.trees.append(grow(X[idx], y[idx], w[idx], 0, k['max_depth'], k['min_leaf'], mtry, rng))
        return self

    def predict(self, X):
        return np.mean([predict_tree(t, X) for t in self.trees], axis=0)


def rules(node, names, ind=''):
    if node[0] == 'leaf': return [f"{ind}-> P(LARVA/DIG)={node[1]:.2f}  (n={node[2]})"]
    _, f, c, L, R, *_ = node
    return [f"{ind}if {names[f]} <= {c:.2f}:"] + rules(L, names, ind + '   ') + \
           [f"{ind}else ({names[f]} > {c:.2f}):"] + rules(R, names, ind + '   ')


# ---------------- evaluation (same measures as a8.py) ----------------
def spans_of(t, on, gap=1.0):
    out = []
    for i in np.flatnonzero(on):
        a, b = t[i] - .125, t[i] + .125
        if out and a - out[-1][1] <= gap: out[-1][1] = b
        else: out.append([a, b])
    return out


def ov(a, b, spans): return any(x <= b and y >= a for x, y in spans)


def ap(score, y):
    o = np.argsort(-score); y = y[o]; tp = np.cumsum(y)
    prec = tp / np.arange(1, len(y) + 1)
    return float((prec * y).sum() / max(1, y.sum()))


def evaluate(D, scores, thr):
    tot = dict(ep=0, hit=0, nz=0, nzhit=0, ls=0, lin=0, us=0, uin=0)
    for d, s in zip(D, scores):
        k = d['keep']; on = np.zeros(len(d['t']), bool); on[k] = s >= thr
        sp = spans_of(d['t'], on)
        unl = k & (d['y'] == 0) & ~d['noise']
        tot['ep'] += len(d['larva']); tot['hit'] += sum(ov(a, b, sp) for a, b in d['larva'])
        tot['nz'] += len(d['nz']); tot['nzhit'] += sum(ov(a, b, sp) for a, b in d['nz'])
        lm = k & (d['y'] == 1)
        tot['ls'] += lm.sum(); tot['lin'] += (on & lm).sum(); tot['us'] += unl.sum(); tot['uin'] += (on & unl).sum()
    return (f"LARVA/DIG ranges {tot['hit']:>3}/{tot['ep']:<3} NOISE ranges {tot['nzhit']:>2}/{tot['nz']:<2} "
            f"LARVA time {tot['lin'] / max(1, tot['ls']):4.0%}  unlabelled time {tot['uin'] / max(1, tot['us']):5.1%}"), tot['uin'] / max(1, tot['us'])


def matched_thr(D, scores, target):
    """Threshold giving about `target` of unlabelled time flagged (to compare at equal false alarms)."""
    u = np.concatenate([s[(d['y'][d['keep']] == 0) & ~d['noise'][d['keep']]] for d, s in zip(D, scores)])
    return float(np.quantile(u, 1 - target))


def xy(D, sel):
    X = np.concatenate([d['X'][d['keep']][:, sel] for d in D]); y = np.concatenate([d['y'][d['keep']] for d in D])
    nz = np.concatenate([d['noise'][d['keep']] for d in D])
    w = np.where(y == 1, (y == 0).sum() / max(1, y.sum()), 1.0) * np.where(nz, 3.0, 1.0)  # balance + hard negatives
    return X, y, w


def main():
    data = [run_frames(r['run']) for r in runs()]
    names = data[0]['names']
    d19 = [d for d in data if r_day(d) == '19']; d18 = [d for d in data if r_day(d) == '18']
    sets = {'lowfreq only': [i for i, n in enumerate(names) if n.startswith('lf_')], 'lowfreq + bands': list(range(len(names)))}
    models = {'tree depth3': dict(n_trees=1, max_depth=3, min_leaf=40, mtry='all', bootstrap=False),
              'forest 60x d7': dict(n_trees=60, max_depth=7, min_leaf=20)}
    base_thr = np.log(lf.RATIO_MIN)

    def keep_scores(D, key='base'):
        return [d[key][d['keep']] for d in D]

    for title, test_sets in (('11/19 leave-one-run-out (9 runs)', None), ('train 11/19 -> test 11/18 (15 runs)', (d19, d18)),
                             ('train 11/18 -> test 11/19', (d18, d19))):
        print(f"\n=== {title} ===")
        tD = d19 if test_sets is None else test_sets[1]
        bs = keep_scores(tD)
        txt, u_base = evaluate(tD, bs, base_thr)
        yy = np.concatenate([d['y'][d['keep']] for d in tD])
        print(f"  baseline ratio>=3.0         AP {ap(np.concatenate(bs), yy):.2f}  {txt}")
        for sname, sel in sets.items():
            for mname, kw in models.items():
                if test_sets is None:
                    sc = []
                    for i, d in enumerate(d19):
                        X, y, w = xy([e for j, e in enumerate(d19) if j != i], sel)
                        sc.append(Forest(**kw).fit(X, y, w).predict(d['X'][d['keep']][:, sel]))
                else:
                    X, y, w = xy(test_sets[0], sel); f = Forest(**kw).fit(X, y, w)
                    sc = [f.predict(d['X'][d['keep']][:, sel]) for d in tD]
                thr = matched_thr(tD, sc, u_base)
                txt, _ = evaluate(tD, sc, thr)
                print(f"  {sname:15s} {mname:13s} AP {ap(np.concatenate(sc), yy):.2f}  {txt}  (at baseline's false-alarm rate)")
                txt5, _ = evaluate(tD, sc, .5)
                print(f"  {'':15s} {'':13s}          {txt5}  (P>=0.5)")
    # the readable tree, trained on all 11/19 runs
    sel = sets['lowfreq + bands']; X, y, w = xy(d19, sel)
    tree = Forest(**models['tree depth3']).fit(X, y, w).trees[0]
    print("\n=== depth-3 tree trained on all 11/19 runs (lowfreq + bands) ===")
    print('\n'.join(rules(tree, [names[i] for i in sel])))
    # forest feature importance by permutation on 11/18 (trained on 11/19)
    f = Forest(**models['forest 60x d7']).fit(X, y, w)
    Xt = np.concatenate([d['X'][d['keep']] for d in d18]); yt = np.concatenate([d['y'][d['keep']] for d in d18])
    a0 = ap(f.predict(Xt), yt); rng = np.random.default_rng(1); imp = []
    for j, n in enumerate(names):
        Xp = Xt.copy(); Xp[:, j] = rng.permutation(Xp[:, j]); imp.append((a0 - ap(f.predict(Xp), yt), n))
    print(f"\n=== permutation importance (forest trained 11/19, AP drop on 11/18, AP {a0:.2f}) ===")
    for v, n in sorted(imp, reverse=True)[:10]: print(f"  {n:24s} {v:+.3f}")


def r_day(d):
    return next(r['started_at_pc'][8:10] for r in RUNS if r['run'] == d['run'])


RUNS = runs()
if __name__ == '__main__':
    main()
