// 観察日記（/diary）: every saved LIVE measurement of a case, by day, in plain words, with the
// verified photos and the human labels from 観察ノート. Read-only (GET /api/review/diary).
import {lightbox} from '../common/lightbox.js';
import {orient} from '../common/orient.js';
import {HUMAN_LABEL, POSE_JA, duration, entrySentence, groupByDay, labelCounts, timeOfDay, totals} from './text.js';

const $ = id => document.getElementById(id);
const S = {data: null, labels: {}, cases: {}, shownDays: 7};
const PHOTOS_PER_ENTRY = 12;
const NONE = '__none__';

function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function icon(name) { return el('span', 'ms', name); }
function withIcon(tag, cls, name, text) { const e = el(tag, cls); e.append(icon(name), text); return e; }
async function getJSON(url) { const r = await fetch(url); const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || r.statusText); return j; }
const caseName = id => id === NONE ? 'ケース未指定の計測' : (S.cases[id] ? `${S.cases[id]}（${id}）` : id);

function fact(name, value, label) { const f = el('div', 'fact'); f.append(el('b', null, String(value)), withIcon('span', null, name, label)); return f; }

function photoTile(p, items, index) {
  const lab = S.labels[p.action_id], h = HUMAN_LABEL[lab?.label] || null;
  const box = el('div', 'ph' + (lab?.label === 'larva_visible' ? ' larva' : ''));
  const img = el('img'); img.src = p.url; orient(img, p.pose); img.loading = 'lazy'; img.alt = `${POSE_JA[p.pose] || p.pose}からの写真`;
  box.append(img, el('span', 'pose', POSE_JA[p.pose] || p.pose || '?'));
  box.append(el('span', 'lab ' + (h?.cls || 'muted'), !h ? '観察ノート未取込' : lab.label === 'unreviewed' ? '人の確認：まだ' : `人が確認：${h.text}`));
  box.onclick = () => lightbox(items, index);
  return box;
}

function entryCard(e) {
  const card = el('article', 'entry' + (e.candidates ? '' : ' quiet') + (e.run === S.focusRun ? ' focus' : ''));
  card.dataset.run = e.run;
  const when = el('div', 'when'); when.append(el('b', null, timeOfDay(e.started_at_pc)), el('small', null, duration(e.duration_s)));
  const body = el('div');
  body.append(el('div', 'text', entrySentence(e, S.labels)));
  const conds = el('div', 'conds');
  if (e.illumination) conds.append(withIcon('span', 'chip', 'light_mode', e.illumination));
  if (e.note) conds.append(withIcon('span', 'chip', 'edit_note', e.note));
  if (e.video) conds.append(withIcon('span', 'chip', 'videocam', '動画あり'));
  // PC reference with a provisional threshold, never a detection (lowfreq_activity.py)
  const L = e.low_activity;
  if (L?.status === 'PENDING') conds.append(withIcon('span', 'chip', 'hourglass_top', '低周波の活動らしさ：計算中（再読み込みで表示）'));
  else if (L) conds.append(withIcon('span', 'chip' + (L.spans ? ' low' : ''), 'waves', L.spans ? `低周波の活動らしさ ${L.spans} 回・計 ${Math.round(L.active_s)} 秒（参考）` : '低周波の活動らしさ：反応なし（参考）'));
  if (conds.children.length) body.append(conds);
  const only = $('onlyLarva').checked;
  const photos = e.photos.filter(p => !only || S.labels[p.action_id]?.label === 'larva_visible');
  if (photos.length) {
    const items = photos.map(p => {
      const h = HUMAN_LABEL[S.labels[p.action_id]?.label];
      return {src: p.url, pose: p.pose, caption: `${timeOfDay(e.started_at_pc)} の計測 ・ ${POSE_JA[p.pose] || p.pose}から\n` +
        (h && S.labels[p.action_id].label !== 'unreviewed' ? `人が確認：${h.text}` : '人はまだ確認していません') + (S.labels[p.action_id]?.note ? `\nメモ：${S.labels[p.action_id].note}` : '')};
    });
    const strip = el('div', 'photos');
    photos.slice(0, PHOTOS_PER_ENTRY).forEach((p, i) => strip.append(photoTile(p, items, i)));
    if (photos.length > PHOTOS_PER_ENTRY) strip.append(el('span', 'morephotos', `ほか ${photos.length - PHOTOS_PER_ENTRY} 枚（写真をクリックして順に見られます）`));
    body.append(strip);
  }
  const links = el('div', 'links'), q = encodeURIComponent(e.run);
  const a = (href, name, text) => { const x = el('a'); x.href = href; x.append(icon(name), text); links.append(x); };
  if (e.candidates) a(`/episodes?run=${q}`, 'auto_stories', 'エピソードを見る');
  if (e.candidates && e.forwarded) a(`/demo?run=${q}`, 'smart_display', 'デモ再生');
  a(`/review?run=${q}`, 'fact_check', e.answered ? `振り返り（答え合わせ ${e.answered}/${e.candidates}）` : '振り返り・答え合わせ');
  body.append(links);
  card.append(when, body);
  return card;
}

