// デモ再生（/demo）: turns one observation episode into a timeline of marks for the 6-step
// navigator (docs/Beetle_Life_Lab_ProtoPedia_Presentation_Design_20261115.md §4–5).
// Pure functions only (no DOM), so Node can load and test this module.
//
// Every mark time comes from the run's own records: the B2 candidate span, the PC receipt of
// the XIAO's arm lines (actuation.jsonl) or the XIAO decision/result times, and the capture
// times of arm_shots(). Where a step has no record of its own (the decision itself, the
// short "recorded" moment after a capture) the mark sits between recorded times; the page
// says the switch-over times are approximate. Nothing here adds sensing results: larva
// activity stays UNKNOWN.

export const STEPS = [
  // labels: docs/Beetle_Life_Lab_Development_Demo_UI_Directive_20261120.md §3.1
  {n: 1, label: '変化に気づく', tech: 'SENSE / 振動のきっかけ（フォレスト・比・B2）', tone: 'observe', icon: 'vibration'},
  {n: 2, label: 'これまでを確認', tech: 'REMEMBER / Evidence・Reducer', tone: 'observe', icon: 'history'},
  {n: 3, label: '次を決める', tech: 'DECIDE / ルールベース方策', tone: 'act', icon: 'alt_route'},
  {n: 4, label: 'カメラを動かす', tech: 'ACT / サーボ', tone: 'act', icon: 'precision_manufacturing'},
  {n: 5, label: '写真で確かめる', tech: 'OBSERVE / Vision V2で撮影', tone: 'observe', icon: 'photo_camera'},
  {n: 6, label: '観察を記録', tech: 'RECORD / Evidence・観察日記', tone: 'done', icon: 'menu_book'},
];
// Material Symbols names: the icon of each mark's result and of each arm view.
export const RESULT_ICON = {SENSE: 'graphic_eq', DECIDE: 'alt_route', WAIT: 'hourglass_top', MOVE: 'open_with', CAPTURE: 'photo_camera',
  RESULT: 'task_alt', PARK: 'home', END: 'visibility'};
export const POSE_ICON = {FRONT: 'arrow_forward', TOP: 'arrow_downward', BACK: 'arrow_back', FIXED: 'videocam'};
export const POSE_JA = {FRONT: '前', TOP: '上', BACK: '後ろ', FIXED: '固定カメラ'};
const STAGES_OF = {WATCH: [], SENSE: [1], DECIDE: [2, 3], WAIT: [2, 3], MOVE: [4], CAPTURE: [5], RESULT: [6],
  PARK: [4], END: []};
export const RECORD_S = 1.0;   // how long ⑥ stays lit after a capture before the next decision
export const WAIT_SHOW_S = 4.0; // a WAIT stays on screen this long when nothing follows
export const CAPTURE_S = 0.6;   // capture length used only when the run has no capture-start record
const PARK_WITHIN_S = 15;       // a park that starts this soon after the last capture belongs to the episode

const isCapture = s => String(s?.action || '').startsWith('CAPTURE_') && s.t_s != null;

/** Marks of one episode, in time order: {t, kind, step?, pose?, estimated?}. */
export function buildTimeline(ep, spans = []) {
  const marks = [], push = (t, kind, extra = {}) => marks.push({t, kind, ...extra});
  const moves = spans.filter(x => x.kind !== 'PARK');
  push(ep.t_start_s, 'SENSE');
  let cur = ep.t_end_s, prevShot = null;
  const steps = ep.steps || [];
  if (!steps.length) push(cur, 'DECIDE', {none: true});
  steps.forEach((s, i) => {
    if (!isCapture(s)) {
      push(prevShot == null ? cur : prevShot + RECORD_S, s.action === 'WAIT' ? 'WAIT' : 'DECIDE', {step: i, pose: s.pose});
      cur = marks[marks.length - 1].t;
      return;
    }
    const shot = Math.max(s.t_s, cur);
    const mv = moves.find(x => s.action_id && x.action_id === s.action_id) ||
      moves.find(x => !x.action_id && x.pose === s.pose && x.t0_s >= cur - 1 && x.t0_s < shot);
    let moveAt = null, captureAt;
    if (mv) {
      moveAt = Math.min(Math.max(cur, mv.t0_s), shot);
      captureAt = mv.kind === 'MOVE' && !mv.end_unknown ? Math.min(Math.max(mv.t1_s, moveAt), shot) : Math.max(moveAt, shot - CAPTURE_S);
    } else {
      captureAt = Math.max(cur, shot - CAPTURE_S); // no move record: the arm was already there, or unknown
    }
    const first = moveAt ?? captureAt;
    const decideAt = prevShot == null ? cur : Math.min(prevShot + RECORD_S, (prevShot + first) / 2);
    push(decideAt, 'DECIDE', {step: i, pose: s.pose});
    if (moveAt != null && captureAt > moveAt) push(moveAt, 'MOVE', {step: i, pose: s.pose});
    push(captureAt, 'CAPTURE', {step: i, pose: s.pose, estimated: !mv});
    push(shot, 'RESULT', {step: i, pose: s.pose});
    cur = prevShot = shot;
  });
  const last = marks[marks.length - 1];
  const park = spans.find(x => x.kind === 'PARK' && x.t0_s >= last.t && x.t0_s - last.t <= PARK_WITHIN_S);
  if (park && prevShot != null) {
    push(Math.max(park.t0_s, last.t), 'PARK', {pose: 'FRONT'});
    push(Math.max(park.t1_s, park.t0_s, last.t), 'END');
  } else if (last.kind === 'WAIT' || last.kind === 'DECIDE') {
    push(last.t + WAIT_SHOW_S, 'END');
  }
  for (let i = 1; i < marks.length; i++) marks[i].t = Math.max(marks[i].t, marks[i - 1].t); // keep order
  return marks;
}

