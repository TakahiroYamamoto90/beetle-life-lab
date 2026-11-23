"""Observation trigger: 1-40 Hz activity OR a B2 candidate (2026-11-24, user decision).

An observation episode on the MCU starts at the start of an activity span, where activity is
  - a 250 ms window where the small random forest (lowfreq_forest.py) is at or above its
    threshold, OR the CH2 1-40 Hz RMS is at least 3x its trailing floor (lowfreq_activity.py)
    (2026-11-23; on the 5 runs of 2026-11-21 evening the forest overlapped 35/41 LARVA ranges,
    the ratio 30/41, B2 10/41: docs/analysis/ad3_label_review_20261118.md), OR
  - a confirmed B2 candidate (CH2 300-1500 Hz, processing.py) (added 2026-11-24: on the two
    labelled runs of 2026-11-23 23:55/23:59 the larva moved near the surface and the 1-40 Hz
    sources caught 3/23 LARVA ranges outside arm spans, B2 15/23, the union 17/23; on 11/21 the
    union went 29/37 -> 30/37: docs/analysis/trigger_b2_union_20261124.md).
A trigger started by the 1-40 Hz sources is sent as detector LOWFREQ_FOREST_RATIO with its ratio
as the score; one started by B2 alone is sent as B2_guarded_mad with the B2 score (both accepted
by firmware 0.17.0, so no firmware change). Every B2 candidate is still saved in
b2_candidates.jsonl (trigger_seq names the trigger it started, if any).

Same numbers as the review page: the windows come from lowfreq_activity.LowTracker (causal), the
forest features and the ratio floor are computed exactly as lowfreq_forest.features() /
lowfreq_activity.judge(), on the latest windows only. Windows while the MCU is busy with an
episode (and a short settle after it) are masked like arm spans: they neither trigger nor enter
the floor; B2 candidates confirmed then are ignored the same way. A trigger is the first active
window or B2 confirmation after at least MERGE_GAP_S without one; it is sent at once, so a long
activity gives one trigger, not one per window.

This is the start of an observation, not a detection of the larva: knocks, the arm and other
vibration also raise both bands.
"""
import hashlib
import json
import math
from pathlib import Path

import numpy as np

import lowfreq_activity as lf
import lowfreq_forest as lff

SCHEMA = 'bll.activity_trigger.v2'
TRIGGER = 'LOWFREQ_FOREST_RATIO_OR_B2'  # what starts observations (manifest observation_trigger.detector)
DETECTOR = 'LOWFREQ_FOREST_RATIO'       # wire/Evidence detector of a trigger started by the 1-40 Hz sources
B2_DETECTOR = 'B2_guarded_mad'          # ... and of one started by a B2 candidate alone
RATIO_MIN = lf.RATIO_MIN          # 3.0
MERGE_GAP_S = lf.MERGE_GAP_S      # 1.0 s without an active window ends a span
SETTLE_AFTER_BUSY_S = 5.0         # after an episode: park (~2.7 s) + settle, masked like the arm
HISTORY = 150                     # windows re-evaluated per step (floor 120 + features 12 + margin)
NOTE = ('Observation trigger (2026-11-24): start of an activity span: 1-40 Hz (forest >= threshold or '
        'ratio >= 3) or a B2 candidate (300-1500 Hz). Starts an observation episode; not a detection of the larva.')


