// 観察エピソード: deleting a saved AD3 record into a recoverable trash, and restoring it.
// Only trash.jsonl next to the run is appended to (POST /api/review/delete, /restore); the
// waveform, candidates, video, labels and the XIAO's own Evidence log stay as they are.
import {api} from '../common/api.js';
import {$, S, clock, el, localTime} from './util.js';

function status(text,error=false){$('trashStatus').textContent=text;$('trashStatus').classList.toggle('error',error)}
function label(r){return `${localTime(r.started_at_pc)} ・ ${clock(r.duration_s)}`+(r.case_id?` ・ ${r.case_id}`:'')+(r.note?` ・ ${r.note}`:'')}

export async function renderTrash(){let runs=[];try{({runs}=await api('/api/review/trash'))}catch(e){status('削除した記録の一覧を読めません: '+e.message,true)}
 $('trashPanel').hidden=!runs.length;$('trashCount').textContent=String(runs.length);$('trashRows').replaceChildren();
 for(const r of runs){const row=el('li'),button=el('button','secondary','元に戻す');button.type='button';
  button.onclick=()=>restore(r.run);
  row.append(el('span',null,`${label(r)} ・ 削除 ${localTime(r.deleted_at_pc)}`+(r.reason?`（${r.reason}）`:'')),button);$('trashRows').append(row)}}

export function renderDeleteScope(run){$('deleteBtn').disabled=!run;$('deleteScope').textContent=run?`対象：${$('run').selectedOptions[0]?.textContent||run}`:'記録が選ばれていません。'}

export function installTrash(reload){
 $('deleteBtn').onclick=async()=>{const run=S.run;if(!run)return;const text=$('run').selectedOptions[0]?.textContent||run;status('削除しています…');
  try{await api('/api/review/delete',{run,reason:$('deleteReason').value.trim()});$('deleteReason').value='';$('deletePanel').open=false;
   await reload(null);await renderTrash();status(`「${text}」を削除しました。下の「削除した記録」から元に戻せます。`)}
  catch(e){status('削除できませんでした: '+e.message,true)}};
 restore=async run=>{status('元に戻しています…');
  try{await api('/api/review/restore',{run});await reload(run);await renderTrash();status('記録を元に戻しました。答え合わせのラベルや動画もそのままです。')}
  catch(e){status('元に戻せませんでした: '+e.message,true)}}}
let restore=async()=>{};
