// デモ再生（/demo）: a 1920x1080 stage for the contest video
// (docs/Beetle_Life_Lab_ProtoPedia_Presentation_Design_20261115.md §5). Two sources:
// - 記録の再生 (default): one saved episode replayed on the run's own time axis - the OBS
//   recording, AD3 band RMS, the XIAO's decisions and the Vision V2 images (timeline.js).
// - 実機のライブ: a camera of this PC (e.g. the USB camera, or OBS Virtual Camera) with the
//   bridge's current acquisition state (/api/state, live.js). Only this mode says LIVE.
// Read-only in both: it never starts or stops hardware. Larva presence/activity stays UNKNOWN.
// The image AI's answer (class and raw score, shown as 確度) is displayed to tell the concept
// (user decision 2026-11-20); it is display only and the records keep presence UNKNOWN.
import {liveState} from './live.js';
import {FLOW_NODES, FLOW_TEXT, buildFlow, flowPath, flowTimes, paintFlow} from './flow.js';
import {buildHistory, codesOf, renderMemory, stateAt} from './memory.js';
import {orient} from '../common/orient.js';
import {statusPrefix} from '../common/review_status.js';
import {POSE_ICON, POSE_JA, RESULT_ICON, STEPS, aiOf, aiText, buildTimeline, doneStages, episodeVerdict, clueText, episodeLabel, markAt, resultText, stagesOf, techText, whyText} from './timeline.js';

const $ = id => document.getElementById(id);
const ANSWER = {LARVA: '幼虫活動', DIG: '人工刺激（掘削）', NOISE: '外乱（声・物音）'};
// Human photo labels of 観察ノート (shown apart from the image AI: other colour, own heading).
const HUMAN = {larva_visible: '幼虫が見える', background: '幼虫は写っていない', uncertain: 'はっきりしない', unusable: '使えない画像'};
const humanOf = step => { const l = step?.action_id && S.data?.human_labels?.[step.action_id]; return l && HUMAN[l] ? {label: l, text: HUMAN[l]} : null; };
const S = {mode: 'replay', run: null, ov: null, data: null, ep: null, marks: [], clip: [0, 1], t: 0, playing: false, useVideo: false,
  offset: 0, last: 0, key: null, ymax: 1, ymin: 0, ready: false, wave: 'low',
  live: null, liveHeld: null, liveBusy: false, stream: null, liveTimer: null};
const video = $('video');

function icon(name, cls = '') { return el('span', ('ms ' + cls).trim(), name); }
function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function clock(t) { if (t == null || !isFinite(t)) return '—'; const m = Math.floor(t / 60), s = t - m * 60; return `${m}:${s.toFixed(1).padStart(4, '0')}`; }
async function getJSON(url) { const r = await fetch(url); const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || r.statusText); return j; }
const jstOf = ms => new Date(ms).toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo', hour12: false}) + ' JST';
function jst(t) {
  const start = Date.parse(S.data?.started_at_pc || '');
  return isFinite(start) ? jstOf(start + t * 1000) + '（推定）' : '';
}

// ---- stage scaling: the stage is laid out at 1920x1080 and scaled to the window ----
function fit() {
  const box = $('viewport').getBoundingClientRect();
  $('stage').style.transform = `scale(${Math.min(box.width / 1920, box.height / 1080)})`;
}

// ---- navigator ----
function buildNav() {
  $('nav').replaceChildren(...STEPS.map(s => {
    const li = el('li', s.tone); li.dataset.n = s.n;
    li.append(el('span', 'n', `0${s.n}`), icon(s.icon, 'step'), el('span', 'l', s.label), el('span', 't', s.tech));
    return li;
  }));
}
function paintNav(mark) {
  const on = stagesOf(mark), steps = S.ep?.steps || [], caps = steps.filter(s => String(s.action || '').startsWith('CAPTURE_'));
  const past = S.mode === 'replay' ? doneStages(S.marks || [], mark) : [];
  for (const li of $('nav').children) {
    const n = +li.dataset.n, base = STEPS[n - 1].tone;
    li.className = base + (on.includes(n) ? ' on' : past.includes(n) ? ' past' : '') + (mark?.kind === 'WAIT' && n === 3 ? ' wait' : '');
    li.querySelector('.badge')?.remove();
    let badge = null;
    if (on.includes(n) && mark.kind === 'WAIT' && n === 3) badge = '待機';
    else if (on.includes(n) && mark.kind === 'PARK') badge = '基本位置へ';
    else if (on.includes(n) && mark.step != null && caps.length > 1 && n >= 3) badge = `${caps.indexOf(steps[mark.step]) + 1}/${caps.length}方向`;
    if (badge) { const b = el('span', 'badge'); b.append(icon(mark.kind === 'WAIT' ? 'hourglass_top' : mark.kind === 'PARK' ? 'home' : 'photo_camera'), badge); li.append(b); }
  }
}

