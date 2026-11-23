// デモ再生（/demo）: the "memory" view - the Evidence history of the run and the latest state
// rebuilt from it (REMEMBER / Evidence・Reducer). Shown in the right-top panel around a decision
// (proposal A) and in the state diagram's info panel at ②③ (proposal B).
//
// Display only. The history is the run's own records (candidates, the XIAO's decisions with their
// reason codes, capture results, arm moves) on the AD3 time axis; decision times between records are
// the timeline's estimates. The state is a count/age summary of those records for display - it is
// NOT the XIAO's internal state and nothing here decides anything (the decision logic lives only in
// observation_core.h). Which state a decision used comes from the XIAO's own reason codes.
import {POSE_JA, buildTimeline} from './timeline.js';

const POSES = ['FRONT', 'TOP', 'BACK'];
const isCapture = s => String(s?.action || '').startsWith('CAPTURE_');

/** Run history: [{t, kind: SENSE|DECIDE|WAIT|RESULT|PARK, seq, pose, ok, codes, action}] in time order. */
export function buildHistory(episodes, actuation = []) {
  const items = [];
  for (const ep of episodes || []) {
    const steps = ep.steps || [];
    for (const m of buildTimeline(ep, actuation)) {
      const step = m.step != null ? steps[m.step] : null;
      if (m.kind === 'SENSE') items.push({t: m.t, kind: 'SENSE', seq: ep.candidate_seq});
      else if ((m.kind === 'DECIDE' || m.kind === 'WAIT') && step) items.push({t: m.t, kind: m.kind, seq: ep.candidate_seq,
        pose: step.pose, action: step.action, codes: step.reason_codes || [], estimated: true});
      else if (m.kind === 'RESULT' && step) items.push({t: m.t, kind: 'RESULT', seq: ep.candidate_seq, pose: step.pose,
        ok: step.capture === 'SUCCESS'});
    }
  }
  for (const a of actuation) if (a.kind === 'PARK') items.push({t: a.t0_s, kind: 'PARK'});
  return items.sort((x, y) => x.t - y.t);
}

/** Count/age summary at time t (display only). ep/mark: the episode and mark on screen. */
export function stateAt(history, actuation, t, ep, mark) {
  const past = history.filter(h => h.t <= t + 1e-6);
  const views = {};
  for (const p of POSES) {
    const last = [...past].reverse().find(h => h.kind === 'RESULT' && h.pose === p);
    views[p] = last ? {age: t - last.t, ok: last.ok} : null;
  }
  let failures = 0;
  for (const h of [...past].reverse()) { if (h.kind !== 'RESULT') continue; if (h.ok) break; failures++; }
  const moving = actuation.find(a => a.t0_s <= t && t <= Math.max(a.t1_s, a.t0_s));
  const stopped = actuation.filter(a => Math.max(a.t1_s, a.t0_s) <= t).sort((a, b) => b.t1_s - a.t1_s)[0];
  const steps = ep?.steps || [];
  const done = past.filter(h => h.kind === 'RESULT' && h.seq === ep?.candidate_seq).length;
  return {
    views, failures,
    arm: moving ? {moving: true, pose: moving.pose, kind: moving.kind} : stopped ? {age: t - Math.max(stopped.t1_s, stopped.t0_s)} : null,
    busy: !!mark && ['MOVE', 'CAPTURE'].includes(mark.kind),
    episode: ep?.candidate_seq != null ? {seq: ep.candidate_seq, done, planned: steps.filter(isCapture).length} : null,
    decisions: past.filter(h => h.kind === 'DECIDE' || h.kind === 'WAIT').length,
    waits: past.filter(h => h.kind === 'WAIT').length,
  };
}

/** The reason codes of the decision in force (the XIAO's own), or []. */
export function codesOf(history, t, seq) {
  const d = [...history].reverse().find(h => (h.kind === 'DECIDE' || h.kind === 'WAIT') && h.t <= t + 1e-6 && h.seq === seq);
  return d ? {codes: d.codes, pose: d.pose, action: d.action} : {codes: [], pose: null, action: null};
}

