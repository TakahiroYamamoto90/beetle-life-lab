"""Context numbers for each B2 candidate: how much CH2 (piezo in the case), CH1 (reference
sensor) and, in review, the recording's sound rose. PC-only display and records.

This never changes the detector, the candidates, what is sent to the XIAO, or any label.
The numbers are hints for a human reviewer, not a classification: on 2026-11-13 digging in
the case raised CH2 only, and outside vibration (voice, servo, desk) raised CH1 far more than
CH2, but that comes from two short, staged runs and no larva activity.

Ratios use the same 300-1500 Hz band RMS windows (20 ms) that features.csv already holds:
ratio = mean RMS inside the candidate / median RMS of the BASELINE_S before it (rows inside
other candidates are left out of the baseline).
"""
import math

import numpy as np

SCHEMA = 'bll.candidate_context.v1'
BASELINE_S = 5.0
GAP_S = 0.3              # skip the rise just before the candidate starts
MIN_BASELINE_ROWS = 25   # 0.5 s of 20 ms windows
SELF_ACTUATION_S = 10.0  # XIAO kSelfActuationGuardMs (config.h), counted from the last frame receipt here
NOTE = ('Reference only: CH1/CH2/sound rises around the candidate. Not a detection and not '
        'sent to the XIAO; thresholds are not established.')


def _ratio(values, inside, base):
    if not inside.any() or base.sum() < MIN_BASELINE_ROWS:
        return None
    floor = float(np.median(values[base]))
    if not math.isfinite(floor) or floor <= 0:
        return None
    return round(float(np.mean(values[inside])) / floor, 2)


def band_ratios(t, ch2, ch1, start, end, other=()):
    """CH2 and CH1 band-RMS ratios for one candidate. t, ch2, ch1 are numpy arrays."""
    inside = (t >= start) & (t <= end)
    base = (t >= start - GAP_S - BASELINE_S) & (t < start - GAP_S)
    for a, b in other:
        base &= ~((t >= a - GAP_S) & (t <= b + GAP_S))
    ch2_x = _ratio(ch2, inside, base)
    ch1_x = _ratio(ch1, inside & np.isfinite(ch1), base & np.isfinite(ch1)) if ch1 is not None else None
    return dict(ch2_x=ch2_x, ch1_x=ch1_x,
                ch1_over_ch2=round(ch1_x / ch2_x, 2) if ch1_x is not None and ch2_x else None)


def rows_to_arrays(rows):
    t = np.array([float(r['t_center_s']) for r in rows])
    ch2 = np.array([float(r['rms_V']) for r in rows])
    ch1 = np.array([float(r['ch1_rms_V']) if r.get('ch1_rms_V') not in (None, '') else math.nan for r in rows])
    return t, ch2, ch1


def audio_ratio(audio, rate, start_video_s, end_video_s, floor):
    """Sound RMS inside the candidate (video time) / floor of the whole recording."""
    a, b = int(start_video_s * rate), int(math.ceil(end_video_s * rate))
    if a < 0 or b > len(audio) or b - a < 2 or not floor:
        return None
    return round(float(np.sqrt(np.mean(audio[a:b] ** 2))) / floor, 1)


def audio_floor(filtered, rate):
    """20th percentile of 0.5 s sound RMS: the quiet level of the recording."""
    step = max(1, rate // 2)
    usable = len(filtered) // step * step
    if usable < step * 4:
        return None
    rms = np.sqrt(np.mean(filtered[:usable].reshape(-1, step) ** 2, axis=1))
    value = float(np.percentile(rms, 20))
    return value if value > 0 else None