// ---- state-transition diagram (click the navigator; directive §3.3) ----
let flowMark = null;
function flowOpen() { return !$('flow').hidden; }
function toggleFlow(show = !flowOpen()) {
  if (!show && document.body.classList.contains('flowdock')) setDock(false);
  $('flow').hidden = !show;
  if (show) paintFlowFor(flowMark);
}
function paintFlowFor(mark) {
  flowMark = mark;
  if (!flowOpen()) return;
  const marks = S.mode === 'replay' ? (S.marks || []) : mark ? [mark] : [];
  const path = flowPath(marks, mark), cur = path[path.length - 1];
  paintFlow($('flowSvg'), path);
  $('flowNow').textContent = 'いま：' + FLOW_NODES[cur].label;
  $('flowText').textContent = FLOW_TEXT[cur];
  const step = mark && mark.kind !== 'SENSE' ? stepOf(mark) : null;
  $('flowWhy').textContent = (cur === 'DEC' || cur === 'WAIT') && step ? `分岐：${step.action === 'WAIT' ? 'WAIT（待つ）' : 'CAPTURE（撮る）'}\n理由：${whyText(step)}` :
    cur === 'FOL' ? '分岐：同じ揺れの続きを見るか、終えるか' :
    cur === 'REC' && step ? resultText(S.ep, mark) : step && !['MON', 'SEN'].includes(cur) ? `この観察：${whyText(step)}` : '';
  const steps = S.ep?.steps || [], done = marks.filter(m => m.kind === 'RESULT' && (!mark || m.t <= mark.t)).length;
  const caps = steps.filter(s => String(s.action || '').startsWith('CAPTURE_')).length, waits = steps.filter(s => s.action === 'WAIT').length;
  $('flowCount').textContent = mark ? `この場面：撮影 ${done}/${caps} 方向${waits ? ` ・ 待機 ${waits} 回` : ''}` : '';
}
// ↓ / ↑: dock the diagram below the video (title row, cards and footnote hidden) or float it again.
function setDock(on) {
  document.body.classList.toggle('flowdock', on);
  const f = $('flow');
  if (on) { $('stage').append(f); f.hidden = false; } else document.querySelector('.middle').append(f);
  // docked: the memory panel stays beside the diagram (third column) instead of switching the graph panel
  const mem = $('memPanel');
  if (on) document.querySelector('.fbody').append(mem); else document.querySelector('.side').prepend(mem);
  document.querySelector('.side').classList.remove('mem');
  $('flowDock').textContent = on ? '↑' : '↓';
  $('flowDock').title = on ? '元の表示に戻す（図を重ねて表示）' : '映像と並べて下に表示（上の見出しと下のカードを隠す）';
  try { localStorage.setItem('bll.demo.flowdock', on ? '1' : ''); } catch (_) {}
  S.key = null; memKey = flowMemKey = null; paintFlowFor(flowMark);
}
function flowJump(id) {
  if (S.mode !== 'replay' || !S.marks?.length) return;
  const t = id === 'MON' ? S.clip[0] : flowTimes(S.marks)[id];
  if (t != null) seek(t);
}

// ---- panels ----
function stepOf(mark) {
  const steps = S.ep?.steps || [];
  if (mark?.step != null) return steps[mark.step];
  if (mark && (mark.kind === 'PARK' || mark.kind === 'END')) return steps[steps.length - 1];
  return null;
}
function paintCards(mark) {
  const ep = S.ep, clue = clueText(ep, mark), step = mark && mark.kind !== 'SENSE' ? stepOf(mark) : null;
  $('clueMain').textContent = clue.main; $('clueIcon').textContent = mark ? 'vibration' : 'visibility';
  $('clueSub').textContent = clue.sub;
  const human = mark && ep.answers?.length ? `人の振り返り：${ep.answers.map(a => ANSWER[a] || a).join('・')}` : '';
  $('clueHuman').hidden = !human; $('clueHuman').textContent = human;
  $('whyMain').textContent = step ? whyText(step) : mark?.none ? '判断なし' : mark?.pending ? '判断しています…' : '—';
  $('whySub').textContent = step ? '決まったルールで選びました（学習済みAIではありません）' : '';
  $('whyTech').textContent = step ? techText(step) : '';
  document.querySelector('.card.why').classList.toggle('waiting', mark?.kind === 'WAIT');
  $('whyIcon').textContent = step ? (step.action === 'WAIT' ? 'hourglass_top' : 'alt_route') : mark?.pending ? 'alt_route' : '';
  $('resultMain').textContent = resultText(ep, mark);
  const failed = mark?.kind === 'RESULT' && step?.capture !== 'SUCCESS';
  const ai = mark?.kind === 'RESULT' ? aiOf(step) : null, verdict = mark && ['PARK', 'END'].includes(mark.kind) ? episodeVerdict(ep) : null;
  const seen = ai ? ai.seen : verdict?.seen;
  $('resultIcon').textContent = failed ? 'error' : seen ? 'pest_control' : RESULT_ICON[mark?.kind] || 'visibility';
  const sub = $('resultSub');
  sub.className = 'sub' + (ai || verdict ? ' ai' : '') + (seen ? ' seen' : '');
  sub.textContent = ai ? `画像AI（${ai.recipe || '分類モデル'}）の判定です。幼虫が見えたら、残りの方向は撮りません` :
    verdict ? '写真と判断の経緯を記録しました（観察日記に残ります）' : '';
  // a human label of the photo just taken (or the episode's labelled photos at the end)
  const labelled = mark?.kind === 'RESULT' ? [step].filter(humanOf) : verdict ? (ep.steps || []).filter(humanOf) : [];
  const hl = labelled.map(s => `${POSE_JA[s.pose] || s.pose}：${humanOf(s).text}`);
  $('resultHuman').hidden = !hl.length; $('resultHuman').textContent = hl.length ? `人が写真を確認 → ${hl.join(' ／ ')}` : '';
  const arm = $('armState'), where = POSE_JA[mark?.pose] || mark?.pose;
  const text = {MOVE: `アーム移動中 → ${where}`, CAPTURE: `撮影中（${where}）`, PARK: 'アーム：基本位置（前）へ戻る'}[mark?.kind];
  arm.hidden = !text; arm.replaceChildren(...(text ? [icon(RESULT_ICON[mark.kind]), text] : []));
}
/** done: indexes of steps whose result is already shown. */
function paintShots(done, mark) {
  const steps = S.ep?.steps || [], shown = steps.filter((s, i) => done.has(i) && s.image_url);
  // At the end of an episode, the photo where the image AI saw the larva (highest score) stays in front.
  const found = mark && ['PARK', 'END'].includes(mark.kind) ? shown.filter(s => aiOf(s)?.seen).sort((x, y) => aiOf(y).score - aiOf(x).score)[0] : null;
  const latest = found || shown[shown.length - 1], img = $('shotImg');
  img.hidden = !latest; $('shotEmpty').hidden = !!latest;
  if (latest && img.getAttribute('src') !== latest.image_url) img.src = latest.image_url;
  orient(img, latest?.pose);
  const lai = aiOf(latest);
  $('shotCap').textContent = latest ? `${POSE_JA[latest.pose] || latest.pose}から${latest.t_s != null ? ' ・ ' + clock(latest.t_s) : ''}` +
    (lai ? ` ・ 画像AI（${lai.recipe || 'Vision V2'}）` : '') : '';
  $('shotAi').hidden = !lai; $('shotAi').className = 'aibadge' + (lai?.seen ? ' seen' : '');
  if (lai) $('shotAi').textContent = lai.seen ? `幼虫が見えた　確度 ${lai.score}` : `見えない　確度 ${lai.score}`;
  $('shotCap').hidden = !latest;
  const poses = ['FRONT', 'TOP', 'BACK', ...(steps.some(s => s.pose === 'FIXED') ? ['FIXED'] : [])];
  $('views').replaceChildren(...poses.map(p => {
    const mine = steps.map((s, i) => ({s, i})).filter(x => x.s.pose === p && String(x.s.action || '').startsWith('CAPTURE_'));
    const finished = mine.filter(x => done.has(x.i));
    // Live: the arm lines name the pose before the step's result exists.
    const now = mark && ['DECIDE', 'MOVE', 'CAPTURE'].includes(mark.kind) &&
      (mark.step != null ? mine.find(x => x.i === mark.step) : mark.pose === p && !finished.length ? {} : null);
    const last = finished[finished.length - 1]?.s, row = el('div', 'view');
    const skipped = !mine.length && steps.some(s => aiOf(s)?.seen) && steps.some(s => String(s.action || '').startsWith('CAPTURE_'));
    let state = mine.length ? '未取得' : skipped ? '幼虫が見えたので省略' : S.mode === 'live' ? '' : 'この場面では撮影なし';
    if (now) { row.classList.add('now'); state = {DECIDE: '次に見る方向', MOVE: 'アーム移動中', CAPTURE: '撮影中'}[mark.kind]; }
    else if (last) { const ok = last.capture === 'SUCCESS'; row.classList.add(ok ? 'ok' : 'fail'); state = ok ? `撮影済${last.t_s != null ? ' ' + clock(last.t_s) : ''}` : '撮影失敗'; }
    const thumb = last?.image_url ? orient(Object.assign(el('img'), {src: last.image_url, alt: ''}), p) : el('div', 'ph');
    const sign = now ? (mark.kind === 'CAPTURE' ? 'photo_camera' : mark.kind === 'MOVE' ? 'open_with' : 'radio_button_checked') :
      last ? (last.capture === 'SUCCESS' ? 'check_circle' : 'error') : mine.length ? 'schedule' : 'remove';
    // two lines per view: name + state, then the image AI (reference) and the human label side by side
    const name = el('b'); name.append(icon(POSE_ICON[p]), `${POSE_JA[p]}${p === 'FIXED' ? '' : 'から'}`);
    const st = el('small', 'st'); st.append(icon(sign), state); name.append(st);
    const text = el('div'); text.append(name);
    const vai = !now && aiOf(last), vh = !now && humanOf(last);
    if (vai || vh) {
      const line = el('small', 'res');
      if (vai) { const a = el('span', 'ai' + (vai.seen ? ' seen' : '')); a.append(icon(vai.seen ? 'pest_control' : 'visibility_off'), `AI：${vai.seen ? '見えた' : '見えない'} ${vai.score}`); line.append(a); }
      if (vh) { const h = el('span', 'human'); h.append(icon('person'), '人：' + vh.text); line.append(h); }
      text.append(line);
    }
    row.append(thumb, text); return row;
  }));
}

