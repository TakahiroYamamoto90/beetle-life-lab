// 観察ノート: moving images to another (or a new) case, and back to the case recorded at capture.
// Each move is appended to the image's case history; the capture session and its recorded case
// never change (POST /api/case-assign).
import {$, api} from './api.js';
import {refresh, selected, showImage, state} from './control.js';
import {renderGallery} from './gallery.js';
import {caseName} from './names.js';

export const picked=new Set();
export function sessionSamples(){return state.samples.filter(s=>s.session_id===$('sessionSelect').value)}
export function togglePick(id,on){if(on)picked.add(id);else picked.delete(id);renderMovePanel()}
export function clearPicks(){picked.clear();renderMovePanel()}
// Which images the move applies to: the ticked ones, else every image of the session.
export function moveTargets(){const inSession=sessionSamples(),ticked=inSession.filter(s=>picked.has(s.id));return ticked.length?ticked:inSession}
export function moveStatus(text,error=false){$('moveStatus').textContent=text;$('moveStatus').classList.toggle('error',error)}
export function renderMovePanel(){const items=sessionSamples(),ticked=items.filter(s=>picked.has(s.id)).length,targets=moveTargets(),corrected=items.filter(s=>s.case_corrected);
 $('moveScope').textContent=!items.length?'このセッションには画像がありません。':ticked?`チェックした ${ticked} 枚が対象です。`:`チェックがないので、このセッションの ${items.length} 枚すべてが対象です。`;
 const moved=new Map();for(const s of corrected)moved.set(s.case_id,(moved.get(s.case_id)||0)+1);
 $('moveSummary').textContent=corrected.length?'訂正済み：'+[...moved].map(([id,n])=>`${caseName(id)} へ ${n} 枚`).join('・')+'（撮影時のケースは記録に残っています）':'';
 $('moveBtn').disabled=!targets.length;$('moveBackBtn').disabled=!targets.some(s=>s.case_corrected);
 $('pickNone').disabled=!ticked;$('pickAll').disabled=!items.length||ticked===items.length}
export async function moveCase(back){const targets=moveTargets(),target=$('moveCase').value.trim();
 if(!targets.length)return;
 if(!back&&!target){moveStatus('移す先のケースIDを入力するか、一覧から選んでください。',true);return}
 const isNew=!back&&!(state.cases||[]).some(c=>c.case_id===target);
 moveStatus('移しています…');
 try{const result=await api('/api/case-assign',{sample_ids:targets.map(s=>s.id),case_id:back?null:target,reason:$('moveReason').value.trim(),editor:$('reviewer').value.trim()});
  const keep=selected&&!selected.preview?selected.id:null;picked.clear();await refresh();
  const again=keep&&state.samples.find(s=>s.id===keep);if(again&&selected?.id===keep)showImage(again);
  const n=result.moved.length,same=result.unchanged.length;
  moveStatus(back?`${n} 枚を撮影時のケースに戻しました${same?`（${same} 枚はもともと撮影時のケース）`:''}。変更履歴も記録しています。`:
   `${n} 枚を ${caseName(target)} へ移しました${same?`（${same} 枚はすでに ${target}）`:''}。`+(isNew?`新しいケース ${target} の役割（学習用／評価用）は「データの流れ・ケースの役割」で設定してください。`:'変更履歴も記録しています。'));
  if(isNew)$('flowPanel').open=true}
 catch(e){moveStatus(`移せませんでした：${e.message}`,true)}}
export function caseLine(item){return item.case_corrected?`ケース：${caseName(item.case_id)}（撮影時 ${item.recorded_case_id||'未指定'} から訂正） / `:''}
export function installMoves(){$('moveBtn').onclick=()=>moveCase(false);$('moveBackBtn').onclick=()=>moveCase(true);
 $('pickAll').onclick=()=>{for(const s of sessionSamples())picked.add(s.id);renderGallery();renderMovePanel()};
 $('pickNone').onclick=()=>{picked.clear();renderGallery();renderMovePanel()}}
