import json, pathlib, sys
ROOT = pathlib.Path(__file__).resolve().parents[2]  # repository root (beetle-life-lab)
sys.path.insert(0, str(ROOT / 'host/ad3_bridge'))
from review import ReviewStore, read_jsonl, marks_and_offset, REVIEW_FILE, episode_ranges, FS
OUT = ROOT / 'captures/ad3'
WORK = ROOT / 'captures/ad3_label_review'  # derived band RMS and CSVs (git-ignored)
def runs():
    st = ReviewStore(OUT)
    rs = [r for r in st.runs() if r['started_at_pc'] >= '2026-11-18T06:48']
    return sorted(rs, key=lambda r: r['started_at_pc'])
def labels(run):
    p = OUT / run
    marks, off = marks_and_offset(read_jsonl(p / REVIEW_FILE))
    out = []
    open_ = {}
    for m in sorted(marks, key=lambda m: (m['t_s'], m['seq'])):
        k = m['kind']
        if k.endswith('_START'):
            open_[k[:-6]] = m
        elif k.endswith('_END'):
            s = open_.pop(k[:-4], None)
            if s: out.append(dict(kind=k[:-4], a=s['t_s'], b=m['t_s'], note=(s.get('note') or '') + '|' + (m.get('note') or ''),
                                  blind=bool(s.get('blind') and m.get('blind'))))
        elif k == 'NOISE':
            out.append(dict(kind='NOISE_POINT', a=m['t_s'], b=m['t_s'], note=m.get('note') or '', blind=bool(m.get('blind'))))
    return sorted(out, key=lambda x: x['a']), off
def candidates(run):
    p = OUT / run / 'candidates.jsonl'
    return read_jsonl(p) if p.is_file() else []