// ---- spotlight (directive UI-04): enlarge the photo on the right for a few seconds after a capture
// whose photo shows the larva (image AI reference or a human label). Display only; times stay the log's.
const SPOT_S = 4;
function spotlight(t) {
  const marks = S.mode === 'replay' ? (S.marks || []) : [];
  const m = marks.filter(x => x.kind === 'RESULT' && x.t <= t && t - x.t < SPOT_S).pop();
  // proposal A: around a decision (②③) the graph panel shows the memory for a few seconds;
  // a newer decision takes over from the photo spotlight of the capture before it
  const d = marks.filter(x => (x.kind === 'DECIDE' || x.kind === 'WAIT') && x.t <= t && t - x.t < MEM_S).pop();
  const mem = !!d && (!m || d.t >= m.t) && !document.body.classList.contains('flowdock');
  const step = m && !mem ? (S.ep?.steps || [])[m.step] : null;
  const on = !!(step?.image_url && (aiOf(step)?.seen || humanOf(step)?.label === 'larva_visible'));
  const side = document.querySelector('.side');
  side.classList.toggle('spot', on);
  side.classList.toggle('mem', mem);
}
const MEM_S = 4;
let memKey = null, flowMemKey = null;
/** Memory content for the right-top panel and the diagram's ②③ detail (display only). */
function paintMemory(t, mark) {
  if (S.mode !== 'replay' || !S.history) return;
  const ep = S.ep, args = () => ({history: S.history, state: stateAt(S.history, S.ov?.actuation || [], t, ep, mark),
    used: codesOf(S.history, t, ep?.candidate_seq), t, clock});
  const key = `${S.run}|${ep?.candidate_seq}|${mark?.kind}|${Math.floor(t)}`;
  const docked = document.body.classList.contains('flowdock');
  if ((docked || document.querySelector('.side').classList.contains('mem')) && key !== memKey) { memKey = key; renderMemory($('memBody'), {...args(), rows: docked ? 7 : 6, compact: true}); }
  // the diagram's own detail repeats the memory only when it is not already shown beside it
  const cur = flowOpen() && !docked ? flowPath(S.marks || [], mark).pop() : null, show = ['MEM', 'DEC', 'WAIT'].includes(cur);
  $('flowMem').hidden = !show; $('flowText').hidden = show; $('flowCount').hidden = show;
  if (show && key !== flowMemKey) { flowMemKey = key; renderMemory($('flowMem'), {...args(), rows: docked ? 3 : 5, compact: true}); }
}

