// Dragging inside a saved range to shift the whole range.
// Split from review.html on 2026-11-14 without behaviour changes.
import {$, api, err} from '../common/api.js';
import {PAD, RANGE_KINDS, S} from './state.js';
import {drawDetail, drawOverview, pxOf} from './plot.js';
import {renderTables} from './tables.js';
import {edgeAt} from './labels_edge.js';

// Drag inside a saved 幼虫活動 / 人工刺激（掘削） range to shift the whole range. A press without
// movement stays an ordinary click (cursor move). Saving appends START and END with
// replaces_seq; if END fails, the new START is retracted so the old range comes back.
// The shift stops before the range would overlap or cross another range of the same kind.
export function rangePairs(){const out=[];for(const [key,[sk,ek]] of Object.entries(RANGE_KINDS)){let open=null;
 for(const x of S.ov.review_marks.filter(x=>x.kind===sk||x.kind===ek).sort((a,b)=>a.t_s-b.t_s)){if(x.kind===sk)open=x;else if(open){out.push({key,s:open,e:x});open=null}}}return out}
export function rangeAt(c,clientX){if(!c._t||!S.ov)return null;const t=c._t(clientX-c.getBoundingClientRect().left);let best=null;
 for(const p of rangePairs())if(t>p.s.t_s&&t<p.e.t_s&&(!best||p.e.t_s-p.s.t_s<best.e.t_s-best.s.t_s))best=p;return best}
// Shifts that would overlap another range of the same kind (pairs are made by time order, so
// overlapping would mix up the ends). The drag jumps over such ranges to the nearest free spot
// instead of stopping in front of them; ranges of other kinds never block.
export function shiftLimits(p){const lo=-p.s.t_s,hi=S.ov.duration_s-1e-5-p.e.t_s,blocks=[];
 for(const q of rangePairs())if(q.key===p.key&&q.s.seq!==p.s.seq)blocks.push([q.s.t_s-.02-p.e.t_s,q.e.t_s+.02-p.s.t_s]);
 blocks.sort((a,b)=>a[0]-b[0]);const merged=[];for(const b of blocks){const last=merged[merged.length-1];if(last&&b[0]<=last[1])last[1]=Math.max(last[1],b[1]);else merged.push([...b])}
 return {lo,hi,blocks:merged}}
export function snapShift(lim,d){d=Math.max(lim.lo,Math.min(lim.hi,d));for(const [a,b] of lim.blocks)if(d>a&&d<b){
  const ok=[a,b].filter(v=>v>=lim.lo-1e-9&&v<=lim.hi+1e-9);return ok.length?ok.reduce((x,y)=>Math.abs(y-d)<Math.abs(x-d)?y:x):0}return d}
export function cancelShift(){if(!S.shift)return;if(S.shift.box){S.shift.box.remove();S.shift.tip.remove()}S.shift=null}
export function startShift(e){const c=e.currentTarget;if(e.button!==0||e.ctrlKey||e.metaKey||S.sel||S.edge||edgeAt(c,e.clientX))return;const p=rangeAt(c,e.clientX);if(!p)return;
 S.shift={p,c,r:c.getBoundingClientRect(),x0:e.clientX,t0:c._t(e.clientX-c.getBoundingClientRect().left),d:0,lim:shiftLimits(p),active:false,box:null,tip:null}}
export function drawShift(clientX){const g=S.shift,c=g.c,r=g.r;const t=c._t(Math.max(PAD.l,Math.min(r.width-PAD.r,clientX-r.left)));g.d=snapShift(g.lim,t-g.t0);
 const a=g.p.s.t_s+g.d,b=g.p.e.t_s+g.d,xa=r.left+pxOf(c,a),xb=r.left+pxOf(c,b),name=RANGE_KINDS[g.p.key][2];
 Object.assign(g.box.style,{left:xa+'px',width:Math.max(1,xb-xa)+'px',top:r.top+'px',height:r.height+'px'});
 g.tip.textContent=`${name} ${g.p.s.t_s.toFixed(3)}〜${g.p.e.t_s.toFixed(3)} → ${a.toFixed(3)}〜${b.toFixed(3)} 秒（${g.d>=0?'+':''}${g.d.toFixed(3)}）`;
 Object.assign(g.tip.style,{left:Math.min(innerWidth-330,xa)+'px',top:Math.max(0,r.top-22)+'px'})}
export async function shiftRange(p,d){const name=RANGE_KINDS[p.key][2];$('saveStatus').textContent='移動を保存中…';let first=null;
 try{first=(await api('/api/review/annotate',{blind:S.blind,run:S.run,kind:p.s.kind,t_s:p.s.t_s+d,note:p.s.note||'',replaces_seq:p.s.seq})).record;
  const r=await api('/api/review/annotate',{blind:S.blind,run:S.run,kind:p.e.kind,t_s:p.e.t_s+d,note:p.e.note||'',replaces_seq:p.e.seq});S.ov.review_marks=r.review_marks;
  $('saveStatus').textContent=`${name} を ${first.t_s.toFixed(3)}〜${r.record.t_s.toFixed(3)} 秒へ移動しました（元の記録は履歴に残ります）`}
 catch(e){let undo='';if(first){try{const r=await api('/api/review/annotate',{blind:S.blind,run:S.run,kind:'RETRACT',target_seq:first.seq});S.ov.review_marks=r.review_marks;undo='（開始の移動も取り消し、元の範囲に戻しました）'}catch(_){undo='（開始だけ動いた可能性があります。表で確認してください）'}}
  $('saveStatus').textContent='';err('範囲の移動を保存できません: '+e.message+undo)}
 renderTables();drawOverview();drawDetail()}

// Top-level listeners of this part; main.js calls install() once, in the original order.
export function install(){
document.addEventListener('mousemove',e=>{const g=S.shift;if(!g)return;if(!g.active){if(Math.abs(e.clientX-g.x0)<=3)return;g.active=true;
 g.box=document.createElement('div');g.box.className='selbox';g.box.style.background='rgba(17,17,17,.12)';g.tip=document.createElement('div');g.tip.className='edgetip';document.body.append(g.box,g.tip);document.body.style.cursor='grabbing'}
 drawShift(e.clientX)});
document.addEventListener('mouseup',e=>{const g=S.shift;if(!g)return;document.body.style.cursor='';const active=g.active,d=g.d;cancelShift();if(!active)return;
 S.suppressClick=true;setTimeout(()=>{S.suppressClick=false},0);if(Math.abs(d)>=.001)shiftRange(g.p,d)});
document.addEventListener('keydown',e=>{if(S.shift&&e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();const active=S.shift.active;document.body.style.cursor='';cancelShift();
 if(active){S.suppressClick=true;setTimeout(()=>{S.suppressClick=false},0)}}},true);
}