def fingerprint(model, b2_config_sha256=None):
    """sha256 of what decides a trigger: the model, the thresholds, the B2 configuration and this code."""
    meta = dict(schema=SCHEMA, detector=TRIGGER, ratio_min=RATIO_MIN, merge_gap_s=MERGE_GAP_S,
                b2_config_sha256=b2_config_sha256,
                settle_after_busy_s=SETTLE_AFTER_BUSY_S, model_sha256=hashlib.sha256(
                    json.dumps(model, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                code_sha256={name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                             for name in ('activity_trigger.py', 'lowfreq_activity.py', 'lowfreq_forest.py')})
    return hashlib.sha256(json.dumps(meta, sort_keys=True, separators=(',', ':')).encode()).hexdigest(), meta


class ActivityTrigger:
    def __init__(self, model=None, b2_config_sha256=None):
        self.model = model or lff.load_model()
        if self.model is None:
            raise RuntimeError('Low-frequency forest model missing (ml/lowfreq_forest)')
        self.fingerprint, self.config = fingerprint(self.model, b2_config_sha256)
        self.tracker = lf.LowTracker()
        self.judged = 0                 # windows already judged
        self.span_end = -math.inf       # end of the current activity span (s)
        self.busy = []                  # [t0, t1] on the sample clock; t1 None while busy
        self.windows = []               # per judged window: (t, p, ratio, active, masked)
        self.b2_used = 0                # B2 candidates that started a trigger
        self.b2_masked = 0              # B2 candidates ignored while busy
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
    def _start(self, a, b):
        """Extend the current span to b; True when a starts a new one."""
        new = a - self.span_end > MERGE_GAP_S
        self.span_end = max(self.span_end, float(b))
        return new

    def push(self, values, b2_events=()):
        """Feed raw (n, 2) samples and the B2 candidates confirmed in them (processing.Processor);
        returns the new trigger events (dicts). A B2-started event carries b2_t_start_s."""
        self.tracker.push(values)
        rows = self.tracker.rows
        events = self._push_b2(b2_events)
        if self.judged >= len(rows):
            return events
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
        for k in range(self.judged - start, len(t)):
            forest_on = bool(usable[k] and p[k] >= self.model['threshold'])
            ratio_on = bool(np.isfinite(ratio[k]) and ratio[k] >= RATIO_MIN and not masked[k])
            active = forest_on or ratio_on
            self.windows.append((float(t[k]), None if np.isnan(p[k]) else float(p[k]),
                                 None if not np.isfinite(ratio[k]) else float(ratio[k]), active, bool(masked[k])))
            a, b = t[k] - lf.WINDOW_S / 2, t[k] + lf.WINDOW_S / 2
            if not active:
                continue
            if self._start(a, b):
                by = [name for name, on in (('FOREST', forest_on), ('RATIO_3', ratio_on)) if on]
                events.append(dict(
                    t_start_s=round(float(a), 3), t_end_s=round(float(b), 3), confirmed_at_s=round(float(b), 3),
                    peak_rms_V=max(float(ch2[k]), 1e-6), peak_score=max(float(ratio[k]), .001),
                    forest_p=None if np.isnan(p[k]) else round(float(p[k]), 3), ratio=round(float(ratio[k]), 3),
                    triggered_by=by, detector=DETECTOR, end_reason='TRIGGER_AT_ACTIVITY_START',
                    quality='DEVELOPMENT_CANDIDATE'))
        self.judged = len(rows)
        last_t = float(t[-1])
        self.state = ('WARMUP' if not np.isfinite(ratio[-1]) else
                      'CANDIDATE' if last_t + lf.WINDOW_S / 2 - self.span_end <= MERGE_GAP_S else 'MONITORING')
        return events

    def _push_b2(self, b2_events):
        events = []
        for e in b2_events:
            if e.get('quality') != 'DEVELOPMENT_CANDIDATE':
                continue
            a = float(e['t_start_s'])
            b = float(e.get('confirmed_at_s') or a)
            if any(x <= a <= y for x, y in self._mask_spans(b)):
                self.b2_masked += 1
                continue
            if not self._start(a, b):
                continue  # inside a span that has already triggered
            self.b2_used += 1
            events.append(dict(
                t_start_s=round(a, 3), t_end_s=round(max(b, a + .001), 3), confirmed_at_s=round(b, 3),
                peak_rms_V=max(float(e['peak_rms_V']), 1e-6), peak_score=float(e['peak_score']),
                forest_p=None, ratio=None, triggered_by=['B2'], detector=B2_DETECTOR,
                b2_t_start_s=e['t_start_s'], end_reason='TRIGGER_AT_ACTIVITY_START',
                quality='DEVELOPMENT_CANDIDATE'))
        return events

    def summary(self):
        n = len(self.windows)
        return dict(schema=SCHEMA, detector=TRIGGER, note=NOTE, config=self.config,
                    config_sha256=self.fingerprint, windows=n, b2_triggers=self.b2_used,
                    b2_ignored_while_busy=self.b2_masked,
                    active_windows=sum(1 for w in self.windows if w[3]),
                    masked_windows=sum(1 for w in self.windows if w[4]),
                    busy_spans=[[round(a, 3), None if b is None else round(b, 3)] for a, b in self.busy])