// ---- vibration clue graph (real band RMS: the saved run, or the live acquisition) ----
/** src: {t, ch1, ch2 (mV), candidates [{t_start_s,t_end_s}], arm [{t0_s,t1_s}], ymax} */
function drawWave(t, src, before = 8, after = 4) {
  const c = $('wave');
  // the panel changes size (docked diagram, spotlight); follow it so the graph is never stretched or 0 px
  if (c.clientWidth && (c.width !== c.clientWidth || c.height !== c.clientHeight)) { c.width = c.clientWidth; c.height = c.clientHeight; }
  const g = c.getContext('2d'), W = c.width, H = c.height;
  g.clearRect(0, 0, W, H);
  if (!src || !src.t.length) { g.fillStyle = '#93a8a6'; g.font = '20px system-ui'; g.fillText(S.mode === 'live' ? '取得していません' : '振動の記録がありません', 20, 40); return; }
  const L = 64, R = 10, T = 12, B = 30, w = W - L - R, h = H - T - B, t0 = t - before, t1 = t + after, ymax = src.ymax;
  const ymin = src.log ? Math.max(src.ymin || ymax / 100, 1e-6) : 0, span = Math.log10(ymax / ymin);
  const frac = v => src.log ? (v > 0 ? Math.log10(v / ymin) / span : 0) : v / ymax;
  const X = x => L + (x - t0) / (t1 - t0) * w, Y = v => T + h - Math.min(1, Math.max(0, frac(v))) * h;
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n);
  g.fillStyle = css('--arm');
  for (const s of src.arm || []) if (s.t1_s >= t0 && s.t0_s <= t1) g.fillRect(X(Math.max(t0, s.t0_s)), T, Math.max(2, X(Math.min(t1, s.t1_s)) - X(Math.max(t0, s.t0_s))), h);
  g.fillStyle = css('--cand'); g.strokeStyle = css('--act'); g.lineWidth = 1.5;
  for (const k of src.candidates || []) if (k.t_end_s >= t0 && k.t_start_s <= t1) {
    const a = X(Math.max(t0, k.t_start_s)), b = X(Math.min(t1, k.t_end_s)); g.fillRect(a, T, b - a, h); g.strokeRect(a, T, b - a, h);
  }
  // low-frequency activity-likeness (PC reference, provisional; lowfreq_activity.py): strip along the top
  g.fillStyle = css('--low');
  for (const s of src.low || []) if (s.t1_s >= t0 && s.t0_s <= t1) g.fillRect(X(Math.max(t0, s.t0_s)), T, Math.max(2, X(Math.min(t1, s.t1_s)) - X(Math.max(t0, s.t0_s))), 7);
  g.strokeStyle = '#24424c'; g.lineWidth = 1; g.fillStyle = '#93a8a6'; g.font = '15px system-ui';
  const ticks = src.log ? logTicks(ymin, ymax) : [0, 1, 2, 3, 4].map(i => ymax * i / 4);
  for (const v of ticks) { const y = Y(v); g.beginPath(); g.moveTo(L, y); g.lineTo(L + w, y); g.stroke(); g.fillText(v >= 1 ? v.toFixed(1) : v >= 0.1 ? v.toFixed(2) : v.toFixed(3), 6, y + 5); }
  g.fillText('mV', 6, T + h + 24);
  for (let s = Math.ceil(t0); s <= t1; s += 2) { const x = X(s); g.fillText(`${s - Math.round(t) >= 0 ? '+' : ''}${s - Math.round(t)}s`, x - 12, T + h + 22); }
  const lo = lower(src.t, t0), hi = lower(src.t, t1) + 1;
  for (const [key, color, width] of [['ch1', css('--out'), 2], ['ch2', css('--in'), 3]]) {
    g.strokeStyle = color; g.lineWidth = width; g.beginPath(); let pen = false;
    for (let i = lo; i < Math.min(hi, src.t.length); i++) {
      const v = src[key][i]; if (v == null || !isFinite(v)) { pen = false; continue; }
      if (pen) g.lineTo(X(src.t[i]), Y(v)); else { g.moveTo(X(src.t[i]), Y(v)); pen = true; }
    }
    g.stroke();
  }
  g.strokeStyle = '#eaf2f0'; g.lineWidth = 2; g.beginPath(); g.moveTo(X(t), T); g.lineTo(X(t), T + h); g.stroke();
}
function logTicks(lo, hi) {
  const out = [];
  for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) for (const m of [1, 2, 5]) { const v = m * 10 ** e; if (v >= lo && v <= hi) out.push(v); }
  return out.length >= 2 ? out : [lo, hi];
}
// The graph's source: 1-40 Hz RMS (default, replay only) or the detector's 300-1500 Hz band RMS.
// Which band started the observations depends on the run: B2 (300-1500 Hz) until 2026-11-22,
// the 1-40 Hz activity (forest or ratio >= 3) from 2026-11-23 (overview.candidate_detector).
const WAVE_TEXT = {low: '低周波 1–40 Hz（RMS 250 ms・対数）', band: 'B2 振動候補（300–1500 Hz 帯域RMS）'};
const WAVE_LABEL = new Proxy({}, {get: (_, k) => {
  const lowTrigger = (S.ov?.candidate_detector || (S.mode === 'live' ? 'LOWFREQ_FOREST_RATIO' : 'B2_guarded_mad')) === 'LOWFREQ_FOREST_RATIO';
  return (lowTrigger === (k === 'low') ? '撮影のきっかけ：' : '') + WAVE_TEXT[k];
}});
function waveSeries() { return S.wave === 'low' && S.ov?.lowfreq?.t?.length ? S.ov.lowfreq : S.ov?.band; }
function waveLog() { return S.wave === 'low' && !!S.ov?.lowfreq?.t?.length; }
/** [ymin, ymax] of the graph over [a, b]: ymax from the peak, ymin (log only) from the quiet floor. */
function rangeOf(a, b) {
  const src = waveSeries(); if (!src) return [0, 1];
  const vals = [];
  for (let i = lower(src.t, a); i < src.t.length && src.t[i] <= b; i++) for (const k of ['ch1', 'ch2']) { const v = src[k][i]; if (v > 0 && isFinite(v)) vals.push(v); }
  if (!vals.length) return [0, 1];
  vals.sort((x, y) => x - y);
  const ymax = vals[vals.length - 1] * 1.15;
  return [waveLog() ? Math.min(vals[Math.floor(vals.length * 0.05)] * 0.6, ymax / 4) : 0, ymax];
}
function setWave(kind) {
  S.wave = kind === 'band' ? 'band' : 'low'; $('waveSel').value = S.wave;
  try { localStorage.setItem('bll.demo.wave', S.wave); } catch (_) {}
  const missing = S.wave === 'low' && S.ov && !waveLog();
  $('waveLabel').textContent = S.mode === 'live' ? WAVE_LABEL.band + '・LIVE は 300–1500 Hz のみ' :
    missing ? WAVE_LABEL.band + '（この記録・ブリッジでは 1–40 Hz のデータを取得できませんでした）' : WAVE_LABEL[S.wave];
  if (S.mode !== 'replay' || !S.ov) return;
  if (S.whole) { for (const w of S.wholeEps || []) [w.ymin, w.ymax] = rangeOf(w.ep.clip_s[0], Math.max(w.ep.clip_s[1], w.end)); S.wholeKey = null; }
  else [S.ymin, S.ymax] = rangeOf(S.clip[0], S.clip[1]);
  S.key = null;
}
function lower(a, x) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] < x) lo = m + 1; else hi = m; } return Math.max(0, lo - 1); }

