"""Retrospective review of saved AD3 runs: read-only on recorded data, append-only human labels.

Labels made here go to review_annotations.jsonl next to the run. They never change samples,
features, candidates, the live operator marks (annotations.jsonl), the detector or the XIAO.
Times are AD3 sample indices chosen by a person looking at the plots, so they are exact on
the sample clock but only as good as the reviewer's judgement.
"""
import base64
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import threading

import numpy as np
from scipy import signal

import disturbance
import lowfreq_activity
import lowfreq_forest
import shadow_trigger

FS = 100000
RUN_RE = re.compile(r'ad3-[0-9a-f]{32}')
REVIEW_FILE = 'review_annotations.jsonl'
# Deleting a run (2026-11-18) is a recoverable trash: trash.jsonl next to the run gets one
# appended record per delete/restore and the latest one wins. Nothing recorded is removed or
# changed; a deleted run is left out of the run list, episodes, review, demo replay and diary.
TRASH_FILE = 'trash.jsonl'
# Annotation progress of a run (2026-11-18): NOT_STARTED until anything is saved in the review
# (a label, a removal, a video offset), then IN_PROGRESS; DONE only when a person says so.
# review_status.jsonl gets one appended record per DONE / reopen and the latest one wins.
STATUS_FILE = 'review_status.jsonl'
REVIEW_STATUSES = ('NOT_STARTED', 'IN_PROGRESS', 'DONE')
# NOISE is a point (a knock, a word); NOISE_START/END mark a lasting disturbance (talking, music).
REVIEW_KINDS = ('LARVA_START', 'LARVA_END', 'DIG_START', 'DIG_END', 'NOISE', 'NOISE_START', 'NOISE_END', 'SYNC_TAP')
VIDEO_OFFSET = 'VIDEO_OFFSET'  # video_time = ad3_time + offset_s; latest active record wins
MAX_OFFSET_S = 600.0
SPEC_ROWS = 96
SPEC_MAX_COLS = 2000
SPEC_FMIN, SPEC_FMAX = 40.0, 16000.0
MAX_NOTE = 300
OVERVIEW_BINS = 6000
WAVE_POINTS = 2000
MAX_WAVE_S = 60.0
REMUX_KEEP = 5  # stream-copied MP4s kept in captures/review_cache (about 60 MB per 3 min)
LOW_BAND_HZ = (1, 40)
LOW_DECIMATE = 50  # block mean 100 kHz -> 2 kHz before the 1-40 Hz filter (display only)


def trashed(path):
    """The latest delete record of a run directory, or None while the run is in use."""
    records = read_jsonl(path / TRASH_FILE) if (path / TRASH_FILE).is_file() else []
    return records[-1] if records and records[-1].get('deleted') is True else None


def review_status(path):
    """dict(status, done_at_pc) of a run directory; see STATUS_FILE."""
    records = read_jsonl(path / STATUS_FILE) if (path / STATUS_FILE).is_file() else []
    if records and records[-1].get('status') == 'DONE':
        return dict(status='DONE', done_at_pc=records[-1].get('created_at_pc'))
    touched = records or ((path / REVIEW_FILE).is_file() and read_jsonl(path / REVIEW_FILE))
    return dict(status='IN_PROGRESS' if touched else 'NOT_STARTED', done_at_pc=None)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def finite(value):
    return None if value is None or not math.isfinite(value) else round(float(value), 9)


def read_jsonl(path):
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def active_review(records):
    """Labels still in effect; nothing is deleted. A RETRACT record hides its target. A label
    moved in review is a new record with replaces_seq, which hides the label it replaces for
    as long as the new one is itself in effect (retracting the move brings the old one back)."""
    by_seq = {r.get('seq'): r for r in records}
    retracted = set()
    for r in records:
        if r.get('kind') != 'RETRACT':
            continue
        for target in r.get('target_seqs') or [r.get('target_seq')]:
            retracted.add(target)
            # with_history (delete from the graph): the positions it was moved from go too
            seen, x = set(), by_seq.get(target)
            while r.get('with_history') and x and x.get('replaces_seq') is not None and x['replaces_seq'] not in seen:
                seen.add(x['replaces_seq'])
                retracted.add(x['replaces_seq'])
                x = by_seq.get(x['replaces_seq'])
    live = [r for r in records if r.get('kind') != 'RETRACT' and r.get('seq') not in retracted]
    hidden = {r['replaces_seq'] for r in live if r.get('replaces_seq') is not None}
    return [r for r in live if r.get('seq') not in hidden]


def marks_and_offset(records):
    """(time labels, latest saved video offset record or None)."""
    active = active_review(records)
    offsets = [r for r in active if r.get('kind') == VIDEO_OFFSET]
    return [r for r in active if r.get('kind') != VIDEO_OFFSET], (offsets[-1] if offsets else None)


def video_path(path, manifest):
    """Recording moved into the run folder, else where OBS left it; None if missing."""
    video = manifest.get('video') or {}
    candidates = []
    if video.get('video_file') and Path(video['video_file']).name == video['video_file']:
        candidates.append(path / video['video_file'])
    if video.get('output_path'):
        candidates.append(Path(video['output_path']))
    for candidate in candidates:
        if candidate.suffix.lower() in ('.mkv', '.mp4', '.mov') and candidate.is_file():
            return candidate
    return None


POSE_OF_ACTION = {'CAPTURE_FRONT': 'FRONT', 'CAPTURE_TOP': 'TOP', 'CAPTURE_BACK': 'BACK', 'CAPTURE_FIXED': 'FIXED'}
EPISODE_RANGES = {'LARVA': ('LARVA_START', 'LARVA_END'), 'DIG': ('DIG_START', 'DIG_END'),
                  'NOISE': ('NOISE_START', 'NOISE_END')}


def episode_ranges(marks, end):
    """Human answer ranges [(kind, t0, t1)] from START/END review labels; an open start runs to the end."""
    ranges = []
    for key, (sk, ek) in EPISODE_RANGES.items():
        open_mark = None
        for m in sorted((m for m in marks if m.get('kind') in (sk, ek)), key=lambda m: m['t_s']):
            if m['kind'] == sk:
                open_mark = m
            elif open_mark:
                ranges.append((key, open_mark['t_s'], m['t_s']))
                open_mark = None
        if open_mark:
            ranges.append((key, open_mark['t_s'], end))
    return ranges


def episode_answers(ranges, marks, a, b):
    """Human answers overlapping the candidate [a, b] (a NOISE point label counts within 1 s)."""
    return sorted({key for key, x, y in ranges if x <= b and y >= a}
                  | {'NOISE' for m in marks if m.get('kind') == 'NOISE' and a - 1 <= m['t_s'] <= b + 1})


def _utc_s(text):
    try:
        return datetime.fromisoformat(str(text).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def evidence_result_times(folders, capture_ms=None):
    """result_event_id -> XIAO timestamp_utc of OBSERVATION_ACTION_RESULT events found in saved
    Evidence exports (read-only JSONL files written by evidence_client / the bridge). When a dict
    is passed as capture_ms, it is filled with result_event_id -> capture duration_ms."""
    times = {}
    for folder in folders:
        if not folder.is_dir():
            continue
        for file in sorted(folder.rglob('evidence-*.jsonl')):
            try:
                lines = file.read_text(encoding='utf-8').splitlines()
            except OSError:
                continue
            for line in lines:
                if '"OBSERVATION_ACTION_RESULT"' not in line and '"POLICY_DECISION"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if (event.get('event_type') in ('OBSERVATION_ACTION_RESULT', 'POLICY_DECISION') and event.get('origin') == 'LIVE'
                        and event.get('time_quality') == 'ESTIMATED' and event.get('timestamp_utc')):
                    times[event['event_id']] = event['timestamp_utc']
                    duration = (((event.get('result') or {}).get('capture') or {}).get('duration_ms')
                                if capture_ms is not None else None)
                    if isinstance(duration, int) and not isinstance(duration, bool) and 0 <= duration <= 60000:
                        capture_ms[event['event_id']] = duration
    return times


