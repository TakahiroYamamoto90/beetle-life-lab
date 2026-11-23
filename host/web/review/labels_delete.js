// Deleting labels from the graph (right-click / Delete key).
// Split from review.html on 2026-11-14 without behaviour changes.
import {$, api, err, typingTarget} from '../common/api.js';
import {KIND, RANGE_KINDS, S} from './state.js';
import {drawDetail, drawOverview, pxOf} from './plot.js';
import {renderTables} from './tables.js';
import {closeRangeMenu} from './labels_range.js';
import {rangePairs} from './labels_shift.js';

// Delete labels on the graph: right-click (or Delete while the mouse is over the graph) lists
// the labels under the pointer - point lines and range ends within 5 px, and ranges that
// contain the time. A range is deleted as a pair in one RETRACT record; with_history also
// hides the positions a moved label came from. Nothing is removed from the file, and the
// status line offers to put the labels back (as new labels at the same times).
export function labelsAt(c,clientX){if(!c._t||!S.ov)return [];const x=clientX-c.getBoundingClientRect().left,t=c._t(x),out=[],paired=new Set(),near=v=>Math.abs(pxOf(c,v)-x)<=5;
 for(const p of rangePairs()){paired.add(p.s.seq);paired.add(p.e.seq);
  if(near(p.s.t_s)||near(p.e.t_s)||(t>p.s.t_s&&t<p.e.t_s))out.push({name:`${RANGE_KINDS[p.key][2]}（${p.s.t_s.toFixed(3)}〜${p.e.t_s.toFixed(3)} 秒）`,cls:RANGE_KINDS[p.key][3],marks:[p.s,p.e],len:p.e.t_s-p.s.t_s})}
 for(const m of S.ov.review_marks)if(!paired.has(m.seq)&&near(m.t_s))out.push({name:`${KIND[m.kind]||m.kind}（${m.t_s.toFixed(3)} 秒）`,marks:[m],len:0});
 return out.sort((a,b)=>a.len-b.len)}
export function deleteMenu(c,clientX,clientY){const items=labelsAt(c,clientX);if(!items.length)return false;closeRangeMenu();
 const m=document.createElement('div');m.className='selmenu';S.sel={menu:m,del:true};const head=document.createElement('div');head.innerHTML='<b>削除するラベル</b>';m.append(head);
 const r=c.getBoundingClientRect(),hint=document.createElement('div');hint.className='selbox';hint.style.cssText+=';background:rgba(200,40,40,.18);border-color:#c83030;display:none';document.body.append(hint);
 for(const it of items){const bt=document.createElement('button');bt.type='button';if(it.cls)bt.className=it.cls;bt.textContent='🗑 '+it.name;
  bt.onmouseenter=bt.onfocus=()=>{const a=r.left+pxOf(c,it.marks[0].t_s),b=r.left+pxOf(c,it.marks[it.marks.length-1].t_s);Object.assign(hint.style,{display:'block',left:(Math.min(a,b)-1)+'px',width:Math.max(2,Math.abs(b-a)+2)+'px',top:r.top+'px',height:r.height+'px'})};
  bt.onclick=()=>{closeRangeMenu();deleteLabels(it)};m.append(bt)}
 const cancel=document.createElement('button');cancel.type='button';cancel.textContent='やめる（Esc）';cancel.onclick=closeRangeMenu;m.append(cancel);
 const note=document.createElement('small');note.className='muted';note.textContent='記録は追記で残ります。直後なら「元に戻す」で戻せます';m.append(note);
 document.body.append(m);const w=m.offsetWidth,h=m.offsetHeight;m.style.left=Math.min(innerWidth-w-8,clientX+8)+'px';m.style.top=Math.min(innerHeight-h-8,clientY+8)+'px';m.querySelector('button').focus();return true}
export async function deleteLabels(it){const seqs=it.marks.map(x=>x.seq);$('saveStatus').textContent='削除中…';
 try{const r=await api('/api/review/annotate',{blind:S.blind,run:S.run,kind:'RETRACT',target_seqs:seqs,with_history:true});S.ov.review_marks=r.review_marks;
  const st=$('saveStatus');st.textContent=`${it.name} を削除しました `;const undo=document.createElement('button');undo.type='button';undo.textContent='元に戻す';
  undo.onclick=async()=>{undo.disabled=true;try{let r2;for(const x of it.marks)r2=await api('/api/review/annotate',{blind:S.blind,run:S.run,kind:x.kind,t_s:x.t_s,note:x.note||''});S.ov.review_marks=r2.review_marks;st.textContent=`${it.name} を戻しました（新しいラベルとして追記）`}
   catch(e){err('戻せません: '+e.message+'（途中まで戻った可能性があります。表で確認してください）')}renderTables();drawOverview();drawDetail()};st.append(undo)}
 catch(e){$('saveStatus').textContent='';err('削除できません: '+e.message)}renderTables();drawOverview();drawDetail()}
export function graphMenu(e){if(e.ctrlKey||e.metaKey||S.edge||S.shift)return;if(deleteMenu(e.currentTarget,e.clientX,e.clientY))e.preventDefault()}

// Top-level listeners of this part; main.js calls install() once, in the original order.
export function install(){
document.addEventListener('keydown',e=>{if((e.key!=='Delete'&&e.key!=='Backspace')||!S.hover||S.sel||S.edge||S.shift||typingTarget(document.activeElement))return;
 const h=S.hover;if(!h.c.matches(':hover'))return;if(deleteMenu(h.c,h.x,h.y))e.preventDefault()});
}
