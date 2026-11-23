"""Observation trigger from low-frequency activity (2026-11-23, user decision).

The trigger that starts an observation episode on the MCU is no longer the B2 detector
(CH2 300-1500 Hz, 20 ms) but the start of a low-frequency activity span: a 250 ms window where
the small random forest (lowfreq_forest.py) is at or above its threshold OR the CH2 1-40 Hz RMS
is at least 3x its trailing floor (lowfreq_activity.py). On the 5 runs of 2026-11-21 evening
(not used for training) the forest overlapped 35/41 LARVA ranges and the ratio 30/41, against
10/41 for B2 (docs/analysis/ad3_label_review_20261118.md). B2 still runs and is saved for
comparison (b2_candidates.jsonl); it no longer triggers anything.

Same numbers as the review page: the windows come from lowfreq_activity.LowTracker (causal), the
forest features and the ratio floor are computed exactly as lowfreq_forest.features() /
lowfreq_activity.judge(), on the latest windows only. Windows while the MCU is busy with an
episode (and a short settle after it) are masked like arm spans: they neither trigger nor enter
the floor. A trigger is the first active window after at least MERGE_GAP_S without one; it is
sent at once (end = that window's end), so a long activity gives one trigger, not one per window.

This is the start of an observation, not a detection of the larva: knocks and other vibration
also raise the 1-40 Hz band.
"""
import hashlib
import json
import math
from pathlib import Path

import numpy as np

import lowfreq_activity as lf
import lowfreq_forest as lff

SCHEMA = 'bll.activity_trigger.v1'
DETECTOR = 'LOWFREQ_FOREST_RATIO'
RATIO_MIN = lf.RATIO_MIN          # 3.0
MERGE_GAP_S = lf.MERGE_GAP_S      # 1.0 s without an active window ends a span
SETTLE_AFTER_BUSY_S = 5.0         # after an episode: park (~2.7 s) + settle, masked like the arm
HISTORY = 150                     # windows re-evaluated per step (floor 120 + features 12 + margin)
NOTE = ('Observation trigger (2026-11-23): start of a 1-40 Hz activity span (forest >= threshold or '
        'ratio >= 3). Starts an observation episode; not a detection of the larva.')


def fingerprint(model):
    """sha256 of what decides a trigger: the model, the thresholds and this code."""
    meta = dict(schema=SCHEMA, detector=DETECTOR, ratio_min=RATIO_MIN, merge_gap_s=MERGE_GAP_S,
                settle_after_busy_s=SETTLE_AFTER_BUSY_S, model_sha256=hashlib.sha256(
                    json.dumps(model, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                code_sha256={name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                             for name in ('activity_trigger.py', 'lowfreq_activity.py', 'lowfreq_forest.py')})
    return hashlib.sha256(json.dumps(meta, sort_keys=True, separators=(',', ':')).encode()).hexdigest(), meta


class ActivityTrigger:
    def __init__(self, model=None):
        self.model = model or lff.load_model()
        if self.model is None:
            raise RuntimeError('Low-frequency forest model missing (ml/lowfreq_forest)')
        self.fingerprint, self.config = fingerprint(self.model)
        self.tracker = lf.LowTracker()
        self.judged = 0                 # windows already judged
        self.span_end = -math.inf       # end of the current activity span (s)
        self.busy = []                  # [t0, t1] on the sample clock; t1 None while busy
        self.windows = []               # per judged window: (t, p, ratio, active, masked)
        self.state = 'WARMUP'

    # ---- busy spans (delivery thread) ----
    def busy_begin(self, t_s):
        self.busy.append([t_s, None])

    def busy_end(self, t_s):
        if self.busy and self.busy[-1][1] is None:
            self.busy[-1][1] = t_s + SETTLE_AFTER_BUSY_S

    def _mask_spans(self, now_s):
        return [(a, now_s + 3600 if b is None else b) for a, b in list(self.busy)]

    # ---- acquisition thread ----
    def push(self, values):
        """Feed raw (n, 2) samples; returns the new trigger events (dicts)."""
        self.tracker.push(values)
        rows = self.tracker.rows
        if self.judged >= len(rows):
            return []
        start = max(0, self.judged - HISTORY)
        part = rows[start:]
        t, ch1, ch2 = (np.array(x, dtype=float) for x in zip(*part))
        spans = self._mask_spans(float(t[-1]))
        _, X, usable = lff.features((t, ch1, ch2), spans)
        p = np.full(len(t), np.nan)
        if usable.any():
            p[usable] = lff.predict(self.model, X[usable])
        ratio = np.exp(X[:, 0])        # ch2 / trailing floor (NaN until the floor exists)
        masked = lf._arm_mask(t, spans)
        events = []
        for k in range(self.judged - start, len(t)):
            forest_on = bool(usable[k] and p[k] >= self.model['threshold'])
            ratio_on = bool(np.isfinite(ratio[k]) and ratio[k] >= RATIO_MIN and not masked[k])
            active = forest_on or ratio_on
            self.windows.append((float(t[k]), None if np.isnan(p[k]) else float(p[k]),
                                 None if not np.isfinite(ratio[k]) else float(ratio[k]), active, bool(masked[k])))
            a, b = t[k] - lf.WINDOW_S / 2, t[k] + lf.WINDOW_S / 2
            if not active:
                continue
            if a - self.span_end > MERGE_GAP_S:
                by = [name for name, on in (('FOREST', forest_on), ('RATIO_3', ratio_on)) if on]
                events.append(dict(
                    t_start_s=round(float(a), 3), t_end_s=round(float(b), 3), confirmed_at_s=round(float(b), 3),
                    peak_rms_V=max(float(ch2[k]), 1e-6), peak_score=max(float(ratio[k]), .001),
                    forest_p=None if np.isnan(p[k]) else round(float(p[k]), 3), ratio=round(float(ratio[k]), 3),
                    triggered_by=by, detector=DETECTOR, end_reason='TRIGGER_AT_ACTIVITY_START',
                    quality='DEVELOPMENT_CANDIDATE'))
            self.span_end = float(b)
        self.judged = len(rows)
        last_t = float(t[-1])
        self.state = ('WARMUP' if not np.isfinite(ratio[-1]) else
                      'CANDIDATE' if last_t + lf.WINDOW_S / 2 - self.span_end <= MERGE_GAP_S else 'MONITORING')
        return events

    def summary(self):
        n = len(self.windows)
        return dict(schema=SCHEMA, detector=DETECTOR, note=NOTE, config=self.config,
                    config_sha256=self.fingerprint, windows=n,
                    active_windows=sum(1 for w in self.windows if w[3]),
                    masked_windows=sum(1 for w in self.windows if w[4]),
                    busy_spans=[[round(a, 3), None if b is None else round(b, 3)] for a, b in self.busy])