function render() {
  const pick = $('case').value, all = S.data.entries;
  const entries = pick === '' ? all : all.filter(e => (e.case_id || NONE) === pick);
  const t = totals(entries), lc = labelCounts(t.photos, S.labels);
  $('summary').hidden = false;
  $('caseName').textContent = pick === '' ? 'すべてのケース' : caseName(pick);
  $('facts').replaceChildren(
    fact('event_note', t.runs, `計測（合計 ${duration(t.seconds)}）`), fact('vibration', t.candidates, '気になる揺れ'),
    fact('photo_camera', t.captures, '撮った写真'), fact('hourglass_top', t.waits, '撮らずに待った'),
    fact('visibility', lc.larva_visible || 0, '人が「幼虫が見える」とした写真'));
  const days = groupByDay(entries), box = $('days');
  box.replaceChildren();
  if (!days.length) { box.append(el('div', 'empty', 'このケースの計測はまだありません')); $('more').hidden = true; return; }
  for (const d of days.slice(0, S.shownDays)) {
    const sec = el('section', 'day'), h = el('h2');
    const lcd = labelCounts(d.totals.photos, S.labels);
    h.append(icon('calendar_month'), d.title, el('small', 'muted',
      `計測 ${d.totals.runs} 回（${duration(d.totals.seconds)}） ・ 揺れ ${d.totals.candidates} 回 ・ 写真 ${d.totals.captures} 枚 ・ 待った ${d.totals.waits} 回` +
      (lcd.larva_visible ? ` ・ 幼虫が見えた写真 ${lcd.larva_visible} 枚（人が確認）` : '')));
    const list = el('div', 'entries');
    for (const e of d.entries) list.append(entryCard(e));
    sec.append(h, list); box.append(sec);
  }
  $('more').hidden = days.length <= S.shownDays;
  $('more').textContent = `さらに前の日を表示（残り ${days.length - S.shownDays} 日）`;
}

(async () => {
  const noIcons = () => document.body.classList.add('noicons');
  document.fonts.load('24px "Material Symbols Rounded"', 'home').then(f => { if (!f.length) noIcons(); }).catch(noIcons);
  setTimeout(() => { if (!document.fonts.check('24px "Material Symbols Rounded"', 'home')) noIcons(); }, 4000);
  try { S.data = await getJSON('/api/review/diary'); }
  catch (e) { $('error').textContent = '日記を読み込めません: ' + e.message; return; }
  const col = S.data.collection || {};
  S.labels = col.labels || {}; S.cases = col.cases || {};
  const counts = new Map();
  for (const e of S.data.entries) counts.set(e.case_id || NONE, (counts.get(e.case_id || NONE) || 0) + 1);
  const sel = $('case');
  sel.append(el('option', null, `すべてのケース（${S.data.entries.length} 回）`)); sel.lastChild.value = '';
  for (const [id, n] of [...counts].sort((a, b) => (a[0] === NONE) - (b[0] === NONE) || a[0].localeCompare(b[0]))) {
    const o = el('option', null, `${caseName(id)}（${n} 回）`); o.value = id; sel.append(o);
  }
  const q = new URLSearchParams(location.search), want = q.get('case');
  // ?run=… (from the demo page): open the case and enough days to show that measurement, then scroll to it
  const focus = S.data.entries.find(e => e.run === q.get('run'));
  S.focusRun = focus?.run || null;
  const latest = S.data.entries[0]?.case_id || NONE;
  sel.value = focus ? (focus.case_id || NONE) : want != null && [...sel.options].some(o => o.value === want) ? want : latest;
  if (focus) {
    const days = groupByDay(S.data.entries.filter(e => (e.case_id || NONE) === sel.value));
    const at = days.findIndex(d => d.entries.some(e => e.run === focus.run));
    if (at >= 0) S.shownDays = Math.max(S.shownDays, (Math.floor(at / 7) + 1) * 7);
  }
  $('info').textContent = (col.error ? `観察ノートのラベルは読めません（${col.error}） ・ ` : '') +
    (S.data.mock_runs ? `MOCK（合成波形）の計測 ${S.data.mock_runs} 回は日記に含めていません` : '');
  sel.onchange = () => { S.shownDays = 7; try { history.replaceState(null, '', `?case=${encodeURIComponent(sel.value)}`); } catch (_) {} render(); };
  $('onlyLarva').onchange = render;
  $('more').onclick = () => { S.shownDays += 7; render(); };
  render();
  document.querySelector('article.entry.focus')?.scrollIntoView({block: 'center'});
})();