def arm_shots(path, manifest, xiao_times=None):
    """XIAO arm captures of a run placed on the AD3 time axis (seconds from sample 0).

    Sample 0 is the PC time bracket of the AD3 configure call (ESTIMATED). Best first:
    XIAO_RESULT_EVENT - the XIAO's own time of the capture result in a saved Evidence export
    (UTC from the bridge's clock sync at the run start, ESTIMATED); PC_FRAME_RECEIPT - PC
    receipt of the frame line (bridge 2026-11-13 and later); ESTIMATED_EVEN_SPACING - older
    runs without either, spread evenly between candidate send and episode end (can be
    several seconds off). Display only."""
    xiao_times = xiao_times or {}
    ref = (manifest.get('acquisition') or {}).get('sample_clock_reference') or {}
    if ref.get('pc_utc_before_ms') is None or ref.get('pc_utc_after_ms') is None:
        return []
    zero = (ref['pc_utc_before_ms'] + ref['pc_utc_after_ms']) / 2000
    sent, shots = {}, []
    for record in read_jsonl(path / 'delivery.jsonl'):
        seq = record.get('candidate_seq')
        if record.get('status') == 'sending':
            sent[seq] = _utc_s(record.get('received_at_pc'))
            continue
        if record.get('status') != 'acknowledged' or not record.get('observation'):
            continue
        steps = [s for s in [record['observation']] + list(record.get('follow_ups') or []) if s]
        captures = [s for s in steps if str(s.get('action', '')).startswith('CAPTURE_')]
        start, end = sent.get(seq), _utc_s(record.get('received_at_pc'))
        for index, step in enumerate(captures):
            result = step.get('result') or {}
            at, basis = _utc_s(xiao_times.get(result.get('result_event_id'))), 'XIAO_RESULT_EVENT'
            if at is None:
                at, basis = _utc_s(step.get('frame_received_at_pc')), 'PC_FRAME_RECEIPT'
            if at is None and start is not None and end is not None and end > start:
                at, basis = start + (index + 1) / len(captures) * (end - start), 'ESTIMATED_EVEN_SPACING'
            model = ((step.get('vision_ai') or {}).get('prediction') or {})
            top = model.get('top') or {}
            shots.append(dict(
                action_id=step.get('action_id'), candidate_seq=seq, step=index,
                pose=step.get('target_pose') or POSE_OF_ACTION.get(step.get('action')),
                t_s=round(at - zero, 3) if at is not None else None, time_basis=basis if at is not None else None,
                capture=result.get('capture'),
                image_url=step.get('image_url') if step.get('image_verified') is True else None,
                model_reference=dict(class_name=top.get('class_name'), score=top.get('score'),
                                     status=model.get('status')) if model else None))
    return shots


def ai_of(step):
    """The Vision V2 model output of one capture step, for display (demo page): class, raw score
    (0-100, not calibrated) and recipe. The record itself keeps presence UNKNOWN."""
    vision = step.get('vision_ai') or {}
    prediction = vision.get('prediction') or {}
    top = prediction.get('top') or {}
    if prediction.get('status') != 'MODEL_OUTPUT_UNVERIFIED' or top.get('score') is None or not top.get('class_name'):
        return None
    return dict(class_name=top['class_name'], score=top['score'], recipe=(vision.get('model') or {}).get('recipe'))


ACTUATION_FILE = 'actuation.jsonl'
SELF_NOISE_AFTER_S = 2.0  # display hint only: a candidate this soon after an arm span is marked


NO_MOVE_MAX_S = 1.75  # decision -> capture start below this: settle only, the arm did not move (see actuation_spans)


def trigger_series(path, rows, arm_spans, model):
    """Observation-trigger inputs per 250 ms window (2026-11-23): forest probability and the CH2
    1-40 Hz ratio. Runs captured with activity_trigger.py have the values used live
    (trigger_windows.csv, LIVE_RECORD); older runs (B2 trigger) are computed here from samples
    with the arm spans masked (COMPUTED_IN_REVIEW). None without a model."""
    if model is None:
        return None
    out = dict(threshold=model['threshold'], ratio_min=lowfreq_activity.RATIO_MIN)
    file = path / 'trigger_windows.csv'
    if file.is_file():
        t, fp, ratio = [], [], []
        with file.open(newline='', encoding='utf-8') as stream:
            for row in csv.DictReader(stream):
                t.append(float(row['t_center_s']))
                fp.append(float(row['forest_p']) if row['forest_p'] else None)
                ratio.append(float(row['ratio']) if row['ratio'] else None)
        return dict(out, source='LIVE_RECORD', t=t, forest_p=fp, ratio=ratio)
    t, X, usable = lowfreq_forest.features(rows, arm_spans)
    p = np.full(len(t), np.nan)
    if usable.any():
        p[usable] = lowfreq_forest.predict(model, X[usable])
    with np.errstate(all='ignore'):
        r = np.exp(X[:, 0]) if len(t) else np.zeros(0)
    r[lowfreq_activity._arm_mask(t, arm_spans)] = np.nan
    clean = lambda a: [None if not np.isfinite(v) else round(float(v), 4) for v in a]
    return dict(out, source='COMPUTED_IN_REVIEW', t=[round(float(v), 3) for v in t], forest_p=clean(p), ratio=clean(r))


def xiao_lowfreq(path):
    """What the XIAO's low-frequency forest answered during the capture (xiao_lowfreq.jsonl,
    firmware 0.15.0+; reference only). None for runs without the file."""
    file = path / 'xiao_lowfreq.jsonl'
    if not file.is_file():
        return None
    recs = read_jsonl(file)
    status = 'ENDED' if any(r.get('kind') == 'END' for r in recs) else next(
        (r['kind'] for r in recs if r.get('kind') in ('UNSUPPORTED', 'ERROR')), 'INCOMPLETE')
    windows = [r for r in recs if r.get('kind') == 'WINDOW']
    begin = next((r for r in recs if r.get('kind') == 'BEGIN'), {})
    return dict(status=status, core=begin.get('core'), threshold=begin.get('threshold'),
                windows=len(windows), arm_windows=sum(1 for w in windows if w.get('arm')),
                activity_like_spans=[dict(t0_s=r['t0_s'], t1_s=r['t1_s'], peak_p=r.get('peak_p'))
                                     for r in recs if r.get('kind') == 'SPAN'],
                errors=[r.get('error') for r in recs if r.get('kind') == 'ERROR'],
                biological_activity='UNKNOWN')