/** The mark in force at AD3 time t (null = still watching, before the candidate). */
export function markAt(marks, t) {
  let found = null;
  for (const m of marks) { if (m.t <= t) found = m; else break; }
  return found;
}
export function stagesOf(mark) { return mark ? STAGES_OF[mark.kind] : []; }

// ---- image AI (Vision V2 classifier) as shown on the demo: the model's top class and raw score ----
// The demo shows the model's answer to tell the concept (user decision 2026-11-20). The score is the
// model's raw 0-100 output, shown as 確度; the records keep presence UNKNOWN.
export function aiOf(step) {
  const a = step?.ai;
  return a && a.class_name && a.score != null ? {seen: a.class_name === 'VISIBLE_LARVA', score: a.score, recipe: a.recipe} : null;
}
export function aiText(ai) { return ai ? (ai.seen ? `幼虫が見えた（確度 ${ai.score}）` : `幼虫は見えない（確度 ${ai.score}）`) : ''; }
/** Stages already passed in the current decide cycle (directive §3.2: finished = quiet mark, future = dim). */
export function doneStages(marks, mark) {
  const i = marks.indexOf(mark); if (i < 0) return [];
  let start = i; while (start > 0 && !['DECIDE', 'WAIT', 'SENSE'].includes(marks[start].kind)) start--;
  const on = new Set(stagesOf(mark)), out = new Set();
  for (let k = start; k < i; k++) for (const n of stagesOf(marks[k])) if (!on.has(n)) out.add(n);
  if (start > 0 && marks[start].kind !== 'SENSE') out.add(1);  // the cycle began from the vibration
  for (const n of on) out.delete(n);
  return [...out];
}
/** One line for the end of an episode: where the image AI saw the larva, or that it did not. */
export function episodeVerdict(ep) {
  const shots = (ep.steps || []).filter(s => s.capture === 'SUCCESS'), ais = shots.map(s => ({s, ai: aiOf(s)})).filter(x => x.ai);
  if (!ais.length) return null;
  const seen = ais.filter(x => x.ai.seen);
  if (seen.length) { const best = seen.reduce((a, b) => (b.ai.score > a.ai.score ? b : a));
    // 0.17.0: the episode ended right after the image where the larva was seen
    const tries = (ep.steps || []).filter(s => String(s.action || '').startsWith('CAPTURE_'));
    const skipped = tries.length < 3 && aiOf(tries[tries.length - 1])?.seen ? '。見えたので、残りの方向は撮りませんでした' : '';
    return {seen: true, text: `画像AI：${seen.map(x => POSE_JA[x.s.pose] || x.s.pose).join('・')}からの写真に幼虫が写っていると判定（確度 ${best.ai.score}）${skipped}`}; }
  return {seen: false, text: `画像AI：${ais.length} 方向とも幼虫は見えないと判定`};
}

