// デモ再生（/demo）: the Physical AI state-transition diagram with its branches, opened by clicking
// the 6-step navigator (docs/Beetle_Life_Lab_Development_Demo_UI_Directive_20261120.md §3.3).
// The path drawn is derived only from the episode's marks (timeline.js), so the diagram shows the
// order of the log; where a stage has no record of its own it is passed through (as in the navigator).
// It is a concept view of the loop, not the firmware's internal state machine.

export const FLOW_NODES = {
  MON: {x: 130, y: 200, label: '見守り', sub: '振動の手がかりを待つ', kind: 'state'},
  SEN: {x: 390, y: 200, label: '① 変化に気づく', sub: 'SENSE ・ 低周波の活動（フォレスト・比）', kind: 'observe'},
  MEM: {x: 650, y: 200, label: '② これまでを確認', sub: 'REMEMBER ・ Evidence / Reducer', kind: 'observe'},
  DEC: {x: 920, y: 200, label: '③ 次を決める', sub: 'DECIDE ・ ルールベースの方策', kind: 'decide'},
  ACT: {x: 1190, y: 200, label: '④ カメラを動かす', sub: 'ACT ・ サーボ', kind: 'act'},
  OBS: {x: 1190, y: 420, label: '⑤ 写真で確かめる', sub: 'OBSERVE ・ Vision V2', kind: 'observe'},
  REC: {x: 920, y: 420, label: '⑥ 観察を記録', sub: 'RECORD ・ Evidence / 観察日記', kind: 'done'},
  FOL: {x: 650, y: 420, label: '続けて見る？', sub: '同じ揺れの続きを判断', kind: 'decide'},
  HOME: {x: 390, y: 420, label: '基本位置に戻る', sub: 'PARK ・ アームを前へ', kind: 'act'},
  WAIT: {x: 920, y: 48, label: '待機（撮らない）', sub: '理由を記録して見守りへ', kind: 'wait'},
};
// [from, to, label, polyline points (optional), label position (optional)]
export const FLOW_EDGES = [
  ['MON', 'SEN', '振動候補'],
  ['SEN', 'MEM', ''],
  ['MEM', 'DEC', ''],
  ['DEC', 'ACT', 'CAPTURE（撮る）'],
  // WAIT sits above ③ (a short loop back to monitoring along the top), which keeps the diagram low
  ['DEC', 'WAIT', 'WAIT（待つ）', null, [800, 128]],
  ['ACT', 'OBS', ''],
  ['OBS', 'REC', ''],
  ['REC', 'FOL', ''],
  ['FOL', 'MEM', '次の方向を評価'],
  ['FOL', 'HOME', '終わり'],
  ['HOME', 'MON', '', [[285, 420], [150, 420], [150, 242]]],
  ['WAIT', 'MON', '', [[815, 48], [100, 48], [100, 158]]],
];
export const FLOW_TEXT = {
  MON: '振動センサーの反応を待っています。反応がなければ動きません。',
  SEN: 'ケース内の振動に変化が現れ、観察のきっかけになりました（振動源はまだわかりません）。',
  MEM: 'MCU が候補をログに保存し、これまでの観察（いつ・どこを見たか、装置の状態）を確認します。',
  DEC: '決まったルールで、撮るか待つか、どの方向を見るかを選びます。画像AIが少し前に幼虫を見た方向があれば、まずそこを見ます（学習済みAIではありません）。',
  ACT: 'サーボアームでカメラを選んだ方向へ動かします。',
  OBS: 'Vision V2 で撮影し、画像AIが幼虫が写っているかを判定します。見えたら残りの方向は撮りません。',
  REC: '写真・判断・理由を記録します。観察日記に残ります。',
  FOL: '同じ揺れの続きとして、まだ見ていない方向を見るかを決めます。',
  HOME: '観察を終え、アームを基本位置（前）へ戻します。',
  WAIT: 'カメラを動かした直後などは撮らずに待ちます。理由も記録します。',
};

/** Node sequence passed up to `mark` (inclusive) for the episode marks; [] before the candidate. */
export function flowPath(marks, mark) {
  const out = ['MON'], push = (...ids) => { for (const id of ids) if (out[out.length - 1] !== id) out.push(id); };
  if (!mark) return out;
  for (const m of marks) {
    const last = out[out.length - 1];
    switch (m.kind) {
      case 'SENSE': push('SEN'); break;
      case 'DECIDE': push(...(last === 'REC' ? ['FOL'] : []), 'MEM', 'DEC'); break;
      case 'WAIT': push(...(last === 'REC' ? ['FOL'] : []), 'MEM', 'DEC', 'WAIT'); break;
      case 'MOVE': push('ACT'); break;
      case 'CAPTURE': push('ACT', 'OBS'); break;
      case 'RESULT': push('REC'); break;
      case 'PARK': push('FOL', 'HOME'); break;
      case 'END': push(...(last === 'REC' ? ['FOL', 'HOME'] : last === 'DEC' ? ['WAIT'] : []), 'MON'); break;
    }
    if (m === mark) break;
  }
  return out;
}

