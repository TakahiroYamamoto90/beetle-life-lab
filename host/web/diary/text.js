// 観察日記（/diary）: plain-language sentences and grouping for the diary entries.
// Pure functions (no DOM), loadable in Node for tests. Everything said here is a count of what
// the records contain; nothing claims larva activity (it stays UNKNOWN). Photo labels are the
// human labels of 観察ノート, always shown as "人が確認".

export const POSE_JA = {FRONT: '前', TOP: '上', BACK: '後ろ', FIXED: '固定カメラ'};
export const HUMAN_LABEL = {
  larva_visible: {text: '幼虫が見える', cls: 'larva'},
  background: {text: '幼虫は写っていない', cls: 'bg'},
  uncertain: {text: 'はっきりしない', cls: 'unsure'},
  unusable: {text: '使えない画像', cls: 'muted'},
  exclude: {text: '除外', cls: 'muted'},
  unreviewed: {text: '未確認', cls: 'muted'},
};
const ANSWER_JA = {LARVA: '幼虫活動', DIG: '人工刺激（掘削）', NOISE: '外乱（声・物音）'};

/** yyyy-mm-dd of an ISO time in JST, and a heading like 11月15日（日）. */
export function dayKey(iso) {
  return new Date(iso).toLocaleDateString('en-CA', {timeZone: 'Asia/Tokyo'});
}
export function dayTitle(iso) {
  return new Date(iso).toLocaleDateString('ja-JP', {timeZone: 'Asia/Tokyo', month: 'long', day: 'numeric', weekday: 'short'});
}
export function timeOfDay(iso) {
  return new Date(iso).toLocaleTimeString('ja-JP', {timeZone: 'Asia/Tokyo', hour: '2-digit', minute: '2-digit'});
}
export function duration(s) {
  if (s == null || !isFinite(s)) return '—';
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = Math.round(s % 60);
  return h ? `${h}時間${m}分` : m ? (sec ? `${m}分${sec}秒` : `${m}分`) : `${sec}秒`;
}

/** Counts of human labels over the photos of entries. labels: {action_id: {label}} */
export function labelCounts(photos, labels) {
  const out = {};
  for (const p of photos) { const l = labels[p.action_id]?.label; if (l) out[l] = (out[l] || 0) + 1; }
  return out;
}

/** The diary sentence of one measurement. */
export function entrySentence(e, labels = {}) {
  if (!e.candidates) return '静かでした。気になる揺れはありませんでした。';
  const out = [`気になる揺れが ${e.candidates} 回ありました。`];
  if (!e.forwarded) out.push('この計測では観察係（MCU）に送っていないため、撮影はしていません。');
  else {
    const poses = [...new Set(e.photos.map(p => POSE_JA[p.pose] || p.pose))];
    if (e.captures) out.push(`${poses.length ? poses.join('・') + 'から' : ''}写真を ${e.captures} 枚撮りました。`);
    if (e.failed) out.push(`${e.failed} 回は撮影できませんでした。`);
    if (e.waits) out.push(`${e.waits} 回は撮らずに待ちました。`);
    if (!e.captures && !e.failed && !e.waits) out.push('観察係（MCU）は行動しませんでした。');
  }
  if (e.answered) {
    const kinds = Object.entries(e.answer_counts || {}).filter(([, n]) => n).map(([k, n]) => `${ANSWER_JA[k]} ${n}`);
    out.push(`振り返りで ${e.answered} 回を答え合わせ済み（${kinds.join('・')}）。`);
  }
  const lc = labelCounts(e.photos, labels), seen = (lc.larva_visible || 0);
  const checked = Object.entries(lc).filter(([k]) => k !== 'unreviewed').reduce((n, [, v]) => n + v, 0);
  if (checked) out.push(`写真は人が ${checked} 枚確認し、幼虫が見えたのは ${seen} 枚でした。`);
  return out.join('');
}

/** Entries -> [{key, title, entries (oldest first), totals}] with the newest day first. */
export function groupByDay(entries) {
  const days = new Map();
  for (const e of entries) {
    if (!e.started_at_pc) continue;
    const k = dayKey(e.started_at_pc);
    if (!days.has(k)) days.set(k, {key: k, title: dayTitle(e.started_at_pc), entries: []});
    days.get(k).entries.push(e);
  }
  const out = [...days.values()].sort((a, b) => (a.key < b.key ? 1 : -1));
  for (const d of out) {
    d.entries.sort((a, b) => (a.started_at_pc < b.started_at_pc ? -1 : 1));
    d.totals = totals(d.entries);
  }
  return out;
}
export function totals(entries) {
  const t = {runs: entries.length, seconds: 0, candidates: 0, captures: 0, waits: 0, answered: 0, photos: []};
  for (const e of entries) {
    t.seconds += e.duration_s || 0; t.candidates += e.candidates; t.captures += e.captures;
    t.waits += e.waits; t.answered += e.answered; t.photos.push(...e.photos);
  }
  return t;
}
