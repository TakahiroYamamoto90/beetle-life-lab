"""Disturbance-likeness of a B2 candidate: PC-only reference numbers (2026-11-18).

From the 2026-11-18 review of 13 labelled runs (docs/analysis/ad3_label_review_20261118.md):
larva activity and digging in the case also raised CH2 (piezo) at 20-300 Hz, while voices and
sounds raised only its 300-1500 Hz band a little; a hard knock raised CH1's 1.5-5 kHz band far
above anything else. This module records those numbers for each candidate and a provisional
"disturbance-like" flag.

It never changes the detector, candidates.jsonl, what is sent to the XIAO, or any label. The
thresholds were fitted to 13 runs of one day and one case (box-04); they are frozen here so new
runs can test them, not because they are established. Not a detection of larva activity.

The filters are causal (sosfilt with carried state), so the live tracker and a replay of
samples.f64 give the same numbers.
"""
from collections import deque
import math

import numpy as np
from scipy import signal

SCHEMA = 'bll.candidate_disturbance.v1'
STATUS = 'PROVISIONAL_20261118_13_RUNS_BOX04'
FS = 100000
WINDOW = 2000                 # 20 ms, the same grid as features.csv
LOW_BAND_HZ = (20, 300)       # CH2: contact vibration through the substrate
CH1_HIGH_BAND_HZ = (1500, 5000)  # CH1 (reference sensor): hard knocks
LOW_MIN_X = 1.2
CREST_MIN = 2.4
CH1_HIGH_MAX_X = 15.0
BASELINE_S = 5.0
GAP_S = 0.3
MIN_BASELINE_ROWS = 25
KEEP_S = 40.0                 # max candidate 15 s + baseline 5.3 s, with room
NOTE = ('Reference only: CH2 20-300 Hz rise, CH2 300-1500 Hz crest and CH1 1.5-5 kHz rise of the '
        'candidate. disturbance_like uses provisional thresholds from 13 runs (2026-11-18); it is not '
        'a detection, it is not sent to the XIAO and it does not change any candidate or label.')


def _sos(band):
    return signal.butter(4, band, 'bandpass', fs=FS, output='sos')


class BandTracker:
    """20 ms RMS of CH2 20-300 Hz and CH1 1.5-5 kHz from raw (n, 2) blocks [CH1, CH2]."""

    def __init__(self):
        self.sos = (_sos(LOW_BAND_HZ), _sos(CH1_HIGH_BAND_HZ))
        self.zi = [np.zeros((len(s), 2)) for s in self.sos]
        self.first = None
        self.pending = np.empty((0, 2))
        self.index = 0
        self.rows = deque(maxlen=round(KEEP_S * FS / WINDOW))

    def push(self, values):
        values = np.asarray(values, dtype=float)
        if values.ndim != 2 or values.shape[1] != 2 or not len(values):
            raise ValueError('Invalid sample block')
        if self.first is None:
            self.first = values[0].copy()
        x = values - self.first
        low, self.zi[0] = signal.sosfilt(self.sos[0], x[:, 1], zi=self.zi[0])
        high, self.zi[1] = signal.sosfilt(self.sos[1], x[:, 0], zi=self.zi[1])
        self.pending = np.concatenate((self.pending, np.column_stack((low, high))))
        complete = len(self.pending) // WINDOW
        if not complete:
            return
        windows = self.pending[:complete * WINDOW].reshape(complete, WINDOW, 2)
        rms = np.sqrt(np.mean(windows * windows, axis=1))
        self.pending = self.pending[complete * WINDOW:].copy()
        for value in rms:
            self.rows.append(((self.index + .5) * WINDOW / FS, float(value[0]), float(value[1])))
            self.index += 1

    def arrays(self):
        if not self.rows:
            return np.zeros(0), np.zeros(0), np.zeros(0)
        t, low, high = map(np.array, zip(*self.rows))
        return t, low, high


def from_samples(path, chunk=1_000_000):
    """Replay a saved samples.f64 through the live tracker (whole run kept)."""
    raw = np.memmap(path, dtype='<f8', mode='r')
    raw = raw[:len(raw) // 2 * 2].reshape(-1, 2)
    tracker = BandTracker()
    tracker.rows = deque()
    for start in range(0, len(raw), chunk):
        tracker.push(np.asarray(raw[start:start + chunk]))
    return tracker.arrays()


def _peak_x(t, values, inside, base):
    if not inside.any() or base.sum() < MIN_BASELINE_ROWS:
        return None
    floor = float(np.median(values[base]))
    if not math.isfinite(floor) or floor <= 0:
        return None
    return round(float(np.max(values[inside])) / floor, 2)


def judge(bands, features, start, end, other=()):
    """Numbers and the provisional flag for one candidate.

    bands: (t, ch2_low_rms, ch1_high_rms) from BandTracker / from_samples.
    features: (t, ch2_rms) of features.csv (300-1500 Hz, the detector's band).
    other: (start, end) of the other candidates, left out of the baseline.
    """
    t, low, high = bands
    inside = (t >= start - .01) & (t <= max(end, start + .02) + .01)
    base = (t >= start - GAP_S - BASELINE_S) & (t < start - GAP_S)
    for a, b in other:
        base &= ~((t >= a - GAP_S) & (t <= b + GAP_S))
    low_x = _peak_x(t, low, inside, base)
    high_x = _peak_x(t, high, inside, base)
    ft, ch2 = features
    span = ch2[(ft >= start - .01) & (ft <= max(end, start + .02) + .01)]
    crest = round(float(span.max() / span.mean()), 2) if len(span) and span.mean() > 0 else None
    reasons = []
    if low_x is None:
        flag = None
        reasons.append('BASELINE_TOO_SHORT')
    else:
        contact = low_x >= LOW_MIN_X or (crest is not None and crest >= CREST_MIN)
        if not contact:
            reasons.append('NO_LOW_BAND_RISE_AND_NOT_SHARP')
        if high_x is not None and high_x >= CH1_HIGH_MAX_X:
            reasons.append('CH1_HIGH_BAND_EXTREME')
        flag = bool(reasons)
    return dict(schema=SCHEMA, status=STATUS, ch2_low_peak_x=low_x, ch2_crest=crest, ch1_high_peak_x=high_x,
                disturbance_like=flag, reasons=reasons,
                thresholds=dict(ch2_low_peak_x_min=LOW_MIN_X, ch2_crest_min=CREST_MIN,
                                ch1_high_peak_x_max=CH1_HIGH_MAX_X))