// ================= replay (default) =================
// Whole-run mode (場面 = 記録全体): the clip is the whole run (or the part covered by the video)
// and the episode on screen follows the time. Between episodes the stage shows "watching"
// with the last episode's photos kept.
const WHOLE = 'all', HOLD_S = 2.0;
function wholeAt(t) {
  const list = S.wholeEps || [];
  let i = -1;
  for (let k = 0; k < list.length; k++) { if (list[k].start <= t) i = k; else break; }
  if (i < 0) return {i: -1, idle: true};
  return {i, idle: t > list[i].end};
}
function setWholeEpisode(t) {
  const {i, idle} = wholeAt(t), list = S.wholeEps;
  const key = `${i}:${idle}`;
  if (key === S.wholeKey) return;
  S.wholeKey = key; S.key = null;
  if (i < 0) { S.ep = {steps: [], answers: []}; S.marks = []; [S.ymin, S.ymax] = rangeOf(t - 8, t + 30); }
  else { S.ep = list[i].ep; S.marks = idle ? [] : list[i].marks; S.ymin = list[i].ymin; S.ymax = list[i].ymax; }
  S.wholeIdle = idle;
  // keep the photos of the episode just finished while watching
  S.keepShots = idle && i >= 0 ? list[i].marks : null;
}
function wholeClip() {
  const d = S.data.duration_s, v = S.ov?.video;
  if (v?.available && S.ready && video.duration) return [Math.max(0, -S.offset), Math.min(d, video.duration - S.offset)];
  return [0, d];
}
function openWhole() {
  pause();
  const eps = S.data?.episodes || [], arm = S.ov.actuation || [];
  S.whole = true; $('episode').value = WHOLE;
  S.wholeEps = eps.map((ep, i) => {
    const marks = buildTimeline(ep, arm), last = marks[marks.length - 1]?.t ?? ep.clip_s[1];
    return {ep, marks, start: marks[0]?.t ?? ep.t_start_s, end: last + HOLD_S, ymax: 0, i};
  });
  // A candidate raised while the arm is still on the previous one waits its turn: the XIAO handles
  // one at a time and the bridge sends the next only after the ack. Its marks (shown on screen)
  // start once the previous episode has ended, so it no longer takes the stage mid-capture and
  // blanks the TOP/BACK photos (user report 2026-11-22). Recorded times are unchanged.
  for (let k = 1; k < S.wholeEps.length; k++) {
    const prev = S.wholeEps[k - 1], w = S.wholeEps[k], busy = prev.marks.at(-1)?.t;
    if (busy == null || w.start >= busy) continue;
    // No recorded capture (WAIT / no decision): its marks are placed estimates; keep their spacing.
    // With recorded captures: those times stand, earlier estimates are clamped.
    const shift = (w.ep.steps || []).some(s => s.t_s != null) ? 0 : busy - w.start;
    w.marks = w.marks.map(m => ({...m, t: Math.max(m.t + shift, busy)}));
    w.start = busy; w.end = Math.max(w.end, w.marks.at(-1).t + HOLD_S);
  }
  for (let k = 0; k < S.wholeEps.length; k++) {
    const w = S.wholeEps[k], next = S.wholeEps[k + 1];
    if (next) w.end = Math.min(w.end, next.start);
    [w.ymin, w.ymax] = rangeOf(w.ep.clip_s[0], Math.max(w.ep.clip_s[1], w.end));
  }
  S.wholeKey = null;
  chooseClock(); S.clip = wholeClip(); chooseClock(); replayFooter(); seek(S.clip[0]);
  try { history.replaceState(null, '', `?run=${encodeURIComponent(S.run)}&ep=${WHOLE}`); } catch (_) {}
}
function jumpEpisode(d) {
  if (!S.whole) { openEpisode(+$('episode').value + d); return; }
  // each scene is entered 2 s before its candidate; prev within 3 s of an entry goes one further back
  const at = (S.wholeEps || []).map(w => Math.max(S.clip[0], w.start - 2));
  const target = d > 0 ? at.find(x => x > S.t + 0.5) : [...at].reverse().find(x => x < S.t - 3) ?? S.clip[0];
  if (target != null) seek(target);
}
function paintReplay(t) {
  if (S.whole) setWholeEpisode(t);
  const mark = markAt(S.marks, t), key = S.marks.indexOf(mark);
  if (key !== S.key) {
    S.key = key; paintNav(mark); paintCards(mark); paintFlowFor(mark);
    paintShots(new Set((S.keepShots || S.marks).filter(m => m.kind === 'RESULT' && m.t <= t).map(m => m.step)), mark);
  }
  const scene = S.whole ? (() => { const {i, idle} = wholeAt(t); return i < 0 || idle ? '  見守り中' : `  場面 ${i + 1}/${S.wholeEps.length}`; })() : '';
  $('clock').textContent = `${clock(t)}  ${jst(t).replace(/^\d+\/\d+\/\d+ /, '')}${scene}`;
  $('flowClock').textContent = '観測記録の再生（同期リプレイ） ' + $('clock').textContent;
  $('seek').value = Math.round((t - S.clip[0]) / Math.max(0.001, S.clip[1] - S.clip[0]) * 1000);
  const low = S.ov?.low_activity?.activity_like_spans || [], now = low.find(s => s.t0_s <= t && t <= s.t1_s);
  $('clueLow').hidden = !now;
  if (now) $('clueLow').textContent = `低い周波数に幼虫の活動らしい変化（最大 ×${now.peak_x.toFixed(1)}）`;
  spotlight(t);
  paintMemory(t, markAt(S.marks, t));
  const w = waveSeries();
  drawWave(t, w && {t: w.t, ch1: w.ch1, ch2: w.ch2, candidates: S.ov.candidates, arm: S.ov.actuation, low, ymax: S.ymax, ymin: S.ymin, log: waveLog()});
}
function loop(now) {
  if (S.mode === 'replay' && S.playing) {
    if (S.useVideo) S.t = video.currentTime - S.offset;
    else S.t += Math.min(0.1, (now - S.last) / 1000) * +$('speed').value;
    if (S.t >= S.clip[1]) { S.t = S.clip[1]; pause(); }
  }
  S.last = now;
  if (S.mode === 'replay' && (S.ep || S.whole)) paintReplay(S.t);
  requestAnimationFrame(loop);
}
function seek(t) {
  S.t = Math.min(S.clip[1], Math.max(S.clip[0], t)); S.key = null;
  if (S.useVideo) video.currentTime = S.t + S.offset;
}
function play() {
  if (S.mode !== 'replay' || !(S.ep || S.whole)) return;
  if (S.t >= S.clip[1] - 0.05) seek(S.clip[0]);
  S.playing = true; $('play').textContent = '❚❚ 一時停止';
  if (S.useVideo) { video.playbackRate = +$('speed').value; video.play().catch(() => {}); }
}
function pause() { S.playing = false; $('play').textContent = '▶ 再生'; if (S.useVideo) video.pause(); }

