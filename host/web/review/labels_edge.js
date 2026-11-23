// Dragging a saved range end or point label line.
// Split from review.html on 2026-11-14 without behaviour changes.
import {$, api, err} from '../common/api.js';
import {EDGE_KINDS, KIND, PAD, S} from './state.js';
import {drawDetail, drawOverview, pxOf} from './plot.js';
import {renderTables} from './tables.js';
import {rangeAt, rangePairs} from './labels_shift.js';

export function edgeAt(c,clientX){if(!c._t||!S.ov)return null;const x=clientX-c.getBoundingClientRect().left;let best=null;
 for(const m of S.ov.review_marks)if(m.kind in EDGE_KINDS){const d=Math.abs(pxOf(c,m.t_s)-x);if(d<=5&&(!best||d<best.d))best={m,d}}return best&&best.m}
export function edgeLimits(m){const end=S.ov.duration_s;if(!EDGE_KINDS[m.kind])return [0,end-1e-5];const [sk,ek]=EDGE_KINDS[m.kind];let open=null,lo=0,hi=end;
 for(const x of S.ov.review_marks.filter(x=>x.kind===sk||x.kind===ek).sort((a,b)=>a.t_s-b.t_s)){
  if(x.kind===sk)open=x;else if(open){if(open.seq===m.seq)hi=x.t_s-.02;if(x.seq===m.seq)lo=open.t_s+.02;open=null}}
 const mine=rangePairs().find(p=>p.s.seq===m.seq||p.e.seq===m.seq);
 if(mine)for(const q of rangePairs())if(q.key===mine.key&&q!==mine){if(q.e.t_s<=mine.s.t_s)lo=Math.max(lo,q.e.t_s+.02);if(q.s.t_s>=mine.e.t_s)hi=Math.min(hi,q.s.t_s-.02)}
 return [Math.max(0,lo),Math.min(end-1e-5,hi)]}
export function cancelEdge(){if(!S.edge)return;S.edge.line.remove();S.edge.tip.remove();S.edge=null}
export function startEdge(e){const c=e.currentTarget;if(e.button!==0||e.ctrlKey||e.metaKey||S.sel)return;const m=edgeAt(c,e.clientX);if(!m)return;e.preventDefault();
 const r=c.getBoundingClientRect(),[lo,hi]=edgeLimits(m),line=document.createElement('div'),tip=document.createElement('div');line.className='edgeline';tip.className='edgetip';document.body.append(line,tip);
 S.edge={m,c,r,lo,hi,t:m.t_s,x0:e.clientX,moved:false,line,tip};moveEdge(e.clientX)}
export function moveEdge(clientX){const g=S.edge,c=g.c,r=g.r;const t=Math.max(g.lo,Math.min(g.hi,c._t(Math.max(PAD.l,Math.min(r.width-PAD.r,clientX-r.left)))));g.t=t;
 const x=r.left+pxOf(c,t);Object.assign(g.line.style,{left:x+'px',top:r.top+'px',height:r.height+'px'});
 g.tip.textContent=`${KIND[g.m.kind]} ${g.m.t_s.toFixed(3)} → ${t.toFixed(3)} 秒`;Object.assign(g.tip.style,{left:Math.min(innerWidth-220,x+6)+'px',top:Math.max(0,r.top-22)+'px'})}
export function hoverEdge(e){const c=e.currentTarget;S.hover={c,x:e.clientX,y:e.clientY};if(S.edge||S.sel||S.shift)return;c.style.cursor=e.ctrlKey?'':edgeAt(c,e.clientX)?'ew-resize':rangeAt(c,e.clientX)?'grab':''}

// Top-level listeners of this part; main.js calls install() once, in the original order.
export function install(){
document.addEventListener('mousemove',e=>{if(!S.edge)return;if(Math.abs(e.clientX-S.edge.x0)>2)S.edge.moved=true;moveEdge(e.clientX)});
document.addEventListener('mouseup',async e=>{const g=S.edge;if(!g)return;S.suppressClick=true;setTimeout(()=>{S.suppressClick=false},0);cancelEdge();
 if(!g.moved||Math.abs(g.t-g.m.t_s)<.001)return;$('saveStatus').textContent='移動を保存中…';
 try{const r=await api('/api/review/annotate',{blind:S.blind,run:S.run,kind:g.m.kind,t_s:g.t,note:g.m.note||'',replaces_seq:g.m.seq});S.ov.review_marks=r.review_marks;
  $('saveStatus').textContent=`${KIND[g.m.kind]} を ${g.m.t_s.toFixed(3)} → ${r.record.t_s.toFixed(3)} 秒に移動しました（元の記録は履歴に残ります）`}
 catch(err2){$('saveStatus').textContent='';err('移動を保存できません: '+err2.message)}renderTables();drawOverview();drawDetail()});
document.addEventListener('keydown',e=>{if(S.edge&&e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();cancelEdge();S.suppressClick=true;setTimeout(()=>{S.suppressClick=false},0)}},true);
}