// ---- which source started the observation (display only) ----
// Until 2026-11-22 every trigger was a B2 candidate (300-1500 Hz); on 2026-11-23 the 1-40 Hz activity
// (forest or ratio >= 3); from 2026-11-24 either (activity_trigger.py). A trigger names what started
// it in triggered_by; older records without it were B2. A trigger is not a larva detection.
export const TRIGGER_JA = {FOREST: 'フォレスト', RATIO_3: '比', B2: 'B2'};
/** x: an episode's trigger ({detector, by, ...}) or a raw candidate ({detector, triggered_by, ...}). */
export function triggerOf(x) {
  const t = x?.trigger || x;
  if (!t || !(t.detector || t.by || t.triggered_by)) return null;  // e.g. the empty scene between episodes
  const detector = t.detector || 'B2_guarded_mad', named = t.by || t.triggered_by || [];
  const by = named.length ? named : detector === 'B2_guarded_mad' ? ['B2'] : [];
  if (!by.length) return null;
  const b2 = by.includes('B2');
  return {by, band: b2 ? 'band' : 'low', short: by.map(b => TRIGGER_JA[b] || b).join('＋'),
    forest_p: t.forest_p ?? null, ratio: t.ratio ?? null, b2_score: t.b2_score ?? (b2 ? t.peak_score ?? null : null)};
}
/** One line for the clue card: what started this observation, with its number. */
export function triggerText(x) {
  const tr = triggerOf(x);
  if (!tr) return '';
  if (tr.band === 'band') return `きっかけ：B2（300–1500 Hz の短い振動${tr.b2_score != null ? `・スコア ${tr.b2_score.toFixed(1)}` : ''}）`;
  const parts = tr.by.map(b => b === 'FOREST' ? `フォレスト${tr.forest_p != null ? ` 確率 ${tr.forest_p.toFixed(2)}` : ''}` :
    b === 'RATIO_3' ? `比${tr.ratio != null ? ` ×${tr.ratio.toFixed(1)}` : ' ≥3'}` : b);
  return `きっかけ：1–40 Hz の低周波（${parts.join('・')}）`;
}

// ---- wording (user-facing first; codes go to the small technical line) ----
const VIEW = () => globalThis.BllObservationView;
const where = pose => (POSE_JA[pose] || pose || '?') + (pose === 'FIXED' ? '' : 'から');
const WAIT_WORDS = [
  ['SELF_ACTUATION_RECENT', 'カメラを動かした直後なので、少し待っています'],
  ['DEVICE_BUSY', 'いま撮影中なので、次の観察を待っています'],
  ['ACTION_IN_PROGRESS', 'いま撮影中なので、次の観察を待っています'],
  ['ALL_VIEWS_RECENTLY_CHECKED', 'どの方向も最近確認したので、次の変化を待ちます'],
  ['VISION_REPEATEDLY_FAILED', '撮影が続けて失敗したため、いったん待機します'],
  ['LOW_LIGHT', '撮影しづらい明るさなので、確認を保留します'],
  ['CAMERA_COOLDOWN_ACTIVE', 'カメラを休ませる時間なので、待っています'],
  ['EPISODE_BUDGET_EXHAUSTED', 'この揺れで撮れる回数を使い切ったので、待ちます'],
  ['LARVA_VISIBLE_STOP', '幼虫が見えたので、残りの方向は撮りません'],
  ['TRIGGER_STALE', '少し前の揺れなので、今は撮りません'],
  ['SAFETY_BLOCKED', '安全のため、動かさずに待ちます'],
];
// 0.18.0: a look again where the image AI last saw the larva (its judgement, not a verified detection).
export const isLarvaStay = step => (step?.reason_codes || []).includes('LARVA_LAST_SEEN_HERE');
/** The stay needs no arm move: the first view of an episode at FRONT, where every episode parks,
 *  or any view in hold mode (0.18.2: the arm waited there; ep.hold is set by the page).
 *  Shown as a decision ("見えている場所では動かない"), so a still arm does not read as doing nothing. */
export function staysPut(step, ep = null) {
  return isLarvaStay(step) && (step.pose === 'FRONT' || !!ep?.hold) && (!ep?.steps || ep.steps[0] === step);
}
/** ep (optional): the episode, so a follow-up after a look where the larva was seen can say it was lost. */
export function whyText(step, ep = null) {
  if (!step) return '';
  const codes = step.reason_codes || [];
  if (step.action !== 'WAIT' && !String(step.action || '').startsWith('CAPTURE_')) return '判断なし';
  if (step.action === 'WAIT') return (WAIT_WORDS.find(([c]) => codes.includes(c)) || [0, 'いまは撮影せずに待ちます'])[1];
  const p = step.pose;
  if (staysPut(step, ep)) return `前回幼虫が見えた${where(p)}、アームを動かさずにもう一度確認します`;
  if (isLarvaStay(step)) return `前回幼虫が見えた${where(p)}、もう一度確認します`;
  const first = ep?.steps?.[0];
  if (codes.includes('EPISODE_FOLLOW_UP') && first !== step && (first?.reason_codes || []).includes('LARVA_LAST_SEEN_HERE') && !aiOf(first)?.seen)
    return `${where(first.pose)}は見えなくなったので、${where(p)}探します`;
  if (codes.includes('PREVIOUS_ATTEMPT_FAILED')) return `前回は撮影できなかったので、${where(p)}撮り直します`;
  if (codes.includes('EPISODE_FOLLOW_UP')) return `同じ揺れの続きとして、${where(p)}も確認します`;
  if (codes.includes(`${p}_NOT_RECENTLY_OBSERVED`)) return `${where(p)}はしばらく見ていないので、${where(p)}確認します`;
  return `新しい振動の反応があったので、${where(p)}確認します`;
}
export function techText(step) {
  if (!step) return '';
  const v = VIEW();
  return `${v ? v.actionText(step.action) : step.action}（${step.action}）／ 理由コード ${(step.reason_codes || []).join(', ') || 'なし'}`;
}
const NOT_SENT = {NOT_FORWARDED: 'この記録では観察係（MCU）に送っていません', not_sent_capture_failed: '撮影の失敗が続いていたため、観察係には送りませんでした',
  sending: '観察係の返事がありません', failed: '観察係に届きませんでした'};
