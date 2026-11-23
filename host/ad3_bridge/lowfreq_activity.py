"""Low-frequency activity-likeness: PC-only reference numbers (2026-11-20).

From the 2026-11-19 runs (9 runs labelled against video, analysis/ad3_label_review/a7.py): when
the larva moved, the case piezo CH2 rose at 1-40 Hz in every run, more than the reference sensor
CH1, while the B2 detector (CH2 300-1500 Hz, 20 ms) overlapped only 11 of 57 LARVA ranges.
This module computes CH1/CH2 1-40 Hz RMS in 250 ms windows and marks spans where CH2 is well
above its own recent floor as "low-frequency activity-like".

It never changes the detector, candidates.jsonl, what is sent to the XIAO, or any label. The
threshold was chosen on those 9 runs of one evening; it is frozen here so new runs can test it,
not because it is established. Not a detection of larva activity: the arm moving and knocks also
raise 1-40 Hz (arm spans are masked when known; knocks are not).

The filters are causal (block mean + sosfilt with carried state), so a live tracker and a replay
of samples.f64 give the same numbers. The band-pass is elliptic because the 50 Hz mains hum is
far larger than the 1-40 Hz floor; the review page's 1-40 Hz display (Butterworth, zero phase)
still contains some of that hum.

Chosen on the 11/19 runs (in-sample): ratio >= 3.0 overlapped 46/57 LARVA ranges and flagged
6.3% of unlabelled time (LARVA time 26%); NOISE ranges 4/12 also overlapped. On the 11/18 runs
(other conditions): LARVA ranges 46/63, unlabelled time 16.8%, NOISE 15/39.
"""
import os
from pathlib import Path

import numpy as np
from scipy import signal

SCHEMA = 'bll.lowfreq_activity.v1'
STATUS = 'PROVISIONAL_20261120_9_RUNS_20261119'
FS = 100000
DECIMATE = 100                # block mean to 1 kHz
FS_LOW = FS // DECIMATE
BAND_HZ = (1, 40)
WINDOW = 250                  # 250 ms at 1 kHz
WINDOW_S = WINDOW / FS_LOW
FLOOR_S = 30.0                # trailing floor: 20th percentile of the last 30 s
FLOOR_PERCENTILE = 20
MIN_FLOOR_S = 10.0            # less usable history -> unknown
RATIO_MIN = 3.0               # CH2 rms / floor (analysis/ad3_label_review/a8.py sweep, 2026-11-20)
MERGE_GAP_S = 1.0
ARM_MARGIN_S = 0.3
NOTE = ('Reference only: CH2 1-40 Hz rms (250 ms) over its trailing 30 s 20th percentile, arm spans '
        'masked. activity_like uses a provisional threshold from 9 runs (2026-11-19); it is not a '
        'detection, it is not sent to the XIAO, and knocks or other vibration also raise it.')


class LowTracker:
    """250 ms RMS of CH1 and CH2 at 1-40 Hz from raw (n, 2) blocks [CH1, CH2]."""

    def __init__(self):
        # Elliptic, 60 dB stopband: a 4th-order Butterworth let the 50 Hz mains hum (about 1.6 mV on
        # CH2, 2026-11-19) through at several times the 1-40 Hz floor.
        self.sos = signal.ellip(6, 0.5, 60, BAND_HZ, 'bandpass', fs=FS_LOW, output='sos')
        self.zi = np.zeros((len(self.sos), 2, 2))
        self.first = None
        self.raw = np.empty((0, 2))
        self.low = np.empty((0, 2))
        self.rows = []            # (t_center_s, ch1_rms_V, ch2_rms_V)

    def push(self, values):
        values = np.asarray(values, dtype=float)
        if values.ndim != 2 or values.shape[1] != 2 or not len(values):
            raise ValueError('Invalid sample block')
        if self.first is None:
            self.first = values[0].copy()
        self.raw = np.concatenate((self.raw, values - self.first))
        blocks = len(self.raw) // DECIMATE
        if not blocks:
            return
        mean = self.raw[:blocks * DECIMATE].reshape(blocks, DECIMATE, 2).mean(axis=1)
        self.raw = self.raw[blocks * DECIMATE:].copy()
        y, self.zi = signal.sosfilt(self.sos, mean, axis=0, zi=self.zi)
        self.low = np.concatenate((self.low, y))
        complete = len(self.low) // WINDOW
        if not complete:
            return
        w = self.low[:complete * WINDOW].reshape(complete, WINDOW, 2)
        rms = np.sqrt(np.mean(w * w, axis=1))
        self.low = self.low[complete * WINDOW:].copy()
        start = len(self.rows)
        for i, value in enumerate(rms):
            self.rows.append(((start + i + .5) * WINDOW_S, float(value[0]), float(value[1])))

    def arrays(self):
        if not self.rows:
            return np.zeros(0), np.zeros(0), np.zeros(0)
        t, ch1, ch2 = map(np.array, zip(*self.rows))
        return t, ch1, ch2