def actuation_spans(path, manifest, xiao_times=None, capture_ms=None):
    """When the XIAO's arm was moving, on the AD3 time axis (seconds from sample 0). Machine
    records, not human labels; display only (never detector input).

    PC_RECEIPT (actuation.jsonl, bridge 2026-11-14 and later): MOVE = OBSERVATION_MOVE_BEGIN to
    OBSERVATION_CAPTURE_BEGIN of the same action (ramp and settle), PARK = SERVO_PARK begin to
    done/failed. The XIAO prints no MOVE_BEGIN when the arm is already assumed at the pose.
    USB transfer delays are small but unmeasured.

    Older runs with a saved Evidence export: POLICY_DECISION to OBSERVATION_ACTION_RESULT of a
    capture step. With the capture duration from that result (capture_ms) the capture is cut off:
    XIAO_DECISION_TO_CAPTURE_START = ramp + settle (estimated, the result is persisted just after
    the capture). When the arm is already assumed at the pose the XIAO skips the move and only
    settles (0.8 s), so a step shorter than NO_MOVE_MAX_S is not an arm movement and is left out.
    Measured on all saved runs (2026-11-14): FRONT (already parked) 0.90-1.37 s, TOP/BACK
    (1.3-1.4 s ramp + settle) 2.19-2.70 s. Without capture_ms: XIAO_DECISION_TO_RESULT, which
    also contains the capture. Parks are unknown there."""
    ref = (manifest.get('acquisition') or {}).get('sample_clock_reference') or {}
    if ref.get('pc_utc_before_ms') is None or ref.get('pc_utc_after_ms') is None:
        return []
    zero = (ref['pc_utc_before_ms'] + ref['pc_utc_after_ms']) / 2000
    rel = lambda text: (lambda v: None if v is None else round(v - zero, 3))(_utc_s(text))
    spans = []
    lines = read_jsonl(path / ACTUATION_FILE) if (path / ACTUATION_FILE).is_file() else []
    if lines:
        moves, park = {}, None
        for a in lines:
            t = rel(a.get('received_at_pc'))
            if t is None:
                continue
            kind = a.get('kind')
            if kind == 'MOVE_BEGIN':
                moves[a.get('action_id')] = (t, a)
            elif kind == 'CAPTURE_BEGIN' and a.get('action_id') in moves:
                t0, begin = moves.pop(a.get('action_id'))
                spans.append(dict(kind='MOVE', pose=begin.get('pose'), action_id=begin.get('action_id'),
                                  t0_s=t0, t1_s=t, time_basis='PC_RECEIPT', candidate_seq=a.get('candidate_seq')))
            elif kind == 'PARK_BEGIN':
                park = (t, a)
            elif kind in ('PARK_DONE', 'PARK_FAILED'):
                t0 = park[0] if park else None
                elapsed = a.get('elapsed_ms')
                if t0 is None and elapsed and str(elapsed).isdigit():
                    t0 = round(t - int(elapsed) / 1000, 3)  # begin not seen: use the XIAO's own duration
                if t0 is not None:
                    spans.append(dict(kind='PARK', pose='FRONT', t0_s=t0, t1_s=t, status=kind[5:],
                                      time_basis='PC_RECEIPT', late_read_possible=bool(a.get('late_read_possible')),
                                      candidate_seq=a.get('candidate_seq')))
                park = None
        for t0, begin in moves.values():  # capture line never seen: show the start only
            spans.append(dict(kind='MOVE', pose=begin.get('pose'), action_id=begin.get('action_id'), t0_s=t0,
                              t1_s=t0, time_basis='PC_RECEIPT', end_unknown=True))
        return sorted(spans, key=lambda s: s['t0_s'])
    xiao_times, capture_ms = xiao_times or {}, capture_ms or {}
    for record in read_jsonl(path / 'delivery.jsonl') if (path / 'delivery.jsonl').is_file() else []:
        if record.get('status') != 'acknowledged':
            continue
        for step in [record.get('observation')] + list(record.get('follow_ups') or []):
            if not step or not str(step.get('action', '')).startswith('CAPTURE_') or step.get('action') == 'CAPTURE_FIXED':
                continue
            result_id = (step.get('result') or {}).get('result_event_id')
            t0 = rel(xiao_times.get(step.get('decision_event_id')))
            t1 = rel(xiao_times.get(result_id))
            if t0 is None or t1 is None or t1 < t0:
                continue
            common = dict(pose=step.get('target_pose'), action_id=step.get('action_id'), t0_s=t0,
                          candidate_seq=record.get('candidate_seq'))
            if result_id in capture_ms:
                start = round(t1 - capture_ms[result_id] / 1000, 3)
                if start - t0 < NO_MOVE_MAX_S:
                    continue  # settle only: the arm was already at the pose
                spans.append(dict(common, kind='MOVE', t1_s=start, time_basis='XIAO_DECISION_TO_CAPTURE_START'))
            else:
                spans.append(dict(common, kind='MOVE_AND_CAPTURE', t1_s=t1, time_basis='XIAO_DECISION_TO_RESULT'))
    return sorted(spans, key=lambda s: s['t0_s'])


def actuation_near(spans, start, end):
    """'DURING' if the candidate overlaps an arm span, 'AFTER' (seconds) if it starts within
    SELF_NOISE_AFTER_S after one ends, else None. A display hint, not a classification."""
    for s in spans:
        if start <= s['t1_s'] and end >= s['t0_s']:
            return dict(relation='DURING', kind=s['kind'], pose=s.get('pose'))
    after = sorted((start - s['t1_s'], s['kind']) for s in spans if 0 < start - s['t1_s'] <= SELF_NOISE_AFTER_S)
    if after:
        return dict(relation='AFTER', seconds=round(after[0][0], 2), kind=after[0][1])
    return None


def offset_estimate(manifest):
    """Rough video time of AD3 sample 0 from PC receipt times (a sync tap refines it)."""
    video = manifest.get('video') or {}
    if video.get('recording_s_at_first_block') is None:
        return None
    return round(video['recording_s_at_first_block'] - (video.get('first_block_samples') or 0) / FS, 4)


def binned(t, values, end_s, bins, reduce=np.nanmax):
    """Per-bin reduction over [0, end_s) so long runs stay a fixed-size plot."""
    t = np.asarray(t, float)
    values = np.asarray(values, float)
    if not len(t):
        return [], []
    if len(t) <= bins:
        return [round(float(x), 4) for x in t], [finite(v) for v in values]
    edges = np.linspace(0, end_s, bins + 1)
    index = np.clip(np.searchsorted(edges, t, side='right') - 1, 0, bins - 1)
    out_t, out_v = [], []
    order = np.argsort(index, kind='stable')
    index, values = index[order], values[order]
    starts = np.flatnonzero(np.r_[True, np.diff(index) > 0])
    for a, b in zip(starts, np.r_[starts[1:], len(index)]):
        chunk = values[a:b]
        out_t.append(round(float((edges[index[a]] + edges[index[a] + 1]) / 2), 4))
        out_v.append(finite(reduce(chunk)) if np.isfinite(chunk).any() else None)
    return out_t, out_v