export function clueText(ep, mark) {
  if (!mark) return {main: '見守り中：振動の反応を待っています', sub: '', trigger: ''};
  const rise = ep.ch2_x == null ? '' : `ケースの中 ×${ep.ch2_x < 10 ? ep.ch2_x.toFixed(1) : Math.round(ep.ch2_x)}` +
    (ep.ch1_x == null ? '' : ` ／ ケースの外 ×${ep.ch1_x < 10 ? ep.ch1_x.toFixed(1) : Math.round(ep.ch1_x)}`) + '（直前5秒との比）';
  let arm = '';
  if (ep.arm) arm = ep.arm.relation === 'DURING' ? 'アームが動いている間の揺れです（自分の動きの音の可能性）' :
    `アームが止まって ${ep.arm.seconds} 秒後の揺れです（自分の動きの音の可能性）`;
  return {main: mark.kind === 'SENSE' ? '振動の反応があります' : '振動の反応がありました', sub: [rise, arm].filter(Boolean).join('\n'),
    trigger: triggerText(ep)};
}
export function resultText(ep, mark) {
  if (!mark) return '—';
  const step = mark.step != null ? (ep.steps || [])[mark.step] : null;
  switch (mark.kind) {
    case 'SENSE': return '揺れの区間を記録しています';
    case 'DECIDE': return mark.pending ? '観察係（MCU）が、これまでの記録から次の行動を選んでいます' : mark.none ? (NOT_SENT[ep.delivery] || `観察係の返事：${ep.delivery}`) :
      isCapture(step) ? (staysPut(step, ep) ? `このまま${where(mark.pose)}撮影します（アームは動かしません）` : `次は${where(mark.pose)}撮影します`) : '判断を記録しました';
    case 'WAIT': return '撮影せずに待機しました（理由も記録）';
    case 'MOVE': return `アームを${POSE_JA[mark.pose] || mark.pose}へ動かしています`;
    case 'CAPTURE': return staysPut(step, ep) ? `アームはそのまま、${where(mark.pose)}撮影しています` : `${where(mark.pose)}撮影しています`;
    case 'RESULT': return step?.capture === 'SUCCESS' ? (aiOf(step) ? `${where(mark.pose)}の写真を記録 → 画像AI：${aiText(aiOf(step))}` : `${where(mark.pose)}の写真を記録しました`) :
      `撮影できませんでした${step?.capture_reason ? `（${step.capture_reason}）` : ''}。いない、という意味ではありません`;
    case 'PARK': return episodeVerdict(ep)?.text || '観察を終えて、アームを基本位置（前）へ戻しています';
    case 'END': return episodeVerdict(ep)?.text || '見守りに戻りました';
  }
  return '';
}
/** Short label of an episode for the picker. */
export function episodeLabel(ep, index, clock) {
  const steps = ep.steps || [], caps = steps.filter(s => String(s.action || '').startsWith('CAPTURE_'));
  const kind = caps.length ? `撮影 ${caps.length} 方向（${caps.map(s => POSE_JA[s.pose] || s.pose).join('→')}）` :
    steps.some(s => s.action === 'WAIT') ? 'あえて待機（WAIT）' : '判断なし';
  const tr = triggerOf(ep), stay = isLarvaStay(caps[0]) ? (staysPut(caps[0], ep) ? '  ・ 動かずに確認' : '  ・ 見えた方向へ') : '';
  return `#${index + 1}  ${clock(ep.t_start_s)}  ${kind}${stay}${tr ? `  ・ きっかけ ${tr.short}` : ''}`;
}