// The run's OBS recording, cropped to the camera area like the episode view.
function cropVideo() {
  const c = S.mode === 'replay' ? S.ov?.video?.content_box : null, wrap = $('vwrap');
  if (!c || !video.videoWidth) { Object.assign(video.style, {width: '100%', left: '0', top: '0'}); wrap.style.aspectRatio = '16 / 9'; return; }
  const [x0, y0, x1, y1] = c, bw = x1 - x0, bh = y1 - y0;
  wrap.style.aspectRatio = `${video.videoWidth * bw} / ${video.videoHeight * bh}`;
  Object.assign(video.style, {width: (100 / bw) + '%', left: (-x0 / bw * 100) + '%', top: (-y0 / bh * 100) + '%'});
}
video.addEventListener('loadedmetadata', () => {
  cropVideo();
  if (S.mode === 'replay') {
    S.ready = true; chooseClock();
    if (S.whole) { const t = S.t; S.clip = wholeClip(); chooseClock(); replayFooter(); seek(Math.max(S.clip[0], t)); } else seek(S.t);
  }
});
function chooseClock() {
  const v = S.ov?.video;
  S.offset = v?.available ? v.offset_s : 0;
  $('noVideo').textContent = 'この記録には外部カメラの動画がありません';
  S.useVideo = !!(v?.available && S.ready && S.clip[0] + S.offset >= 0 && (!video.duration || S.clip[1] + S.offset <= video.duration + 0.5));
  $('noVideo').hidden = !!v?.available; $('vwrap').hidden = !v?.available;
  if (v?.available && !S.useVideo && S.ready) $('noVideo').hidden = false, $('noVideo').textContent = 'この場面は動画の録画範囲外です';
}
function replayFooter() {
  const d = S.data, v = S.ov?.video, sync = !v?.available ? '動画なし（記録の時刻で再生）' :
    v.offset_source === 'SAVED_REVIEW' ? '動画との時刻合わせ：振り返り画面で確認済み' : '動画との時刻合わせ：PCの受信時刻からの推定（数秒ずれることがあります）';
  const est = (S.ep?.steps || []).some(s => s.time_basis === 'ESTIMATED_EVEN_SPACING') ? ' ・ 撮影時刻は古い記録のため均等割りの推定' : '';
  $('foot').textContent = `保存した実際の記録の再生（LIVEではありません）${S.whole ? ' ・ 記録全体を通しで再生（場面は時刻に合わせて自動で切り替え）' : ''} ・ ${sync}${est} ・ 工程の切り替わりは記録時刻からの目安 ・ 判断はルールベース（学習済みAIではありません） ・ ` +
    `幼虫の判定は画像AI（Vision V2 上の分類モデル）の出力、確度はモデルのスコア ・ 記録 ${jst(0).replace('（推定）', '')}から ${clock(d.duration_s)}` + (d.case_id ? ` ・ ケース ${d.case_id}` : '');
}
function fillEpisodes() {
  $('episode').replaceChildren(Object.assign(el('option', null, `記録全体を通しで再生（場面 ${S.data.episodes.length} 件）`), {value: WHOLE}),
    ...S.data.episodes.map((ep, i) => Object.assign(el('option', null, episodeLabel(ep, i, clock)), {value: i})));
}
function openEpisode(index) {
  if (index === WHOLE) { openWhole(); return; }
  pause(); S.whole = false; S.keepShots = null; S.wholeIdle = false;
  const eps = S.data?.episodes || []; if (!eps.length) return;
  index = Math.max(0, Math.min(eps.length - 1, index)); $('episode').value = index;
  S.ep = eps[index];
  S.marks = buildTimeline(S.ep, S.ov.actuation || []);
  const lastMark = S.marks[S.marks.length - 1]?.t ?? S.ep.clip_s[1];
  S.clip = [S.ep.clip_s[0], Math.min(S.data.duration_s, Math.max(S.ep.clip_s[1], lastMark + 2))];
  [S.ymin, S.ymax] = rangeOf(S.clip[0], S.clip[1]);
  chooseClock(); replayFooter(); seek(S.clip[0]);
  try { history.replaceState(null, '', `?run=${encodeURIComponent(S.run)}&ep=${index}`); } catch (_) {}
}
function loadRunVideo() {
  video.srcObject = null; video.removeAttribute('src');
  if (S.ov?.video?.available) { video.src = `/api/review/video?run=${encodeURIComponent(S.run)}`; video.load(); }
  video.muted = !$('sound').checked;
}
async function openRun(run, epIndex) {
  pause(); S.run = run; S.ep = null; S.whole = false; S.keepShots = null; S.ready = false; $('error').textContent = '';
  updateLinks();
  try {
    const [ov, data] = await Promise.all([getJSON(`/api/review/overview?run=${encodeURIComponent(run)}`), getJSON(`/api/review/episodes?run=${encodeURIComponent(run)}`)]);
    S.ov = ov; S.data = data;
  } catch (e) { $('error').textContent = '読み込めません: ' + e.message; return; }
  S.history = buildHistory(S.data.episodes, S.ov.actuation || []); memKey = flowMemKey = null;
  if (S.mode !== 'replay') return;
  setWave(S.wave);
  fillEpisodes();
  loadRunVideo();
  if (epIndex === WHOLE) { openWhole(); return; }
  if (!S.data.episodes.length) { $('error').textContent = 'この記録には振動候補がありません'; return; }
  const best = S.data.episodes.findIndex(ep => (ep.steps || []).filter(s => s.image_url).length >= 2);
  openEpisode(epIndex ?? (best >= 0 ? best : 0));
}