def from_samples(path, chunk=1_000_000):
    """Replay a saved samples.f64 through the tracker."""
    raw = np.memmap(path, dtype='<f8', mode='r')
    raw = raw[:len(raw) // 2 * 2].reshape(-1, 2)
    tracker = LowTracker()
    for start in range(0, len(raw), chunk):
        tracker.push(np.asarray(raw[start:start + chunk]))
    return tracker.arrays()


def _arm_mask(t, arm_spans):
    m = np.zeros(len(t), bool)
    half = WINDOW_S / 2
    for a, b in arm_spans or ():
        m |= (t + half >= a - ARM_MARGIN_S) & (t - half <= b + ARM_MARGIN_S)
    return m


def judge(rows, arm_spans=()):
    """Per-window ratio and merged activity-like spans.

    rows: (t, ch1_rms, ch2_rms) from LowTracker / from_samples.
    arm_spans: (t0, t1) when the arm moved (review.actuation_spans); those windows are neither
    flagged nor used for the floor.
    """
    t, ch1, ch2 = (np.asarray(x, dtype=float) for x in rows)
    arm = _arm_mask(t, arm_spans)
    n_floor, n_min = round(FLOOR_S / WINDOW_S), round(MIN_FLOOR_S / WINDOW_S)
    ratio = np.full(len(t), np.nan)
    if len(t):
        # trailing window of the n_floor windows before i (arm windows left out), vectorised
        hist = np.concatenate((np.full(n_floor, np.nan), np.where(arm, np.nan, ch2)))
        win = np.lib.stride_tricks.sliding_window_view(hist, n_floor)[:len(t)]
        enough = np.sum(~np.isnan(win), axis=1) >= n_min
        if enough.any():
            floor = np.full(len(t), np.nan)
            # row-wise percentile ignoring NaN (linear, as np.percentile): sort puts NaN last
            srt = np.sort(win[enough], axis=1)
            k = np.sum(~np.isnan(srt), axis=1)
            pos = (k - 1) * (FLOOR_PERCENTILE / 100)
            lo = np.floor(pos).astype(int)
            hi = np.minimum(lo + 1, k - 1)
            rows_i = np.arange(len(srt))
            a, b, frac = srt[rows_i, lo], srt[rows_i, hi], pos - lo
            floor[enough] = a + (b - a) * frac
            ok = enough & (floor > 0)
            ratio[ok] = ch2[ok] / floor[ok]
    active = (ratio >= RATIO_MIN) & ~arm
    spans = []
    for i in np.flatnonzero(active):
        a, b = t[i] - WINDOW_S / 2, t[i] + WINDOW_S / 2
        if spans and a - spans[-1]['t1_s'] <= MERGE_GAP_S:
            spans[-1]['t1_s'] = b
            spans[-1]['_i'].append(i)
        else:
            spans.append(dict(t0_s=a, t1_s=b, _i=[i]))
    for s in spans:
        idx = np.array(s.pop('_i'))
        s['t0_s'], s['t1_s'] = round(float(s['t0_s']), 3), round(float(s['t1_s']), 3)
        s['peak_x'] = round(float(np.nanmax(ratio[idx])), 2)
        s['ch2_over_ch1'] = round(float(np.median(ch2[idx] / np.maximum(ch1[idx], 1e-12))), 2)
    usable = ~np.isnan(ratio) & ~arm
    return dict(schema=SCHEMA, status=STATUS, note=NOTE, activity_like_spans=spans,
                window_s=WINDOW_S, usable_s=round(float(usable.sum() * WINDOW_S), 2),
                active_s=round(float(active.sum() * WINDOW_S), 2), unknown_s=round(float((~usable).sum() * WINDOW_S), 2),
                thresholds=dict(ratio_min=RATIO_MIN, floor_s=FLOOR_S, floor_percentile=FLOOR_PERCENTILE,
                                min_floor_s=MIN_FLOOR_S, band_hz=list(BAND_HZ), merge_gap_s=MERGE_GAP_S),
                biological_activity='UNKNOWN')


# ---- derived-rows cache (display only; the run folder is never written) ----
CACHE_VERSION = f'{SCHEMA}|{STATUS}|ellip6-0.5-60|{BAND_HZ}|{DECIMATE}|{WINDOW}'


def _cache_key(samples_path):
    st = samples_path.stat()
    return f'{CACHE_VERSION}|{st.st_size}|{st.st_mtime_ns}'


def cached_only(samples_path, cache_dir):
    """Rows from the cache when it matches the samples file and this module, else None."""
    samples_path, file = Path(samples_path), Path(cache_dir) / (Path(samples_path).parent.name + '.npz')
    try:
        with np.load(file) as z:
            if str(z['key']) == _cache_key(samples_path):
                return z['t'], z['ch1'], z['ch2']
    except (OSError, ValueError, KeyError):
        pass
    return None


def cached_rows(samples_path, cache_dir):
    """Rows of from_samples, computed once per samples file and kept in cache_dir."""
    rows = cached_only(samples_path, cache_dir)
    if rows is not None:
        return rows
    samples_path, cache_dir = Path(samples_path), Path(cache_dir)
    key = _cache_key(samples_path)
    rows = from_samples(samples_path)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_dir / (samples_path.parent.name + '.tmp')
        with open(tmp, 'wb') as fh:
            np.savez(fh, key=key, t=rows[0], ch1=rows[1], ch2=rows[2])
        os.replace(tmp, cache_dir / (samples_path.parent.name + '.npz'))
    except OSError:
        pass  # the numbers are still returned; only the cache is missing
    return rows


def summary(result):
    """Short form for list views: how many spans, how long, the largest ratio."""
    spans = result['activity_like_spans']
    return dict(schema=SCHEMA, status=STATUS, spans=len(spans), active_s=result['active_s'],
                usable_s=result['usable_s'], peak_x=max((s['peak_x'] for s in spans), default=None),
                biological_activity='UNKNOWN')