class ReviewStore:
    def __init__(self, output, busy_run=lambda: None):
        self.output = Path(output)
        self.busy_run = busy_run  # run directory name currently being written, or None
        self.lock = threading.Lock()
        self.remux_lock = threading.Lock()
        self.cache = {}
        # Low-frequency activity-likeness rows (lowfreq_activity.py), cached outside the run folders.
        self.low_dir = self.output.parent / 'review_cache' / 'lowfreq'
        # small forest on the same rows (lowfreq_forest.py; PC reference, MCU-sized); None when absent
        self.forest = lowfreq_forest.load_model()
        self.low_pending, self.low_thread = set(), None

    def _low_rows(self, path, compute=True):
        """Cached 1-40 Hz rows of a run. compute=False never reads the waveform here: a missing
        cache is filled by one background thread and None is returned (shown as pending)."""
        if compute:
            return lowfreq_activity.cached_rows(path / 'samples.f64', self.low_dir)
        rows = lowfreq_activity.cached_only(path / 'samples.f64', self.low_dir)
        if rows is None:
            with self.lock:
                self.low_pending.add(path)
                if not (self.low_thread and self.low_thread.is_alive()):
                    self.low_thread = threading.Thread(target=self._low_worker, daemon=True)
                    self.low_thread.start()
        return rows

    def _low_worker(self):
        while True:
            with self.lock:
                if not self.low_pending:
                    return
                path = min(self.low_pending)
                self.low_pending.discard(path)
            try:
                lowfreq_activity.cached_rows(path / 'samples.f64', self.low_dir)
            except Exception:  # a broken run must not stop the others; it stays pending
                pass

    def _low_judge(self, path, arm, compute=True):
        rows = self._low_rows(path, compute)
        return None if rows is None else lowfreq_activity.judge(rows, [(s['t0_s'], s['t1_s']) for s in arm])

    def xiao_times(self):
        """Capture-result times from Evidence exports under captures/ (cached by file stamps)."""
        root = self.output.parent
        folders = [root / 'evidence', root / 'flash_backup']
        stamp = tuple(sorted((str(p), p.stat().st_mtime_ns) for f in folders if f.is_dir()
                             for p in f.rglob('evidence-*.jsonl')))
        cached = getattr(self, 'xiao_cache', None)
        if not cached or cached[0] != stamp:
            capture_ms = {}
            self.xiao_cache = cached = (stamp, evidence_result_times(folders, capture_ms), capture_ms)
        return cached[1]

    def xiao_capture_ms(self):
        """result_event_id -> capture duration_ms from the same Evidence exports (same cache)."""
        self.xiao_times()
        return self.xiao_cache[2]

    def directory(self, run, allow_trashed=False):
        if not isinstance(run, str) or not RUN_RE.fullmatch(run):
            raise ValueError('Invalid run')
        path = self.output / run
        if not (path / 'manifest.json').is_file() or not (path / 'samples.f64').is_file():
            raise ValueError('Run not found')
        if self.busy_run() == run:
            raise BlockingIOError('取得中の記録は振り返りできません。取得の終了後に開いてください')
        if not allow_trashed and trashed(path):
            raise ValueError('この記録は削除済みです（観察エピソードの「削除した記録」から元に戻せます）')
        return path

    def set_trashed(self, run, deleted, data):
        """Delete a run into the trash (deleted=True) or restore it; appends to trash.jsonl only."""
        path = self.directory(run, allow_trashed=True)
        reason = data.get('reason') or ''
        if not isinstance(reason, str) or len(reason) > MAX_NOTE:
            raise ValueError('Invalid reason')
        with self.lock:
            if bool(trashed(path)) == bool(deleted):
                raise ValueError('この記録はすでに削除済みです' if deleted else 'この記録は削除されていません')
            record = dict(schema='bll.ad3_run_trash.v1', run=run, deleted=bool(deleted),
                          created_at_pc=utc_now(), reason=reason.strip())
            with (path / TRASH_FILE).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        return record

    def set_review_status(self, run, data):
        """Mark the annotation of a run DONE, or reopen it (IN_PROGRESS); appends to STATUS_FILE only."""
        path = self.directory(run)
        status = data.get('status')
        if status not in ('DONE', 'IN_PROGRESS'):
            raise ValueError('Invalid status')
        with self.lock:
            if (review_status(path)['status'] == 'DONE') == (status == 'DONE'):
                raise ValueError('すでに完了になっています' if status == 'DONE' else 'この記録は完了になっていません')
            record = dict(schema='bll.ad3_review_status.v1', run=run, status=status,
                          created_at_pc=utc_now(), source='HUMAN_REVIEW')
            with (path / STATUS_FILE).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
        return dict(record=record, review_status=review_status(path))

    def trash(self):
        """Deleted runs, newest deletion first, with what is needed to recognise them."""
        rows = []
        if not self.output.is_dir():
            return rows
        for path in self.output.iterdir():
            if not RUN_RE.fullmatch(path.name):
                continue
            record = trashed(path)
            if not record:
                continue
            try:
                manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
                size = (path / 'samples.f64').stat().st_size
            except (OSError, ValueError):
                continue
            request = manifest.get('request') or {}
            rows.append(dict(run=path.name, origin=manifest.get('origin'), started_at_pc=manifest.get('started_at_pc'),
                             duration_s=round(size / 16 / FS, 2), case_id=request.get('case_id') or '',
                             note=request.get('note') or '', deleted_at_pc=record.get('created_at_pc'),
                             reason=record.get('reason') or ''))
        rows.sort(key=lambda r: r['deleted_at_pc'] or '', reverse=True)
        return rows

    @staticmethod
    def samples(path):
        size = (path / 'samples.f64').stat().st_size
        count = size // 16
        if not count:
            raise ValueError('Run has no samples')
        return np.memmap(path / 'samples.f64', dtype='<f8', mode='r', shape=(count, 2))

    def runs(self):
        busy, rows = self.busy_run(), []
        if not self.output.is_dir():
            return rows
        for path in self.output.iterdir():
            if not RUN_RE.fullmatch(path.name) or path.name == busy or trashed(path):
                continue
            try:
                manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
                size = (path / 'samples.f64').stat().st_size
            except (OSError, ValueError):
                continue
            request = manifest.get('request') or {}
            rows.append(dict(
                run=path.name, origin=manifest.get('origin'), started_at_pc=manifest.get('started_at_pc'),
                duration_s=round(size / 16 / FS, 2), complete=bool(manifest.get('complete')),
                note=request.get('note') or '', case_id=request.get('case_id') or '',
                candidates=len(read_jsonl(path / 'candidates.jsonl')) if (path / 'candidates.jsonl').is_file() else 0,
                live_marks=len(read_jsonl(path / 'annotations.jsonl')),
                review_marks=len(marks_and_offset(read_jsonl(path / REVIEW_FILE))[0]),
                review_status=review_status(path)['status'],
                video=(manifest.get('video') or {}).get('status') == 'RECORDED'))
        rows.sort(key=lambda r: r['started_at_pc'] or '', reverse=True)
        return rows

    def low_band(self, path, x_count):
        """1-40 Hz |peak| per channel for display; zero-phase because this is offline only."""
        raw = self.samples(path)
        n = len(raw) // LOW_DECIMATE * LOW_DECIMATE
        parts = []
        step = LOW_DECIMATE * 20000
        for start in range(0, n, step):
            block = np.asarray(raw[start:min(n, start + step)])
            parts.append(block.reshape(-1, LOW_DECIMATE, 2).mean(axis=1))
        x = np.concatenate(parts) if parts else np.zeros((0, 2))
        fs = FS / LOW_DECIMATE
        if len(x) < 64:
            return [], [], []
        sos = signal.butter(4, LOW_BAND_HZ, fs=fs, btype='bandpass', output='sos')
        y = np.abs(signal.sosfiltfilt(sos, x - x.mean(axis=0), axis=0)) * 1e3
        t = (np.arange(len(y)) + .5) / fs
        end = len(raw) / FS
        tt, ch1 = binned(t, y[:, 0], end, x_count)
        _, ch2 = binned(t, y[:, 1], end, x_count)
        return tt, ch1, ch2

    def overview(self, run):
        path = self.directory(run)
        key = (run, (path / 'samples.f64').stat().st_mtime_ns, (path / 'features.csv').stat().st_mtime_ns
               if (path / 'features.csv').is_file() else 0)
        with self.lock:
            cached = self.cache.get(run)
        if not cached or cached[0] != key:
            end = len(self.samples(path)) / FS
            t, ch1, ch2, score = [], [], [], []
            if (path / 'features.csv').is_file():
                with (path / 'features.csv').open(encoding='utf-8', newline='') as stream:
                    for row in csv.DictReader(stream):
                        t.append(float(row['t_center_s']))
                        ch2.append(float(row['rms_V']) * 1e3)
                        ch1.append(float(row['ch1_rms_V']) * 1e3 if row.get('ch1_rms_V') else math.nan)
                        score.append(float(row['score']) if row.get('score') else math.nan)
            bt, b1 = binned(t, ch1, end, OVERVIEW_BINS)
            _, b2 = binned(t, ch2, end, OVERVIEW_BINS)
            _, bs = binned(t, score, end, OVERVIEW_BINS)
            lt, l1, l2 = self.low_band(path, OVERVIEW_BINS)
            data = dict(duration_s=round(end, 5), band=dict(t=bt, ch1=b1, ch2=b2, score=bs),
                        low=dict(t=lt, ch1=l1, ch2=l2),
                        # causal 1-40 Hz rows for the activity-likeness reference (not sent to the client)
                        _lowfreq=self._low_rows(path))
            with self.lock:
                self.cache = {run: (key, data)}  # one run at a time keeps memory bounded
            cached = (key, data)
        manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
        # candidates.jsonl = the observation triggers sent to the MCU. Until 2026-11-22 they were B2;
        # on 2026-11-23 the 1-40 Hz activity starts (detector LOWFREQ_FOREST_RATIO), B2 saved for
        # comparison in b2_candidates.jsonl; from 2026-11-24 either (B2-started ones: B2_guarded_mad).
        candidates = [dict(t_start_s=c.get('t_start_s'), t_end_s=c.get('t_end_s'), peak_score=c.get('peak_score'),
                           detector=c.get('detector', 'B2_guarded_mad'), triggered_by=c.get('triggered_by'))
                      for c in read_jsonl(path / 'candidates.jsonl')]
        b2_path = path / 'b2_candidates.jsonl'
        b2 = ([dict(t_start_s=c.get('t_start_s'), t_end_s=c.get('t_end_s'), peak_score=c.get('peak_score'))
               for c in read_jsonl(b2_path) if c.get('quality') == 'DEVELOPMENT_CANDIDATE']
              if b2_path.is_file() else [c for c in candidates if c['detector'] == 'B2_guarded_mad'])
        marks, saved_offset = marks_and_offset(read_jsonl(path / REVIEW_FILE))
        arm = actuation_spans(path, manifest, self.xiao_times(), self.xiao_capture_ms())
        # Low-frequency activity-likeness: PC-only reference, never detector input (lowfreq_activity.py)
        low_activity = lowfreq_activity.judge(cached[1]['_lowfreq'], [(s['t0_s'], s['t1_s']) for s in arm])
        # the same, by the small forest (display only, provisional; in_training marks in-sample runs)
        forest_activity = lowfreq_forest.judge(cached[1]['_lowfreq'], [(s['t0_s'], s['t1_s']) for s in arm], self.forest, run)
        xiao_forest = xiao_lowfreq(path)
        # shadow period: where each 1-40 Hz source would have raised a candidate (B2 unchanged)
        shadow = shadow_trigger.compare(
            cached[1]['duration_s'], b2,
            dict(LOWFREQ_RATIO_3=(low_activity['activity_like_spans'], 'peak_x'),
                 FOREST_PC=None if forest_activity.get('status') == 'NO_MODEL' else (forest_activity['activity_like_spans'], 'peak_p'),
                 FOREST_XIAO=(xiao_forest['activity_like_spans'], 'peak_p') if xiao_forest and xiao_forest['status'] == 'ENDED' else None),
            [(s['t0_s'], s['t1_s']) for s in arm])
        shadow['forest_in_training'] = bool(forest_activity.get('in_training'))
        lt, l1, l2 = cached[1]['_lowfreq']
        # same 1-40 Hz RMS (elliptic, 250 ms) as the activity-likeness, in mV; display only
        lowfreq = dict(t=[round(float(x), 3) for x in lt], ch1=[round(float(x) * 1e3, 5) for x in l1],
                       ch2=[round(float(x) * 1e3, 5) for x in l2], window_s=lowfreq_activity.WINDOW_S)
        return dict({k: v for k, v in cached[1].items() if k != '_lowfreq'}, run=run, origin=manifest.get('origin'), started_at_pc=manifest.get('started_at_pc'),
                    note=(manifest.get('request') or {}).get('note') or '', candidates=candidates, b2_candidates=b2,
                    trigger_series=trigger_series(path, cached[1]['_lowfreq'], [(s['t0_s'], s['t1_s']) for s in arm], self.forest),
                    # the run's trigger (2026-11-24: LOWFREQ_FOREST_RATIO_OR_B2, whose candidates name
                    # the source that started each one), else the first candidate's detector
                    candidate_detector=(manifest.get('observation_trigger') or {}).get('detector') or
                    (candidates[0]['detector'] if candidates else 'B2_guarded_mad'),
                    video=self.video_info(path, manifest, saved_offset),
                    live_marks=read_jsonl(path / 'annotations.jsonl'),
                    review_marks=marks, review_status=review_status(path),
                    shots=arm_shots(path, manifest, self.xiao_times()),
                    actuation=arm, low_activity=low_activity, forest_activity=forest_activity,
                    xiao_forest_activity=xiao_forest, shadow_triggers=shadow, lowfreq=lowfreq,
                    biological_activity='UNKNOWN')

    def wave(self, run, t0, t1):
        path = self.directory(run)
        raw = self.samples(path)
        t0, t1 = float(t0), float(t1)
        if not (math.isfinite(t0) and math.isfinite(t1)) or t1 <= t0:
            raise ValueError('Invalid time range')
        if t1 - t0 > MAX_WAVE_S:
            raise ValueError(f'Time range is limited to {MAX_WAVE_S:g} s')
        a, b = max(0, int(t0 * FS)), min(len(raw), int(math.ceil(t1 * FS)))
        if b - a < 2:
            raise ValueError('Time range is outside the recording')
        x = np.asarray(raw[a:b])
        x = (x - np.median(x, axis=0)) * 1e3
        bins = min(WAVE_POINTS, b - a)
        edges = np.linspace(0, b - a, bins + 1).astype(int)
        out = dict(t0=a / FS, t1=b / FS, t=[], ch1_min=[], ch1_max=[], ch2_min=[], ch2_max=[])
        for i in range(bins):
            part = x[edges[i]:max(edges[i + 1], edges[i] + 1)]
            out['t'].append(round((a + (edges[i] + edges[i + 1]) / 2) / FS, 6))
            out['ch1_min'].append(round(float(part[:, 0].min()), 5))
            out['ch1_max'].append(round(float(part[:, 0].max()), 5))
            out['ch2_min'].append(round(float(part[:, 1].min()), 5))
            out['ch2_max'].append(round(float(part[:, 1].max()), 5))
        return out

    def annotate(self, run, data):
        path = self.directory(run)
        kind = data.get('kind')
        note = data.get('note') or ''
        if not isinstance(note, str) or len(note) > MAX_NOTE:
            raise ValueError('Invalid note')
        with self.lock:
            records = read_jsonl(path / REVIEW_FILE)
            seq = max((r.get('seq', 0) for r in records), default=0) + 1
            record = dict(schema='bll.ad3_review_annotation.v1', run=run, seq=seq, kind=kind,
                          created_at_pc=utc_now(), source='HUMAN_REVIEW', note=note)
            if kind == VIDEO_OFFSET:
                offset = data.get('offset_s')
                if type(offset) not in (int, float) or not math.isfinite(offset) or abs(offset) > MAX_OFFSET_S:
                    raise ValueError('Invalid video offset')
                record.update(offset_s=round(float(offset), 4), meaning='video_time_s = ad3_time_s + offset_s')
            elif kind == 'RETRACT':
                # target_seq: one label (a moved label goes back to where it was).
                # target_seqs + with_history: delete labels outright (e.g. both ends of a range at once).
                targets = data.get('target_seqs', [data.get('target_seq')])
                active = {r.get('seq') for r in active_review(records)}
                if (not isinstance(targets, list) or not 1 <= len(targets) <= 2 or len(set(targets)) != len(targets)
                        or any(type(t) is not int or t not in active for t in targets)):
                    raise ValueError('Unknown label')
                record['target_seq'] = targets[0]
                if len(targets) > 1 or 'target_seqs' in data:
                    record['target_seqs'] = targets
                if data.get('with_history') is True:
                    record['with_history'] = True
            elif kind in REVIEW_KINDS:
                t_s = data.get('t_s')
                if type(t_s) not in (int, float) or not math.isfinite(t_s):
                    raise ValueError('Invalid time')
                count = len(self.samples(path))
                sample = int(round(t_s * FS))
                if not 0 <= sample < count:
                    raise ValueError('Time is outside the recording')
                record.update(sample=sample, t_s=sample / FS, time_basis='SAMPLE_INDEX_CHOSEN_IN_REVIEW')
                blind = data.get('blind', False)
                if type(blind) is not bool:
                    raise ValueError('Invalid blind flag')
                if blind:  # chosen with the detector / model bands hidden (2026-11-21)
                    record['blind'] = True
                replaces = data.get('replaces_seq')
                if replaces is not None:
                    # Moving a label or editing its note: the old record stays in the file; this one
                    # supersedes it. Only what changed is noted (moved_from_t_s, note_changed_from).
                    old = next((r for r in active_review(records) if r.get('seq') == replaces), None)
                    if type(replaces) is not int or old is None or old.get('kind') != kind:
                        raise ValueError('Unknown label to move')
                    record['replaces_seq'] = replaces
                    if old.get('sample') != sample:
                        record['moved_from_t_s'] = old.get('t_s')
                    if (old.get('note') or '') != note:
                        record['note_changed_from'] = old.get('note') or ''
            else:
                raise ValueError('Invalid label kind')
            with (path / REVIEW_FILE).open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            marks, offset = marks_and_offset(records + [record])
            return dict(record=record, review_marks=marks, video_offset=offset, review_status=review_status(path))

    # ---- video (PC-only review aid; never detector input) ----
    def video_info(self, path, manifest, saved_offset):
        video = manifest.get('video')
        if not video:
            return None
        info = dict(status=video.get('status'), reason=video.get('reason') or video.get('move_error'),
                    available=False, offset_estimate_s=offset_estimate(manifest))
        file = video_path(path, manifest)
        if file is None:
            return info
        meta = self._video_meta(file)
        offset = saved_offset['offset_s'] if saved_offset else info['offset_estimate_s']
        source = 'SAVED_REVIEW' if saved_offset else ('ESTIMATED_PC_RECEIPT' if offset is not None else 'UNKNOWN_ZERO')
        info.update(available=meta['frames'] > 0, file=str(file), in_run_folder=file.parent == path, **meta,
                    offset_s=offset if offset is not None else 0.0, offset_source=source,
                    audio=self._audio_available())
        try:
            box = self._content_box(file, meta) if meta['frames'] > 0 else None
        except (OSError, ValueError):
            box = None
        w, h = meta['width'] or 1, meta['height'] or 1
        # Camera area as fractions of the frame, so the browser player can crop OBS margins too.
        info['content_box'] = [box[0] / w, box[1] / h, box[2] / w, box[3] / h] if box else None
        return info

    def playable_video(self, run):
        """MP4 the browser can play and seek: the recording itself, or a stream-copied
        (not re-encoded) MP4 of an OBS .mkv kept in captures/review_cache. Display only."""
        path = self.directory(run)
        manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
        file = video_path(path, manifest)
        if file is None:
            raise LookupError('この記録に動画はありません')
        if file.suffix.lower() == '.mp4':
            return file
        stat = file.stat()
        cache = self.output.parent / 'review_cache'
        target = cache / f'{run}-{stat.st_size}-{stat.st_mtime_ns}.mp4'
        with self.remux_lock:
            if target.is_file():
                return target
            try:
                import av
            except ImportError as exc:
                raise NotImplementedError('動画の再生には PyAV（av）が必要です') from exc
            cache.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix('.partial')
            with av.open(str(file)) as source, \
                    av.open(str(temp), 'w', format='mp4', options={'movflags': 'faststart'}) as out:
                streams = {}
                for stream in source.streams:
                    if stream.type in ('video', 'audio') and not any(s.type == stream.type for s in streams.values()):
                        streams[stream.index] = out.add_stream_from_template(stream)
                if not any(s.type == 'video' for s in streams.values()):
                    raise LookupError('動画トラックがありません')
                for packet in source.demux([s for s in source.streams if s.index in streams]):
                    if packet.dts is None:
                        continue
                    packet.stream = streams[packet.stream.index]
                    out.mux(packet)
            temp.replace(target)
            old = sorted((p for p in cache.glob('ad3-*.mp4') if p != target), key=lambda p: p.stat().st_mtime)
            for stale in [p for p in old if p.name.startswith(run + '-')] + old[:-(REMUX_KEEP - 1) or None]:
                try:
                    stale.unlink()
                except OSError:
                    pass
            return target

    @staticmethod
    def _audio_available():
        try:
            import av  # noqa: F401
            return dict(available=True)
        except ImportError:
            return dict(available=False, reason='音声の表示には PyAV（av）が必要です')

    def _capture(self, file):
        """One cached OpenCV capture (and its lock) for the most recently used video."""
        import cv2
        with self.lock:
            cached = getattr(self, 'video_cache', None)
            if cached and cached[0] == file:
                return cached[1], cached[2]
            if cached:
                cached[1].release()
            capture = cv2.VideoCapture(str(file))
            if not capture.isOpened():
                raise ValueError('動画を開けません')
            self.video_cache = (file, capture, threading.Lock())
            return capture, self.video_cache[2]

    def _video_meta(self, file):
        import cv2
        capture, lock = self._capture(file)
        with lock:
            fps = capture.get(cv2.CAP_PROP_FPS) or 0
            frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            width, height = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)), int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        return dict(fps=round(fps, 3), frames=frames, duration_s=round(frames / fps, 3) if fps else 0,
                    width=width, height=height)

    def frame(self, run, video_t, width=480):
        """JPEG of the frame at video time video_t (seconds from the start of the recording)."""
        import cv2
        path = self.directory(run)
        manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
        file = video_path(path, manifest)
        if file is None:
            raise ValueError('この記録に動画はありません')
        video_t, width = float(video_t), int(width)
        if not math.isfinite(video_t) or not 120 <= width <= 1920:
            raise ValueError('Invalid frame request')
        meta = self._video_meta(file)
        if not 0 <= video_t <= meta['duration_s']:
            raise LookupError('動画の範囲外です')
        capture, lock = self._capture(file)
        with lock:
            capture.set(cv2.CAP_PROP_POS_MSEC, video_t * 1000)
            ok, image = capture.read()
        if not ok:
            raise LookupError('この時刻のフレームを読めません')
        box = self._content_box(file, meta)
        if box:
            x0, y0, x1, y1 = box
            image = image[y0:y1, x0:x1]
        if image.shape[1] != width:
            image = cv2.resize(image, (width, round(image.shape[0] * width / image.shape[1])),
                               interpolation=cv2.INTER_AREA)
        ok, jpeg = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return jpeg.tobytes()

    def _content_box(self, file, meta):
        """Camera area inside the OBS canvas, or None when the picture fills the frame.

        A camera source smaller than the canvas leaves exact-black margins (2026-11-13:
        640x480 camera at the top-left of a 1920x1080 canvas). The box is the union of
        non-black pixels over a few frames, so a dark scene inside the camera area is kept.
        Display only; the recording is not changed."""
        import cv2
        import numpy as np
        key = (file, file.stat().st_mtime_ns)
        cached = getattr(self, 'box_cache', None)
        if cached and cached[0] == key:
            return cached[1]
        capture, lock = self._capture(file)
        mask = None
        with lock:
            for k in range(1, 6):
                capture.set(cv2.CAP_PROP_POS_MSEC, meta['duration_s'] * 1000 * k / 6)
                ok, image = capture.read()
                if ok:
                    lit = image.max(axis=2) > 12
                    mask = lit if mask is None else mask | lit
        box = None
        if mask is not None and mask.any():
            ys, xs = np.nonzero(mask)
            h, w = mask.shape
            x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
            # Small dark borders inside the camera picture are not OBS margins.
            x0 = 0 if x0 < w * .02 else x0
            y0 = 0 if y0 < h * .02 else y0
            x1 = w if x1 > w * .98 else x1
            y1 = h if y1 > h * .98 else y1
            if (x1 - x0) * (y1 - y0) < .9 * w * h and x1 - x0 >= 64 and y1 - y0 >= 48:
                box = (x0, y0, x1, y1)
        self.box_cache = (key, box)
        return box

    def _audio(self, file):
        """Mono float32 audio of the recording and its start in video time (cached for one file)."""
        key = (file, file.stat().st_mtime_ns)
        cached = getattr(self, 'audio_cache', None)
        if cached and cached[0] == key:
            return cached[1]
        try:
            import av
        except ImportError as exc:
            raise NotImplementedError('音声の表示には PyAV（av）が必要です') from exc
        parts, rate, start = [], None, None
        with av.open(str(file)) as container:
            if not container.streams.audio:
                raise LookupError('動画に音声トラックがありません')
            stream = container.streams.audio[0]
            for frame in container.decode(stream):
                if start is None:
                    start = float(frame.time or 0.0)
                rate = frame.sample_rate
                raw = frame.to_ndarray()
                channels = len(frame.layout.channels)
                data = raw if frame.format.is_planar else raw.reshape(-1, channels).T
                data = data.astype(np.float32)
                if np.issubdtype(raw.dtype, np.integer):
                    data /= float(np.iinfo(raw.dtype).max)
                parts.append(data.mean(axis=0))
        if not parts:
            raise LookupError('音声を読めません')
        audio = (np.concatenate(parts), int(rate), start or 0.0)
        self.audio_cache = (key, audio)
        return audio

    def candidate_context(self, run):
        """CH2 / CH1 / sound rises and the XIAO's answer for every B2 candidate (reference only)."""
        import candidate_context as cc
        path = self.directory(run)
        manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
        rows = []
        if (path / 'features.csv').is_file():
            with (path / 'features.csv').open(encoding='utf-8', newline='') as stream:
                rows = list(csv.DictReader(stream))
        t, ch2, ch1 = cc.rows_to_arrays(rows) if rows else (np.zeros(0),) * 3
        candidates = read_jsonl(path / 'candidates.jsonl')
        spans = [(c['t_start_s'], c['t_end_s']) for c in candidates]
        xiao = {}
        for record in read_jsonl(path / 'delivery.jsonl') if (path / 'delivery.jsonl').is_file() else []:
            step = record.get('observation')
            if record.get('status') == 'acknowledged' and step:
                xiao[record.get('candidate_seq')] = dict(
                    action=step.get('action'), reason_codes=[r for r in step.get('reason_codes') or []
                                                             if r != 'PASSIVE_CANDIDATE_NEW'])
        sound, sound_note = None, None
        file = video_path(path, manifest)
        if file is not None:
            try:
                audio, rate, start = self._audio(file)
                sos = signal.butter(4, (200, min(4000, rate / 2 * .9)), fs=rate, btype='bandpass', output='sos')
                filtered = signal.sosfiltfilt(sos, audio).astype(np.float32)
                offset = self.video_info(path, manifest, marks_and_offset(read_jsonl(path / REVIEW_FILE))[1])
                sound = (filtered, rate, start, cc.audio_floor(filtered, rate), offset.get('offset_s') or 0.0,
                         offset.get('offset_source'))
            except (NotImplementedError, LookupError) as exc:
                sound_note = str(exc)
        # Disturbance-likeness (reference): the value saved during acquisition when there is one,
        # otherwise the same causal computation replayed over samples.f64 (about 1 s per 90 s).
        saved = {}
        if (path / 'candidate_context.jsonl').is_file():
            for record in read_jsonl(path / 'candidate_context.jsonl'):
                if record.get('disturbance'):
                    saved[record.get('candidate_seq')] = dict(record['disturbance'], source='LIVE_RECORD')
        bands = None
        if candidates and len(t) and any(c.get('candidate_seq') not in saved for c in candidates):
            bands = self._disturbance_bands(path)
        items = []
        for c in candidates:
            a, b = c['t_start_s'], c['t_end_s']
            others = [s for s in spans if s != (a, b)]
            dz = saved.get(c.get('candidate_seq'))
            if dz is None and bands is not None:
                dz = dict(disturbance.judge(bands, (t, ch2), a, b, others), source='COMPUTED_IN_REVIEW')
            item = dict(candidate_seq=c.get('candidate_seq'), t_start_s=a, t_end_s=b,
                        peak_score=c.get('peak_score'),
                        **cc.band_ratios(t, ch2, ch1, a, b, others),
                        disturbance=dz, sound_x=None, xiao=xiao.get(c.get('candidate_seq')))
            if sound:
                filtered, rate, start, floor, offset_s, _ = sound
                item['sound_x'] = cc.audio_ratio(filtered, rate, a + offset_s - start, b + offset_s - start, floor)
            items.append(item)
        return dict(schema=cc.SCHEMA, run=run, note=cc.NOTE, band_hz=[300, 1500], baseline_s=cc.BASELINE_S,
                    disturbance_note=disturbance.NOTE,
                    sound=dict(available=bool(sound), band_hz=[200, 4000], note=sound_note,
                               offset_source=sound[5] if sound else None),
                    candidates=items, biological_activity='UNKNOWN')

    def _disturbance_bands(self, path):
        key = (str(path), (path / 'samples.f64').stat().st_mtime_ns)
        cached = getattr(self, 'band_cache', None)
        if not cached or cached[0] != key:
            cached = self.band_cache = (key, disturbance.from_samples(path / 'samples.f64'))
        return cached[1]

    def episodes(self, run):
        """One card per B2 candidate for the user-facing episode view (docs/ui_design_episode_view_20261114.md):
        felt (candidate, CH2/CH1 rises), decided and acted (XIAO steps), checked (verified images), the
        human answer (overlapping review labels) and a video span. Read-only; no sound ratio (fast, and
        the user view does not handle voices). Biological activity stays UNKNOWN."""
        import candidate_context as cc
        path = self.directory(run)
        manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
        end = len(self.samples(path)) / FS
        rows = []
        if (path / 'features.csv').is_file():
            with (path / 'features.csv').open(encoding='utf-8', newline='') as stream:
                rows = list(csv.DictReader(stream))
        t, ch2, ch1 = cc.rows_to_arrays(rows) if rows else (np.zeros(0),) * 3
        candidates = read_jsonl(path / 'candidates.jsonl') if (path / 'candidates.jsonl').is_file() else []
        spans = [(c['t_start_s'], c['t_end_s']) for c in candidates]
        delivery = {}
        for record in read_jsonl(path / 'delivery.jsonl') if (path / 'delivery.jsonl').is_file() else []:
            if record.get('status') != 'sending':
                delivery[record.get('candidate_seq')] = record
        shots = {s['action_id']: s for s in arm_shots(path, manifest, self.xiao_times()) if s.get('action_id')}
        arm = actuation_spans(path, manifest, self.xiao_times(), self.xiao_capture_ms())
        marks, saved_offset = marks_and_offset(read_jsonl(path / REVIEW_FILE))
        video = self.video_info(path, manifest, saved_offset) or {}
        offset = video.get('offset_s') if video.get('available') else None
        ranges = episode_ranges(marks, end)
        low = self._low_judge(path, arm)
        items, counts = [], dict(captures=0, waits=0, answered=0)
        for c in candidates:
            a, b = c['t_start_s'], c['t_end_s']
            record = delivery.get(c.get('candidate_seq')) or {}
            steps = []
            for step in [record.get('observation')] + list(record.get('follow_ups') or []):
                if not step or step.get('action') in (None, 'NONE'):  # NONE closes the episode
                    continue
                result = step.get('result') or {}
                shot = shots.get(step.get('action_id')) or {}
                steps.append(dict(action=step.get('action'), action_id=step.get('action_id'), reason_codes=step.get('reason_codes') or [],
                                  pose=step.get('target_pose') or POSE_OF_ACTION.get(step.get('action')),
                                  status=step.get('status'), follow_up=bool(step.get('follow_up')),
                                  capture=result.get('capture'), capture_reason=result.get('reason'),
                                  image_url=step.get('image_url') if step.get('image_verified') is True else None,
                                  t_s=shot.get('t_s'), time_basis=shot.get('time_basis'),
                                  ai=ai_of(step) if result.get('capture') == 'SUCCESS' else None))
                if result.get('capture') == 'SUCCESS':
                    counts['captures'] += 1
                elif step.get('action') == 'WAIT':
                    counts['waits'] += 1
            answers = episode_answers(ranges, marks, a, b)
            counts['answered'] += bool(answers)
            last = max([s['t_s'] for s in steps if s['t_s'] is not None] + [b])
            clip = [max(0.0, a - 5), min(end, last + 3 if last > b else b + 10)]
            items.append(dict(
                candidate_seq=c.get('candidate_seq'), t_start_s=a, t_end_s=b, peak_score=c.get('peak_score'),
                **cc.band_ratios(t, ch2, ch1, a, b, [s for s in spans if s != (a, b)]),
                delivery=record.get('status') or 'NOT_FORWARDED', steps=steps, answers=answers,
                arm=actuation_near(arm, a, b),
                clip_s=[round(x, 3) for x in clip],
                clip_video_s=[round(x + offset, 3) for x in clip] if offset is not None else None,
                low_activity=[x for x in low['activity_like_spans'] if x['t0_s'] <= clip[1] and x['t1_s'] >= clip[0]],
                biological_activity='UNKNOWN'))
        request = manifest.get('request') or {}
        return dict(run=run, started_at_pc=manifest.get('started_at_pc'), duration_s=round(end, 2),
                    case_id=request.get('case_id') or '', note=request.get('note') or '',
                    forwarding=(path / 'delivery.jsonl').is_file(),
                    video=dict(available=bool(video.get('available')), offset_source=video.get('offset_source'),
                               content_box=video.get('content_box')),
                    summary=dict(candidates=len(items), **counts), episodes=items,
                    low_activity=dict(lowfreq_activity.summary(low), note=lowfreq_activity.NOTE),
                    arm_basis=arm[0]['time_basis'] if arm else None, biological_activity='UNKNOWN')

    def diary(self):
        """Every saved LIVE run as one diary entry (観察日記, docs/ad3_passive_bridge.md): when, the
        case and conditions, how many candidates, what the XIAO did (photos, waits) and the
        verified photos, plus how many candidates a human has answered in the review. Light:
        reads the small JSONL files only, never the waveform (the low-frequency activity-likeness
        is read from its cache; a missing one is computed in the background and shown as pending).
        MOCK runs are counted, not listed.
        Biological activity stays UNKNOWN; human photo labels are added by the server."""
        busy, entries, mock = self.busy_run(), [], 0
        if not self.output.is_dir():
            return dict(entries=entries, mock_runs=0, biological_activity='UNKNOWN')
        for path in self.output.iterdir():
            if not RUN_RE.fullmatch(path.name) or path.name == busy or trashed(path):
                continue
            try:
                manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
                size = (path / 'samples.f64').stat().st_size
            except (OSError, ValueError):
                continue
            if manifest.get('origin') != 'LIVE':
                mock += 1
                continue
            end = size / 16 / FS
            request = manifest.get('request') or {}
            candidates = read_jsonl(path / 'candidates.jsonl') if (path / 'candidates.jsonl').is_file() else []
            photos, counts = [], dict(captures=0, failed=0, waits=0)
            for record in read_jsonl(path / 'delivery.jsonl') if (path / 'delivery.jsonl').is_file() else []:
                if record.get('status') != 'acknowledged':
                    continue
                for step in [record.get('observation')] + list(record.get('follow_ups') or []):
                    if not step or step.get('action') in (None, 'NONE'):
                        continue
                    capture = (step.get('result') or {}).get('capture')
                    if step.get('action') == 'WAIT':
                        counts['waits'] += 1
                    elif capture == 'SUCCESS':
                        counts['captures'] += 1
                    elif capture:
                        counts['failed'] += 1
                    if capture == 'SUCCESS' and step.get('image_verified') is True and step.get('image_url'):
                        photos.append(dict(action_id=step.get('action_id'), url=step['image_url'],
                                           pose=step.get('target_pose') or POSE_OF_ACTION.get(step.get('action')),
                                           candidate_seq=record.get('candidate_seq')))
            marks, _ = marks_and_offset(read_jsonl(path / REVIEW_FILE))
            ranges = episode_ranges(marks, end)
            answers = [episode_answers(ranges, marks, c['t_start_s'], c['t_end_s']) for c in candidates]
            # PC reference only; computed in the background the first time (status PENDING until then)
            low = self._low_judge(path, actuation_spans(path, manifest, self.xiao_times(), self.xiao_capture_ms()), compute=False)
            entries.append(dict(
                run=path.name, started_at_pc=manifest.get('started_at_pc'), duration_s=round(end, 2),
                case_id=request.get('case_id') or '', note=request.get('note') or '',
                illumination=request.get('illumination') or '', forwarded=(path / 'delivery.jsonl').is_file(),
                video=(manifest.get('video') or {}).get('status') == 'RECORDED',
                candidates=len(candidates), answered=sum(1 for a in answers if a),
                answer_counts={k: sum(1 for a in answers if k in a) for k in EPISODE_RANGES},
                photos=photos, low_activity=lowfreq_activity.summary(low) if low else dict(status='PENDING'), **counts))
        entries.sort(key=lambda r: r['started_at_pc'] or '', reverse=True)
        return dict(entries=entries, mock_runs=mock, biological_activity='UNKNOWN')

    def spectrogram(self, run, v0=None, v1=None, cols=1200):
        """Log-frequency power spectrogram of the video's audio over video time [v0, v1]."""
        path = self.directory(run)
        manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
        file = video_path(path, manifest)
        if file is None:
            raise ValueError('この記録に動画はありません')
        audio, rate, start = self._audio(file)
        end = start + len(audio) / rate
        v0 = start if v0 is None else max(start, float(v0))
        v1 = end if v1 is None else min(end, float(v1))
        cols = int(cols)
        if not (math.isfinite(v0) and math.isfinite(v1)) or v1 <= v0 or not 10 <= cols <= SPEC_MAX_COLS:
            raise ValueError('Invalid audio range')
        n = 4096 if v1 - v0 > 30 else 2048 if v1 - v0 > 4 else 1024
        window = np.hanning(n).astype(np.float32)
        freqs = np.fft.rfftfreq(n, 1 / rate)
        fmax = min(SPEC_FMAX, rate / 2)
        edges = np.geomspace(SPEC_FMIN, fmax, SPEC_ROWS + 1)
        lo = np.minimum(np.searchsorted(freqs, edges[:-1]), len(freqs) - 1)
        hi = np.minimum(np.maximum(np.searchsorted(freqs, edges[1:]), lo + 1), len(freqs))
        times = np.linspace(v0, v1, cols)
        power = np.empty((SPEC_ROWS, cols), np.float32)
        for j, t in enumerate(times):
            a = max(0, int(round((t - start) * rate)) - n // 2)
            segment = audio[a:a + n]
            if len(segment) < n:
                segment = np.pad(segment, (0, n - len(segment)))
            spectrum = np.abs(np.fft.rfft(segment * window)) ** 2
            csum = np.concatenate(([0.0], np.cumsum(spectrum)))
            power[:, j] = (csum[hi] - csum[lo]) / (hi - lo)
        db = 10 * np.log10(power + 1e-12)
        floor, top = np.percentile(db, 5), np.percentile(db, 99.7)
        scaled = np.clip((db - floor) / max(top - floor, 1e-6) * 255, 0, 255).astype(np.uint8)[::-1]
        return dict(t_video=[round(float(t), 4) for t in times], rows=SPEC_ROWS, cols=cols,
                    f_top_hz=round(float(fmax)), f_bottom_hz=SPEC_FMIN, sample_rate=rate,
                    db_floor=round(float(floor), 1), db_top=round(float(top), 1),
                    data=base64.b64encode(scaled.tobytes()).decode(),  # rows top = high frequency
                    note='Audio of the PC video; display only.')