/** The time each node was first entered within the episode (for jumping to it). */
export function flowTimes(marks) {
  const times = {};
  for (const m of marks) {
    const path = flowPath(marks, m);
    for (const id of path.slice(1)) if (times[id] == null) times[id] = m.t;
  }
  return times;
}

const NS = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs = {}, text) => { const e = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v); if (text != null) e.textContent = text; return e; };
const W = 210, H = 84;

function edgePoints([a, b, , pts]) {
  if (pts) return pts;
  const A = FLOW_NODES[a], B = FLOW_NODES[b];
  // from the side of A facing B to the side of B facing A
  const dx = B.x - A.x, dy = B.y - A.y;
  if (Math.abs(dx) >= Math.abs(dy)) {
    const s = Math.sign(dx);
    return [[A.x + s * W / 2, A.y], [B.x - s * W / 2, B.y]];
  }
  const s = Math.sign(dy);
  return [[A.x, A.y + s * H / 2], [B.x, B.y - s * H / 2]];
}

/** Build the diagram inside `svg`; onNode(id) is called when a state is clicked. */
export function buildFlow(svg, onNode) {
  svg.replaceChildren();
  svg.setAttribute('viewBox', '0 0 1320 480');
  const defs = svgEl('defs');
  for (const [id, color] of [['arr', '#5d7a78'], ['arrOn', '#f2c14e']]) {
    const m = svgEl('marker', {id, viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse'});
    m.append(svgEl('path', {d: 'M0,0 L10,5 L0,10 z', fill: color})); defs.append(m);
  }
  svg.append(defs);
  for (const e of FLOW_EDGES) {
    const pts = edgePoints(e), g = svgEl('g', {class: 'fedge', 'data-edge': `${e[0]}>${e[1]}`});
    g.append(svgEl('polyline', {points: pts.map(p => p.join(',')).join(' '), 'marker-end': 'url(#arr)'}));
    if (e[2] && e[4]) g.append(svgEl('text', {x: e[4][0], y: e[4][1], 'text-anchor': 'start', class: 'elabel'}, e[2]));
    else if (e[2]) {
      const [p, q] = [pts[0], pts[pts.length - 1]], vertical = Math.abs(q[1] - p[1]) > Math.abs(q[0] - p[0]);
      // horizontal labels sit above the row of states, so they never cover a state
      g.append(svgEl('text', {x: (p[0] + q[0]) / 2 + (vertical ? 14 : 0), y: (p[1] + q[1]) / 2 + (vertical ? 4 : -62),
        'text-anchor': vertical ? 'start' : 'middle', class: 'elabel'}, e[2]));
    }
    svg.append(g);
  }
  for (const [id, n] of Object.entries(FLOW_NODES)) {
    const g = svgEl('g', {class: `fnode ${n.kind}`, 'data-node': id, tabindex: 0, role: 'button'});
    if (n.kind === 'decide') g.append(svgEl('polygon', {points: `${n.x},${n.y - 56} ${n.x + 118},${n.y} ${n.x},${n.y + 56} ${n.x - 118},${n.y}`}));
    else g.append(svgEl('rect', {x: n.x - W / 2, y: n.y - H / 2, width: W, height: H, rx: 16}));
    g.append(svgEl('text', {x: n.x, y: n.y - 4, class: 'nlabel', 'text-anchor': 'middle'}, n.label));
    g.append(svgEl('text', {x: n.x, y: n.y + 22, class: 'nsub', 'text-anchor': 'middle'}, n.sub));
    // pass count: top right of a box, upper right of a diamond (clear of the arrows above it)
    g.append(svgEl('text', {x: n.x + (n.kind === 'decide' ? 92 : W / 2 - 10), y: n.y - (n.kind === 'decide' ? 30 : H / 2 + 8), class: 'ncount', 'text-anchor': n.kind === 'decide' ? 'start' : 'end'}));
    g.addEventListener('click', () => onNode(id));
    g.addEventListener('keydown', ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); onNode(id); } });
    svg.append(g);
  }
}

/** Highlight: path = node ids passed (last = current). Counts how often each node was entered. */
export function paintFlow(svg, path) {
  const cur = path[path.length - 1], seen = new Set(path), counts = {};
  for (const id of path) counts[id] = (counts[id] || 0) + 1;
  const edges = new Map();
  for (let i = 1; i < path.length; i++) { const k = `${path[i - 1]}>${path[i]}`; edges.set(k, i); }
  const lastEdge = path.length > 1 ? `${path[path.length - 2]}>${cur}` : null;
  for (const g of svg.querySelectorAll('.fnode')) {
    const id = g.dataset.node;
    g.classList.toggle('cur', id === cur); g.classList.toggle('seen', seen.has(id) && id !== cur);
    g.querySelector('.ncount').textContent = counts[id] > 1 ? `×${counts[id]}` : '';
  }
  for (const g of svg.querySelectorAll('.fedge')) {
    const k = g.dataset.edge, on = k === lastEdge;
    g.classList.toggle('cur', on); g.classList.toggle('seen', edges.has(k) && !on);
    g.querySelector('polyline').setAttribute('marker-end', on ? 'url(#arrOn)' : 'url(#arr)');
  }
}
