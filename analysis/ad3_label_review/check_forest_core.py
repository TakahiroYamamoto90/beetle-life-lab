# 2026-11-21: does the MCU core (firmware/xiao_esp32s3/include/lowfreq_forest_core.h, float32)
# give the same windows and spans as the PC reference (host/ad3_bridge/lowfreq_forest.py) on the
# recorded runs? Host only: the recorded values go to a host build of the core, never to a device.
#   python check_forest_core.py [--quant-uV 0.01]   (also try the values rounded as a serial line would)
import json
import subprocess
import sys
import numpy as np
from common import OUT, WORK, ROOT, runs
from review import actuation_spans
import lowfreq_activity as lf
import lowfreq_forest as lff

EXE = ROOT / 'captures/test_tmp/test_lowfreq_forest_core.exe'


def build():
    subprocess.run([sys.executable, '-m', 'ziglang', 'c++', '-std=c++17', '-O2', '-Wno-nullability-completeness',
                    '-I', str(ROOT / 'firmware/xiao_esp32s3/include'),
                    str(ROOT / 'firmware/xiao_esp32s3/test/host/test_lowfreq_forest_core.cpp'), '-o', str(EXE)], check=True)


def core_replay(t, ch1, ch2, arm, quant_uv=None):
    if quant_uv:
        ch1 = np.round(ch1 * 1e6 / quant_uv) * quant_uv / 1e6
        ch2 = np.round(ch2 * 1e6 / quant_uv) * quant_uv / 1e6
    text = '\n'.join(f'{round(a * 1000)} {b:.9g} {c:.9g} {int(d)}' for a, b, c, d in zip(t, ch1, ch2, arm))
    out = subprocess.run([str(EXE), '--replay'], input=text, capture_output=True, text=True, check=True).stdout.split('\n')
    w = [l.split() for l in out if l.startswith('W ')]
    s = [l.split() for l in out if l.startswith('S ')]
    p = np.array([float(x[1]) for x in w]); act = np.array([x[2] == '1' for x in w])
    return np.where(p < 0, np.nan, p), act, [(int(x[1]) / 1000, int(x[2]) / 1000) for x in s]


def main():
    quant = float(sys.argv[sys.argv.index('--quant-uV') + 1]) if '--quant-uV' in sys.argv else None
    build()
    model = lff.load_model()
    tot = dict(win=0, use_diff=0, act_diff=0, pmax=0.0, spans_py=0, spans_c=0, span_same=0)
    for r in runs():
        run = r['run']
        manifest = json.loads((OUT / run / 'manifest.json').read_text(encoding='utf-8'))
        arm_spans = [(s['t0_s'], s['t1_s']) for s in actuation_spans(OUT / run, manifest)]
        rows = lf.cached_rows(OUT / run / 'samples.f64', WORK.parent / 'review_cache/lowfreq')
        t, X, usable = lff.features(rows, arm_spans)
        p_py = np.full(len(t), np.nan)
        if usable.any():
            p_py[usable] = lff.predict(model, X[usable])
        act_py = usable & (p_py >= model['threshold'])
        res = lff.judge(rows, arm_spans, model)
        sp_py = [(s['t0_s'], s['t1_s']) for s in res['activity_like_spans']]
        p_c, act_c, sp_c = core_replay(t, rows[1], rows[2], lf._arm_mask(t, arm_spans), quant)
        both = usable & ~np.isnan(p_c)
        tot['win'] += len(t)
        tot['use_diff'] += int(np.sum(usable != ~np.isnan(p_c)))
        tot['act_diff'] += int(np.sum(act_py != act_c))
        tot['pmax'] = max(tot['pmax'], float(np.max(np.abs(p_py[both] - p_c[both]), initial=0)))
        same = sum(any(abs(a - c) <= .26 and abs(b - d) <= .26 for c, d in sp_c) for a, b in sp_py)
        tot['spans_py'] += len(sp_py); tot['spans_c'] += len(sp_c); tot['span_same'] += same
        if len(sp_py) != len(sp_c) or same != len(sp_py) or np.sum(act_py != act_c):
            print(f"  {run[:12]} {r['started_at_pc'][:16]} windows differ {int(np.sum(act_py != act_c))}, spans PC {len(sp_py)} / core {len(sp_c)} (same {same})")
    print(f"{len(runs())} runs, {tot['win']} windows{f' (values rounded to {quant} uV)' if quant else ''}: "
          f"usable differs {tot['use_diff']}, active differs {tot['act_diff']} ({tot['act_diff'] / tot['win']:.3%}), "
          f"max |p diff| {tot['pmax']:.3f}; spans PC {tot['spans_py']} / core {tot['spans_c']}, same within 0.26 s {tot['span_same']}")


if __name__ == '__main__':
    main()