// Links to the other pages carry what is open here: the run, the episode (candidate seq) and the time.
// Refreshed just before a click (pointerdown/focus), so a middle click or a new tab gets them too.
function currentSeq() {
  if (S.whole) { const {i} = wholeAt(S.t); return i >= 0 ? S.wholeEps[i].ep.candidate_seq : null; }
  return S.ep?.candidate_seq ?? null;
}
function updateLinks() {
  if (!S.run || S.mode !== 'replay') {
    $('toDiary').href = '/diary'; $('toEpisodes').href = '/episodes'; $('toReview').href = '/review'; return;
  }
  const run = encodeURIComponent(S.run), seq = currentSeq(), t = Math.max(0, S.t || 0).toFixed(2);
  $('toDiary').href = `/diary?run=${run}`;
  $('toEpisodes').href = `/episodes?run=${run}` + (seq != null ? `&seq=${seq}` : '');
  $('toReview').href = `/review?run=${run}&t=${t}`;
}

// ================= live =================
async function listCameras() {
  const sel = $('camera'), keep = sel.value;
  try {
    const cams = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === 'videoinput');
    sel.replaceChildren(...cams.map((d, i) => Object.assign(el('option', null, d.label || `カメラ ${i + 1}`), {value: d.deviceId})));
    if ([...sel.options].some(o => o.value === keep)) sel.value = keep;
  } catch (_) { /* no camera list: the default camera is used */ }
}
function stopCamera() { for (const tr of S.stream?.getTracks() || []) tr.stop(); S.stream = null; video.srcObject = null; }
async function startCamera() {
  stopCamera(); video.removeAttribute('src');
  // Until the browser's permission prompt is answered, say so instead of a black frame.
  $('vwrap').hidden = true; $('noVideo').hidden = false;
  $('noVideo').textContent = 'カメラを準備しています。ブラウザがカメラの使用許可を求めている場合は「許可」を選んでください';
  try {
    const id = $('camera').value;
    S.stream = await navigator.mediaDevices.getUserMedia({video: id ? {deviceId: {exact: id}, width: {ideal: 1920}, height: {ideal: 1080}} : {width: {ideal: 1920}, height: {ideal: 1080}}, audio: false});
    if (S.mode !== 'live') { stopCamera(); return; }
    video.srcObject = S.stream; video.muted = true; $('vwrap').hidden = false; $('noVideo').hidden = true;
    cropVideo(); await video.play().catch(() => {});
    await listCameras();
    const label = S.stream.getVideoTracks()[0]?.label; if (label) $('camera').value = [...$('camera').options].find(o => o.text === label)?.value ?? $('camera').value;
  } catch (e) {
    $('vwrap').hidden = true; $('noVideo').hidden = false;
    $('noVideo').textContent = `カメラを開けません（${e.name || e.message}）。OBSがカメラを使っている場合は「OBS Virtual Camera」を選んでください`;
  }
}
async function pollLive() {
  if (S.mode !== 'live' || S.liveBusy) return;
  S.liveBusy = true;
  try { S.live = await getJSON('/api/state'); $('error').textContent = ''; }
  catch (e) { $('error').textContent = '取得状態を読めません: ' + e.message; }
  finally { S.liveBusy = false; }
  if (S.mode === 'live' && S.live) paintLive(S.live);
}
function paintLive(s) {
  // Remember when the answer of the episode in focus was first seen, so it stays a while.
  const seq = s.delivering ?? (s.candidates || []).at(-1)?.candidate_seq ?? null;
  const answered = (s.observations || []).some(o => o.candidate_seq === seq);
  if (!S.liveHeld || S.liveHeld.seq !== seq || S.liveHeld.run !== s.run_id) S.liveHeld = {seq, run: s.run_id, at: null};
  if (answered && S.liveHeld.at == null) S.liveHeld.at = s.elapsed_s ?? 0;
  const {ep, mark} = liveState(s, S.liveHeld.at);
  S.ep = ep;
  const key = JSON.stringify([s.run_id, s.running, seq, mark, ep.steps.length]);
  if (key !== S.key) {
    S.key = key; paintNav(mark); paintCards(mark); paintFlowFor(mark);
    paintShots(new Set(ep.steps.map((_, i) => i)), mark);
    if (!s.running) {
      $('clueMain').textContent = '取得していません'; $('clueIcon').textContent = 'pause_circle';
      $('clueSub').textContent = '取得画面（AD3ピエゾ）で計測を開始すると、ここに観測ループが表示されます';
    }
    liveFooter(s);
  }
  const t = s.elapsed_s ?? 0;
  $('clock').textContent = `${s.running ? clock(t) : '停止中'}  ${jstOf(Date.now()).replace(/^\d+\/\d+\/\d+ /, '')}`;
  $('flowClock').textContent = 'LIVE ' + $('clock').textContent;
  const f = s.features || [], src = {t: f.map(r => +r.t_center_s), ch2: f.map(r => r.rms_V == null ? null : r.rms_V * 1e3),
    ch1: f.map(r => r.ch1_rms_V == null || r.ch1_rms_V === '' ? null : r.ch1_rms_V * 1e3), candidates: s.candidates || [], arm: []};
  let ymax = 0; for (let i = lower(src.t, t - 10); i < src.t.length; i++) for (const k of ['ch1', 'ch2']) if (isFinite(src[k][i])) ymax = Math.max(ymax, src[k][i]);
  src.ymax = Math.max(0.02, ymax * 1.1);
  drawWave(t, s.running || f.length ? src : null, 10, 1);
}
function liveFooter(s) {
  const tag = s.origin === 'SYNTHETIC' ? 'MOCK（合成波形）の取得です。測定データではありません' : s.running ? 'いま動いている取得を表示' : '取得は停止中';
  $('foot').textContent = `LIVE：このPCのカメラ映像と、ブリッジの現在の状態をそのまま表示 ・ ${tag} ・ ` +
    `複数の方向を撮るときは、各方向の結果が1回の観察の終わりにまとめて届きます ・ 判断はルールベース（学習済みAIではありません） ・ 幼虫の判定は画像AI（Vision V2 上の分類モデル）の出力` +
    (s.case_id ? ` ・ ケース ${s.case_id}` : '');
}

