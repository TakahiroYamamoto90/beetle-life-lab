// Label tables and the per-row retract.
// Split from review.html on 2026-11-14; note editing added 2026-11-14.
import {$, api, err, localTime} from '../common/api.js';
import {KIND, RANGE_KINDS, S} from './state.js';
import {drawDetail, drawOverview} from './plot.js';
import {setCursor} from './view.js';

export function renderTables(){const ov=S.ov,rows=$('reviewRows'),live=$('liveRows');rows.replaceChildren();live.replaceChildren();
 for(const m of [...ov.review_marks].sort((a,b)=>a.t_s-b.t_s)){const tr=rows.insertRow();
  const go=document.createElement('a');go.href='#';go.textContent=m.t_s.toFixed(3);go.onclick=e=>{e.preventDefault();setCursor(m.t_s)};tr.insertCell().append(go);
  tr.insertCell().textContent=(KIND[m.kind]||m.kind)+(m.replaces_seq!=null&&m.moved_from_t_s!=null?`（${m.moved_from_t_s.toFixed(3)}秒から移動）`:'')+(m.blind?'（ブラインド）':'');noteCell(tr.insertCell(),m);tr.insertCell().textContent=localTime(m.created_at_pc);
  const b=document.createElement('button');b.type='button';b.textContent='取消';b.onclick=()=>retract(m.seq);tr.insertCell().append(b)}
 if(!ov.review_marks.length)rows.insertRow().insertCell().textContent='まだありません';
 for(const m of ov.live_marks){const tr=live.insertRow();const go=document.createElement('a');go.href='#';go.textContent=(+m.t_s).toFixed(2);go.onclick=e=>{e.preventDefault();setCursor(m.t_s)};tr.insertCell().append(go);tr.insertCell().textContent=KIND[m.kind]||m.kind}
 if(!ov.live_marks.length)live.insertRow().insertCell().textContent='なし'}
// The range a START/END row belongs to (both ends share the note), else just this label.
function noteTargets(m){for(const [sk,ek] of Object.values(RANGE_KINDS)){if(m.kind!==sk&&m.kind!==ek)continue;let open=null;
 for(const x of S.ov.review_marks.filter(x=>x.kind===sk||x.kind===ek).sort((a,b)=>a.t_s-b.t_s)){if(x.kind===sk)open=x;else if(open){if(open.seq===m.seq||x.seq===m.seq)return [open,x];open=null}}}return [m]}
function noteCell(td,m){td.replaceChildren();const text=document.createElement('span');text.textContent=m.note||'';td.append(text);
 if(m.note_changed_from!=null){const was=document.createElement('small');was.className='muted';was.textContent='（修正）';was.title=`前のメモ: ${m.note_changed_from||'（なし）'}`;td.append(was)}
 const edit=document.createElement('button');edit.type='button';edit.className='noteedit';edit.textContent='メモ修正';edit.onclick=()=>editNote(td,m);td.append(edit)}
function editNote(td,m){const targets=noteTargets(m);td.replaceChildren();
 const input=document.createElement('input');input.maxLength=300;input.size=28;input.value=m.note||'';
 const save=document.createElement('button');save.type='button';save.textContent='保存';
 const cancel=document.createElement('button');cancel.type='button';cancel.textContent='やめる';cancel.onclick=()=>noteCell(td,m);
 td.append(input,save,cancel);
 if(targets.length>1){const hint=document.createElement('small');hint.className='muted';hint.textContent=' 範囲の開始・終了の両方に付きます';td.append(hint)}
 save.onclick=()=>saveNote(targets,input.value,save);
 input.addEventListener('keydown',e=>{if(e.isComposing)return;if(e.key==='Enter'){e.preventDefault();save.click()}else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();noteCell(td,m)}});
 input.focus()}
// Appends the label again at the same time with the new note (replaces_seq); the old record stays.
// For a range, if the second end fails the first edit is retracted so both keep the old note.
async function saveNote(targets,note,button){if(targets.every(x=>(x.note||'')===note)){renderTables();return}button.disabled=true;$('saveStatus').textContent='保存中…';let first=null,last=null;
 try{for(const x of targets){last=await api('/api/review/annotate',{blind:S.blind,run:S.run,kind:x.kind,t_s:x.t_s,note,replaces_seq:x.seq});first=first||last.record}
  S.ov.review_marks=last.review_marks;$('saveStatus').textContent='メモを修正しました（前のメモも記録に残ります）'}
 catch(e){let undo='';if(first&&targets.length>1&&last.record===first){try{const u=await api('/api/review/annotate',{blind:S.blind,run:S.run,kind:'RETRACT',target_seq:first.seq});S.ov.review_marks=u.review_marks;undo='（片側だけ変わらないよう取消を記録しました）'}catch(_){undo='（片側の取消にも失敗。表を確認してください）'}}
  $('saveStatus').textContent='';err('メモを保存できません: '+e.message+undo)}
 renderTables();drawOverview();drawDetail()}
export async function retract(seq){try{const r=await api('/api/review/annotate',{blind:S.blind,run:S.run,kind:'RETRACT',target_seq:seq});S.ov.review_marks=r.review_marks;$('saveStatus').textContent='取消を記録しました';renderTables();drawOverview();drawDetail()}catch(e){err('取消できません: '+e.message)}}