const sec = s => (s < 10 ? s.toFixed(1) : Math.round(s)) + '秒前';
function itemText(h) {
  const where = p => (POSE_JA[p] || p) + 'から';
  switch (h.kind) {
    case 'SENSE': return ['vibration', `振動候補 #${h.seq} を保存`];
    case 'DECIDE': return ['alt_route', `判断：${where(h.pose)}撮る`];
    case 'WAIT': return ['hourglass_top', '判断：待つ（撮らない）'];
    case 'RESULT': return [h.ok ? 'photo_camera' : 'error', `撮影 ${POSE_JA[h.pose] || h.pose}：${h.ok ? '成功' : '失敗'}`];
    case 'PARK': return ['home', 'アームを基本位置へ'];
  }
  return ['circle', h.kind];
}

/** Render into box. opts: {history, state, used: codesOf(), t, clock, rows, compact (no summary cards)} */
export function renderMemory(box, {history, state, used, t, clock, rows = 6, compact = false}) {
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const icon = name => el('span', 'ms', name);
  const past = history.filter(h => h.t <= t + 1e-6), shown = past.slice(-rows), codes = used.codes || [];
  const hist = el('ol', 'mhist');
  if (!shown.length) hist.append(el('li', 'empty', 'まだ記録はありません（見守り中）'));
  shown.forEach((h, i) => {
    const [ic, text] = itemText(h), li = el('li', h.kind.toLowerCase() + (i === shown.length - 1 ? ' now' : ''));
    li.append(el('span', 'mt', clock(h.t)), icon(ic), el('span', 'mx', text));
    if (t - h.t < 1.5) li.append(el('b', 'saved', '保存'));
    hist.append(li);
  });
  const cards = el('div', 'mstate');
  const card = (cls, name, value, tag, star) => {
    const c = el('div', 'mcard ' + cls + (tag ? ' used' : '') + (star ? ' key' : ''));
    c.append(el('b', null, name), el('span', 'mv', value));
    if (tag) c.append(el('span', 'mtag', (star ? '★ ' : '◀ ') + tag));
    cards.append(c);
  };
  for (const p of POSES) {
    const v = state.views[p], recent = codes.includes(`${p}_RECENTLY_CHECKED`), notRecent = codes.includes(`${p}_NOT_RECENTLY_OBSERVED`);
    const value = v ? (v.ok ? `✓ ${sec(v.age)}に撮影` : `× ${sec(v.age)}に失敗`) : '― まだ見ていない';
    card('pose', POSE_JA[p] + 'から', value, recent ? '最近見た' : notRecent ? 'しばらく見ていない' : '',
      notRecent && used.pose === p && used.action !== 'WAIT');
  }
  const a = state.arm;
  card('arm', 'アーム', a ? (a.moving ? `動作中（→${POSE_JA[a.pose] || a.pose || '基本位置'}）` : `${sec(a.age)}に停止`) : '記録なし',
    codes.includes('SELF_ACTUATION_RECENT') ? '動かした直後（自分の揺れ）' : '', codes.includes('SELF_ACTUATION_RECENT'));
  const camCodes = ['DEVICE_BUSY', 'ACTION_IN_PROGRESS', 'VISION_REPEATEDLY_FAILED', 'PREVIOUS_ATTEMPT_FAILED'].filter(c => codes.includes(c));
  // the XIAO's own reason (DEVICE_BUSY: still working on an earlier observation) wins over the display count
  const busy = state.busy || codes.includes('DEVICE_BUSY') || codes.includes('ACTION_IN_PROGRESS');
  card('cam', 'カメラ', `${busy ? '使用中' : '空き'} ・ 連続失敗 ${state.failures} 回`,
    camCodes.length ? {DEVICE_BUSY: '前の観察がまだ動作中', ACTION_IN_PROGRESS: '前の観察がまだ動作中', VISION_REPEATEDLY_FAILED: '失敗が続いた', PREVIOUS_ATTEMPT_FAILED: '前回失敗'}[camCodes[0]] : '',
    camCodes.length > 0 && used.action === 'WAIT');
  if (state.episode) card('ep', 'この揺れ', `撮影 ${state.episode.done}/${state.episode.planned} 方向`, codes.includes('EPISODE_FOLLOW_UP') ? '同じ揺れの続き' : '', false);
  if (!compact) {
    card('tot', 'この記録', `判断 ${state.decisions} 回 ・ 待機 ${state.waits} 回`, '', false);
    if (codes.includes('LIGHT_UNKNOWN')) card('light', '明るさ', 'センサーなし（UNKNOWN）', '', false);
  }
  box.replaceChildren(hist, cards);
}