// ================= mode switch =================
function setMode(mode) {
  S.mode = mode; S.key = null; pause(); $('clueLow').hidden = true; document.querySelector('.side').classList.remove('spot', 'mem');
  $('flowMem').hidden = true; $('flowText').hidden = false; $('flowCount').hidden = false;
  if (mode === 'live') $('memBody').textContent = 'LIVE では記憶欄を表示しません（記録の再生で表示します）';
  memKey = flowMemKey = null;
  $('waveLabel').textContent = mode === 'live' ? WAVE_LABEL.band + '・LIVE は 300–1500 Hz のみ' : WAVE_LABEL[S.wave];
  document.body.classList.toggle('live', mode === 'live');
  $('mode').value = mode;
  $('replayTag').hidden = mode === 'live'; $('liveTag').hidden = mode !== 'live';
  $('vlabelText').textContent = mode === 'live' ? '実機のライブ映像（このPCのカメラ）' : '実機の映像（記録の動画・OBS）';
  try { history.replaceState(null, '', mode === 'live' ? '?mode=live' : S.run ? `?run=${encodeURIComponent(S.run)}&ep=${$('episode').value || 0}` : location.pathname); } catch (_) {}
  S.whole = false; S.keepShots = null;
  clearInterval(S.liveTimer);
  if (mode === 'live') {
    S.ep = {steps: [], answers: []}; startCamera(); pollLive(); S.liveTimer = setInterval(pollLive, 400);
  } else {
    stopCamera();
    if (S.data) { fillEpisodes(); S.ready = false; loadRunVideo(); openEpisode(0); }
  }
}

(async () => {
  // Icons come from Google Fonts; without the font (offline) the icon names would show as words.
  const noIcons = () => document.body.classList.add('noicons');
  document.fonts.load('24px "Material Symbols Rounded"', 'home').then(f => { if (!f.length) noIcons(); }).catch(noIcons);
  setTimeout(() => { if (!document.fonts.check('24px "Material Symbols Rounded"', 'home')) noIcons(); }, 4000);
  buildNav(); fit(); addEventListener('resize', fit);
  buildFlow($('flowSvg'), flowJump);
  $('nav').addEventListener('click', () => toggleFlow());
  $('flowClose').onclick = () => toggleFlow(false);
  $('flowDock').onclick = () => setDock(!document.body.classList.contains('flowdock'));
  try { if (localStorage.getItem('bll.demo.flowdock')) setDock(true); } catch (_) {}
  const c = $('wave'); c.width = c.clientWidth; c.height = c.clientHeight;
  $('mode').onchange = () => setMode($('mode').value);
  $('camera').onchange = () => { if (S.mode === 'live') startCamera(); };
  $('episode').onchange = () => openEpisode($('episode').value === WHOLE ? WHOLE : +$('episode').value);
  $('prev').onclick = () => jumpEpisode(-1);
  $('next').onclick = () => jumpEpisode(1);
  $('play').onclick = () => (S.playing ? pause() : play());
  $('rewind').onclick = () => { pause(); seek(S.clip[0]); };
  $('speed').onchange = () => { video.playbackRate = +$('speed').value; };
  let savedWave = null; try { savedWave = localStorage.getItem('bll.demo.wave'); } catch (_) {}
  setWave(new URLSearchParams(location.search).get('wave') || savedWave || 'low');
  $('waveSel').onchange = () => setWave($('waveSel').value);
  $('seek').oninput = () => seek(S.clip[0] + (+$('seek').value / 1000) * (S.clip[1] - S.clip[0]));
  $('sound').onchange = () => { if (S.mode === 'replay') video.muted = !$('sound').checked; };
  $('rec').onclick = () => { document.body.classList.add('rec'); fit(); };
  for (const id of ['toDiary', 'toEpisodes', 'toReview']) for (const ev of ['pointerdown', 'focus', 'mouseenter']) $(id).addEventListener(ev, updateLinks);
  // 前の版／最新版の比較リンク: open the other version on the same run and scene
  const other = $('toOther'); if (other) other.onclick = e => { e.preventDefault(); location.href = other.getAttribute('href') + location.search; };
  $('full').onclick = () => document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen().catch(() => {});
  addEventListener('keydown', e => {
    if (e.target.closest('select,input')) return;
    if (e.key === 'Escape' && flowOpen()) { toggleFlow(false); return; }
    if (e.key === 'h' || e.key === 'H') { document.body.classList.toggle('rec'); fit(); }
    else if (e.key === 'f' || e.key === 'F') $('full').onclick();
    if (S.mode !== 'replay') return;
    if (e.key === ' ') { e.preventDefault(); S.playing ? pause() : play(); }
    else if (e.key === 'ArrowRight') jumpEpisode(1);
    else if (e.key === 'ArrowLeft') jumpEpisode(-1);
  });
  requestAnimationFrame(loop);
  listCameras();
  const q = new URLSearchParams(location.search);
  if (q.get('mode') === 'live') setMode('live');
  try {
    const {runs} = await getJSON('/api/review/runs'), sel = $('run');
    for (const r of runs) {
      const when = new Date(r.started_at_pc).toLocaleString('ja-JP', {month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'});
      sel.append(Object.assign(el('option', null, `${statusPrefix(r.review_status)}${when} ・ ${clock(r.duration_s)} ・ 揺れ ${r.candidates} 回` + (r.case_id ? ` ・ ${r.case_id}` : '') + (r.note ? ` ・ ${r.note}` : '')), {value: r.run}));
    }
    if (!runs.length) { if (S.mode === 'replay') $('error').textContent = 'まだ記録がありません'; return; }
    const want = q.get('run'), pick = runs.some(r => r.run === want) ? want : runs[0].run; sel.value = pick;
    sel.onchange = () => openRun(sel.value);
    openRun(pick, q.get('ep') === WHOLE ? WHOLE : q.has('ep') ? +q.get('ep') : undefined);
  } catch (e) { if (S.mode === 'replay') $('error').textContent = '記録の一覧を読めません: ' + e.message; }
})();
