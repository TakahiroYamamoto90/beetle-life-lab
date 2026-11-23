"""Shadow triggers: where a 1-40 Hz path would have raised a candidate (PC reference, 2026-11-21).

Run beside B2 before any change to what starts an observation (user-approved shadow period,
2026-11-21; docs/analysis/ad3_label_review_20261118.md, B2 threshold sweep). B2 stays the only
detector whose candidates go to the XIAO; nothing here is sent anywhere, written to the run
folder or the Evidence log, or used by the Observation Policy.

A shadow trigger is the start of an activity-like span of one source, at the time it would have
been known: the end of the span's first 250 ms window (all sources are causal). Only triggers are
counted. Whether the XIAO would then have observed or waited (cool-downs, recency) is its policy's
decision (observation_core.h) and is deliberately not imitated on the PC.

Sources: LOWFREQ_RATIO_3 (lowfreq_activity, ratio >= 3.0), FOREST_PC (lowfreq_forest on the PC),
FOREST_XIAO (the same forest computed by the XIAO during the capture, xiao_lowfreq.jsonl).
"""
SCHEMA = 'bll.shadow_trigger.v1'
WINDOW_S = 0.25      # lowfreq_activity.WINDOW_S: a span is known after its first window
MATCH_S = 1.0        # a trigger and a B2 candidate this close count as the same moment
AFTER_ARM_S = 2.0    # review.SELF_NOISE_AFTER_S: a trigger this soon after an arm span is marked
NOTE = ('Shadow only: where each 1-40 Hz source would have raised a candidate (start of its '
        'activity-like span). Not sent to the XIAO and not an observation decision; B2 is unchanged '
        'and still the only trigger.')


def triggers(spans, peak_key):
    out = []
    for s in spans or ():
        out.append(dict(t_s=round(s['t0_s'] + WINDOW_S, 3), t0_s=s['t0_s'], t1_s=s['t1_s'],
                        peak=s.get(peak_key), windows=max(1, round((s['t1_s'] - s['t0_s']) / WINDOW_S))))
    return out


def _near(t, spans, gap):
    return any(a - gap <= t <= b + gap for a, b in spans)


def compare(duration_s, b2_candidates, sources, arm_spans=()):
    """sources: name -> (spans, peak_key) or None when the source is unavailable for this run."""
    minutes = max(duration_s, 1e-9) / 60
    b2 = [(c['t_start_s'], c['t_end_s']) for c in b2_candidates
          if c.get('t_start_s') is not None and c.get('t_end_s') is not None]
    after_arm = [(b, b + AFTER_ARM_S) for _, b in arm_spans or ()]
    out = dict(schema=SCHEMA, note=NOTE, duration_s=round(duration_s, 3), match_s=MATCH_S,
               b2=dict(count=len(b2), per_min=round(len(b2) / minutes, 2)), sources={},
               biological_activity='UNKNOWN')
    for name, source in sources.items():
        if source is None:
            out['sources'][name] = dict(status='UNAVAILABLE')
            continue
        spans, peak_key = source
        trig = triggers(spans, peak_key)
        for x in trig:
            x['with_b2'] = _near(x['t_s'], b2, MATCH_S)
            x['after_arm'] = _near(x['t_s'], after_arm, 0)
        shadow_spans = [(x['t0_s'], x['t1_s']) for x in trig]
        out['sources'][name] = dict(
            status='OK', count=len(trig), per_min=round(len(trig) / minutes, 2),
            with_b2=sum(x['with_b2'] for x in trig), without_b2=sum(not x['with_b2'] for x in trig),
            after_arm=sum(x['after_arm'] for x in trig),
            b2_not_covered=sum(not any(a - MATCH_S <= v and u <= b + MATCH_S for u, v in shadow_spans) for a, b in b2),
            triggers=trig)
    return out
