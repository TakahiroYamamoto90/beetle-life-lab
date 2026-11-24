// デモ再生（/demo）LIVE mode: the current bridge state (/api/state) as one episode and a mark for
// the 6-step navigator. Pure functions (no DOM), loadable in Node for tests.
//
// What the bridge knows while a run is going: the detector state (CANDIDATE = inside a
// vibration event), the finished candidates, which one is being sent to the XIAO
// (delivering), the arm lines the XIAO prints while it works (arm_live: MOVE_BEGIN,
// CAPTURE_BEGIN, PARK_*), and - once the XIAO's answer for the whole episode is back - its
// decisions, results and images (observations). Results of follow-up views therefore appear
// together at the end of an episode. Larva activity stays UNKNOWN.

export const HOLD_S = 8; // a finished episode stays on screen this long (seconds of acquisition)

const capture = a => String(a || '').startsWith('CAPTURE_');

/** One observation summary of the bridge -> the step shape used by the replay. */
export function stepOf(o) {
  return {action: o.action, action_id: o.action_id, reason_codes: o.reason_codes || [], pose: o.target_pose,
    status: o.status, capture: o.result?.capture, capture_reason: o.result?.reason,
    image_url: o.image_verified === true ? o.image_url : null, t_s: null, ai: aiFromVision(o.vision_ai, o.result?.capture)};
}
/** Same shape as review.ai_of: the model's top class and raw score of a successful capture. */
export function aiFromVision(v, capture) {
  const p = v?.prediction, top = p?.top;
  if (capture !== 'SUCCESS' || p?.status !== 'MODEL_OUTPUT_UNVERIFIED' || top?.score == null || !top.class_name) return null;
  return {class_name: top.class_name, score: top.score, recipe: v.model?.recipe ?? null};
}

/** The episode in focus and the mark in force (null = watching), from a /api/state snapshot.
 *  heldSince: acquisition seconds when this episode's answer was first seen (the caller keeps it). */
export function liveState(s, heldSince = null) {
  const candidates = s.candidates || [], obs = s.observations || [];
  const seq = s.delivering ?? candidates[candidates.length - 1]?.candidate_seq ?? null;
  const cand = candidates.find(c => c.candidate_seq === seq) || null;
  const steps = obs.filter(o => o.candidate_seq === seq).map(stepOf);
  const ctx = cand?.context || {};
  const ep = {candidate_seq: seq, t_start_s: cand?.t_start_s, t_end_s: cand?.t_end_s, ch2_x: ctx.ch2_x ?? null,
    ch1_x: ctx.ch1_x ?? null, steps, answers: [], arm: null,
    // what started this trigger (B2 or the 1-40 Hz forest/ratio), shown on the clue card and graph
    trigger: cand ? {detector: cand.detector, by: cand.triggered_by, forest_p: cand.forest_p, ratio: cand.ratio, peak_score: cand.peak_score} : null,
    delivery: !s.forwarding ? 'NOT_FORWARDED' : steps.length ? 'acknowledged' : 'sending'};
  if (!s.running) return {ep, mark: null};
  if (s.detector_state === 'CANDIDATE') return {ep: {...ep, steps: [], ch2_x: null, ch1_x: null, trigger: null}, mark: {kind: 'SENSE'}};
  if (seq == null) return {ep, mark: null};
  const arm = s.arm_live || [], last = arm[arm.length - 1];
  const working = s.delivering != null;
  if (working && !steps.length) {
    // The XIAO is working on this candidate right now; its arm lines tell how far it got.
    if (last?.kind === 'MOVE_BEGIN') return {ep, mark: {kind: 'MOVE', pose: last.pose}};
    if (last?.kind === 'CAPTURE_BEGIN') return {ep, mark: {kind: 'CAPTURE', pose: last.pose}};
    return {ep, mark: {kind: 'DECIDE', pending: true}};
  }
  if (working && last?.kind === 'PARK_BEGIN') return {ep, mark: {kind: 'PARK', pose: 'FRONT'}};
  const end = heldSince ?? cand?.t_end_s ?? s.elapsed_s;
  if (!working && s.elapsed_s - end > HOLD_S) return {ep, mark: null}; // back to watching
  if (!steps.length) return {ep, mark: {kind: 'DECIDE', none: !s.forwarding, pending: !!s.forwarding}};
  const i = steps.length - 1, step = steps[i];
  if (step.action === 'WAIT') return {ep, mark: {kind: 'WAIT', step: i}};
  if (capture(step.action)) return {ep, mark: {kind: 'RESULT', step: i, pose: step.pose}};
  return {ep, mark: {kind: 'DECIDE', step: i, pose: step.pose}};
}
