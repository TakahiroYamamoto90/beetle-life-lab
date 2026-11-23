// Ctrl+drag range labels, Ctrl+click point labels, and the label buttons (mark).
// Split from review.html on 2026-11-14 without behaviour changes.
import {$, api, err} from '../common/api.js';
import {KIND, PAD, POINT_KINDS, RANGE_KINDS, S} from './state.js';
import {drawDetail, drawOverview} from './plot.js';
import {renderTables} from './tables.js';

// Note box inside a mouse menu, prefilled from the page note field; its value goes with the label.
function menuNote(m){const box=document.createElement('label');box.className='selnote';box.textContent='メモ ';
 const input=document.createElement('input');input.maxLength=300;input.size=28;input.value=$('note').value;input.placeholder='例：アーム戻り（動画で確認）';
 box.append(input);m.append(box);return()=>input.value}
export function closeRangeMenu(){for(const el of document.querySelectorAll('.selbox,.selmenu'))el.remove();S.sel=null}
export async function markRange(key,a,b,note=$('note').value){const [k0,k1,name]=RANGE_KINDS[key];$('saveStatus').textContent='保存中…';let first=null;
 try{first=(await api('/api/review/annotate',{blind:S.blind,run:S.run,kind:k0,t_s:a,note})).record;
  const r=await api('/api/review/annotate',{blind:S.blind,run:S.run,kind:k1,t_s:b,note});S.ov.review_marks=r.review_marks;$('note').value='';
  $('saveStatus').textContent=`${name} を ${first.t_s.toFixed(3)}〜${r.record.t_s.toFixed(3)} 秒に保存しました`}
 catch(e){let undo='';if(first){try{const r=await api('/api/review/annotate',{blind:S.blind,run:S.run,kind:'RETRACT',target_seq:first.seq});S.ov.review_marks=r.review_marks;undo='（開始だけ残らないよう取消を記録しました）'}catch(_){undo='（開始の取消にも失敗。表で取消してください）'}}
  $('saveStatus').textContent='';err('範囲を保存できません: '+e.message+undo)}
 renderTables();drawOverview();drawDetail()}
export function startRange(e){const c=e.currentTarget;if(!(e.ctrlKey||e.metaKey)||e.button!==0||!c._t||!S.ov)return;e.preventDefault();closeRangeMenu();
 const r=c.getBoundingClientRect(),box=document.createElement('div');box.className='selbox';document.body.append(box);
 const clampT=x=>Math.max(0,Math.min(S.ov.duration_s,c._t(Math.max(PAD.l,Math.min(r.width-PAD.r,x-r.left)))));
 S.sel={canvas:c,rect:r,x0:e.clientX,t0:clampT(e.clientX),t1:clampT(e.clientX),box,clampT,moved:false};
 const draw=x=>{const a=Math.min(S.sel.x0,x),b=Math.max(S.sel.x0,x);Object.assign(box.style,{left:a+'px',top:r.top+'px',width:Math.max(1,b-a)+'px',height:r.height+'px'})};draw(e.clientX)}
export function pointMenu(s,e){const t=s.t0,m=document.createElement('div');m.className='selmenu';s.menu=m;s.box.style.width='2px';
 const head=document.createElement('div');head.innerHTML=`<b>${t.toFixed(3)} 秒</b>に点のラベルを付ける`;m.append(head);const note=menuNote(m);
 for(const [kind,name,cls] of POINT_KINDS){const bt=document.createElement('button');bt.type='button';bt.className=cls;bt.textContent=name;bt.onclick=()=>{const n=note();closeRangeMenu();mark(kind,t,n)};m.append(bt)}
 const cancel=document.createElement('button');cancel.type='button';cancel.textContent='やめる（Esc）';cancel.onclick=closeRangeMenu;m.append(cancel);
 const hint=document.createElement('small');hint.className='muted';hint.textContent='範囲で付けるときは Ctrl を押したままドラッグ';m.append(hint);
 document.body.append(m);const w=m.offsetWidth,h=m.offsetHeight;m.style.left=Math.min(innerWidth-w-8,e.clientX+8)+'px';m.style.top=Math.min(innerHeight-h-8,e.clientY+8)+'px';m.querySelector('button').focus()}
export async function mark(kind,at,note=$('note').value){if(!S.ov)return;$('saveStatus').textContent='保存中…';
 try{const r=await api('/api/review/annotate',{blind:S.blind,run:S.run,kind,t_s:at??S.cursor,note});S.ov.review_marks=r.review_marks;$('note').value='';
  $('saveStatus').textContent=`${KIND[kind]} を ${r.record.t_s.toFixed(3)} 秒に保存しました`;renderTables();drawOverview();drawDetail()}catch(e){$('saveStatus').textContent='';err('保存できません: '+e.message)}}

// Top-level listeners of this part; main.js calls install() once, in the original order.
export function install(){
document.addEventListener('mousemove',e=>{const s=S.sel;if(!s||s.menu)return;s.t1=s.clampT(e.clientX);if(Math.abs(e.clientX-s.x0)>3)s.moved=true;
 const a=Math.min(s.x0,e.clientX),b=Math.max(s.x0,e.clientX);Object.assign(s.box.style,{left:a+'px',width:Math.max(1,b-a)+'px'})});
document.addEventListener('mouseup',e=>{const s=S.sel;if(!s||s.menu)return;S.suppressClick=true;setTimeout(()=>{S.suppressClick=false},0);
 const a=Math.min(s.t0,s.t1),b=Math.max(s.t0,s.t1);if(!s.moved){pointMenu(s,e);return}if(b-a<.02){closeRangeMenu();return}
 const m=document.createElement('div');m.className='selmenu';s.menu=m;const head=document.createElement('div');head.innerHTML=`<b>${a.toFixed(3)}〜${b.toFixed(3)} 秒</b>（${(b-a).toFixed(2)} 秒）にラベルを付ける`;m.append(head);const note=menuNote(m);
 for(const [key,[,,name,cls]] of Object.entries(RANGE_KINDS)){const bt=document.createElement('button');bt.type='button';bt.className=cls;bt.textContent=`${name}（開始〜終了）`;bt.onclick=()=>{const n=note();closeRangeMenu();markRange(key,a,b,n)};m.append(bt)}
 const cancel=document.createElement('button');cancel.type='button';cancel.textContent='やめる（Esc）';cancel.onclick=closeRangeMenu;m.append(cancel);
 const hint=document.createElement('small');hint.className='muted';hint.textContent='メモは開始・終了の両方に付きます';m.append(hint);
 document.body.append(m);const w=m.offsetWidth,h=m.offsetHeight;m.style.left=Math.min(innerWidth-w-8,e.clientX+8)+'px';m.style.top=Math.min(innerHeight-h-8,e.clientY+8)+'px';m.querySelector('button').focus()});
document.addEventListener('keydown',e=>{if(S.sel&&e.key==='Escape'){e.preventDefault();e.stopImmediatePropagation();closeRangeMenu()}},true);
document.addEventListener('mousedown',e=>{if(S.sel&&S.sel.menu&&!e.target.closest('.selmenu'))closeRangeMenu()},true);
addEventListener('scroll',()=>{if(S.sel)closeRangeMenu()},{passive:true});
}
