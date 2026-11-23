// 観察ノート: page state (state/selected/busy/annotating) and the actions that change it.
// Split from vision_capture/index.html on 2026-11-14 without behaviour changes.
import {$, api, message} from './api.js';
import {renderCases, sessionInfo} from './cases.js';
import {clearCapturePreview, renderGallery, renderImageModelOverlay, renderModelResult, renderRoleNote, renderSessionOptions, reviewStatus} from './gallery.js';
import {caseLine} from './moves.js';
import {caseName, labels, poseName} from './names.js';
import {startPiezo} from './piezo.js';
import {orient} from '../common/orient.js';

export let state={sessions:[],samples:[],ports:[]}, selected=null, busy=false, annotating=false;
// startPiezo (notes/piezo) sets busy through this: imported bindings are read-only.
export function setBusy(value){busy=value}
export function buttons(){const blocked=busy||annotating||state.busy||state.piezo?.running;$('previewBtn').disabled=blocked||!$('portSelect').value;$('saveBtn').disabled=blocked||!$('sessionSelect').value||!$('portSelect').value;$('classifySaveBtn').disabled=blocked||!$('sessionSelect').value||!$('portSelect').value;$('annotateBtn').disabled=busy;$('piezoStartBtn').disabled=blocked||!$('portSelect').value;$('piezoStopBtn').disabled=!state.piezo?.running}
export function clearSelection(){selected=null;$('image').src='';$('image').style.display='none';$('imageModelOverlay').classList.toggle('hidden',true);$('placeholder').textContent='右の一覧から画像を選んでください。';$('placeholder').hidden=false;$('imageTag').textContent='画像未選択';$('imageInfo').textContent='';$('review').classList.toggle('hidden',true)}
export function showImage(item){selected=item;$('placeholder').hidden=true;$('image').style.display='block';const separator=item.image_url.includes('?')?'&':'?';$('image').src=`${item.image_url}${separator}capture=${encodeURIComponent(item.id)}`;orient($('image'),item.metadata?.camera_pose);renderImageModelOverlay(item);$('imageTag').textContent=item.preview?'調整用プレビュー':labels[item.label];$('imageInfo').textContent=`${caseLine(item)}${item.metadata.camera_pose?poseName(item.metadata.camera_pose)+' / ':''}${item.metadata.width}×${item.metadata.height} / ${item.metadata.bytes.toLocaleString()} bytes / ${new Date(item.metadata.received_utc_pc).toLocaleString('ja-JP')} / ${item.id}`;$('review').classList.toggle('hidden',!!item.preview);renderModelResult(item);$('label').value=item.label||'unreviewed';$('reviewer').value=item.reviewer||'';$('reviewNote').value=item.note||'';$('evalOnly').checked=item.dataset_role==='evaluation_only';renderRoleNote(item);reviewStatus(item.reviewed_at?`保存済み：${new Date(item.reviewed_at).toLocaleString('ja-JP')}`:'この画像の確認内容はまだ保存されていません。');renderGallery()}
export async function refresh(preferredSession){const previous=$('sessionSelect').value;const port=$('portSelect').value;state=await api('/api/state');renderSessionOptions(preferredSession||previous);$('caseList').replaceChildren();for(const c of state.cases||[])$('caseList').append(new Option(caseName(c.case_id),c.case_id));renderCases();$('portSelect').replaceChildren();if(!state.ports.length)$('portSelect').add(new Option('MCUが見つかりません',''));for(const p of state.ports)$('portSelect').add(new Option(`${p.port} · ${p.name}`,p.port));if(state.ports.some(p=>p.port===port))$('portSelect').value=port;sessionInfo()}
export async function take(mode){if(busy)return;busy=true;buttons();const progress=mode==='preview'?'プレビューを撮影しています…':mode==='classify_save'?'推論と同じフレームを撮影・判定・保存しています…':'撮影してセッションに保存しています…';clearCapturePreview('新しい画像を取得しています…');$('review').classList.add('hidden');message(progress);try{const item=await api('/api/capture',{mode,port:$('portSelect').value,session_id:$('sessionSelect').value});await refresh();showImage(item);message(mode==='preview'?'プレビューを取得しました。向き・明るさ・距離を確認してください。':mode==='classify_save'?'AI出力付きで保存しました。人の画像ラベルは別に確認してください。':'保存しました。画像を確認してラベルを記録してください。')}catch(e){clearCapturePreview('撮影に失敗しました。前回の画像は表示していません。');$('imageTag').textContent='画像取得失敗';message(`撮影失敗：${e.message}。前回の画像は破棄しました。`,true)}finally{busy=false;buttons()}}
// Autosave: each change is queued with the image it belongs to, so switching images
// mid-save never applies a label to the wrong sample. Saves run in order.
export let saveChain=Promise.resolve(),pendingSaves=0;
export function savedOf(id){return state.samples.find(sample=>sample.id===id)||selected}
export function queueSave(kind){
 if(!selected||selected.preview)return saveChain;
 const id=selected.id,saved=savedOf(id),reviewer=$('reviewer').value.trim();
 const data=kind==='role'?{sample_id:id,role:$('evalOnly').checked?'evaluation_only':'unassigned',reviewer}:{sample_id:id,label:$('label').value,reviewer,note:$('reviewNote').value};
 if(kind==='role'?data.role===(saved.dataset_role||'unassigned'):data.label===(saved.label||'unreviewed')&&reviewer===(saved.reviewer||'')&&data.note===(saved.note||''))return saveChain;
 pendingSaves++;annotating=true;buttons();reviewStatus('保存しています…');
 saveChain=saveChain.then(async()=>{
  try{
   const item=await api(kind==='role'?'/api/dataset-role':'/api/annotate',data);
   state.samples=state.samples.map(sample=>sample.id===item.id?item:sample);
   if(selected?.id===item.id){$('annotateBtn').hidden=true;selected=item;$('imageTag').textContent=labels[item.label];reviewStatus(kind==='role'?`保存しました：${item.dataset_role==='evaluation_only'?'評価専用':'評価専用を解除'}`:`保存しました：${labels[item.label]}。変更履歴も記録しています。`);renderRoleNote(item)}
   renderSessionOptions();renderGallery();
  }catch(e){if(selected?.id===id){$('annotateBtn').hidden=false;reviewStatus(`保存できませんでした：${e.message}。「保存を再試行」で再試行できます。`,true)}else message(`前の画像の保存に失敗しました：${e.message}`,true)}
  finally{pendingSaves--;annotating=pendingSaves>0;buttons()}
 });
 return saveChain;
}
