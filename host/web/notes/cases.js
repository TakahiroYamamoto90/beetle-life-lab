// 観察ノート: case table, session info and titles.
// Split from vision_capture/index.html on 2026-11-14 without behaviour changes.
import {$, api, message} from './api.js';
import {buttons, clearSelection, refresh, selected, state} from './control.js';
import {renderGallery} from './gallery.js';
import {renderMovePanel, clearPicks} from './moves.js';
import {renderTrash} from './trash.js';
import {CASE_ROLE_TEXT, badge, caseName, caseOf, isLoop, poseName, sessionCaseText, sessionKind, trainedOf} from './names.js';
let shownSession=null;

export function sessionInfo(){const session=state.sessions.find(s=>s.id===$('sessionSelect').value);
 if(shownSession!==$('sessionSelect').value){shownSession=$('sessionSelect').value;clearPicks()}
 // The left pane must never show an image of another session than the one selected.
 if(selected&&!selected.preview&&selected.session_id!==$('sessionSelect').value)clearSelection();
 $('sessionInfo').textContent=session?`${sessionKind(session)} / ${sessionCaseText(session)} / ${poseName(session.camera_pose)} / 作成 ${new Date(session.created).toLocaleString('ja-JP')}`:'新しいセッションを作成してください。';
 $('sessionBadges').replaceChildren();$('sessionTitle').value=session?.title||'';$('sessionTitle').disabled=!session;
 if(session){const c=caseOf(session.box_id),role=c?.role||'unassigned';
  $('sessionBadges').append(session.purpose==='setup'?badge('設置・動作確認（データに使わない）','warn'):isLoop(session)?badge('D 実地検証（観測ループ）','loop'):badge('A データ収集（手動撮影）'));
  if(session.box_id)$('sessionBadges').append(badge(`ケース: ${CASE_ROLE_TEXT[role]}`,role==='evaluation'?'eval':role==='training'?'train':'warn'))}
 renderGallery();renderMovePanel();renderTrash();buttons()}
export function caseCounts(caseId){const items=state.samples.filter(s=>s.case_id===caseId);return {total:items.length,unreviewed:items.filter(s=>s.label==='unreviewed').length,trained:items.filter(s=>trainedOf(s).length).length}}
export function renderCases(){$('caseRows').replaceChildren();for(const c of state.cases||[]){const row=document.createElement('tr'),counts=caseCounts(c.case_id);
  const name=document.createElement('input');name.maxLength=120;name.value=c.display_name||'';name.placeholder='例: 容器1（学習用）';name.setAttribute('aria-label',`${c.case_id}の表示名`);
  const role=document.createElement('select');role.setAttribute('aria-label',`${c.case_id}の役割`);for(const [value,text] of Object.entries(CASE_ROLE_TEXT))role.add(new Option(text,value));role.value=c.role||'unassigned';
  const save=document.createElement('button');save.type='button';save.className='secondary';save.textContent='保存';save.addEventListener('click',()=>saveCase(c.case_id,name.value,role.value));
  const cells=[name,c.case_id,role,`${counts.total}枚`,`${counts.unreviewed}枚`,counts.trained?`${counts.trained}枚`:'—',save];
  for(const value of cells){const td=document.createElement('td');if(typeof value==='string')td.textContent=value;else td.append(value);row.append(td)}
  $('caseRows').append(row)}
 const models=state.training_models||[];$('trainingModels').textContent=models.length?'学習済みの印: '+models.map(m=>m.status==='OK'?`${m.model} ${m.images}枚`:`${m.model}（学習画像の一覧が見つからないため不明）`).join(' / ')+'。学習済みの画像を含むケースは評価用にできません。':'学習済みの印: 学習画像の一覧がありません（不明）。'}
export async function saveCase(caseId,displayName,role){$('caseStatus').textContent='保存しています…';try{await api('/api/case-profile',{case_id:caseId,display_name:displayName,role,editor:$('reviewer').value.trim()});await refresh();$('caseStatus').textContent=`保存しました：${caseName(caseId)}・${CASE_ROLE_TEXT[role]}（変更履歴も記録しています）`}catch(e){$('caseStatus').textContent=`保存できませんでした：${e.message}`}}
export async function saveSessionTitle(){const id=$('sessionSelect').value;if(!id)return;try{await api('/api/session-title',{session_id:id,title:$('sessionTitle').value,editor:$('reviewer').value.trim()});await refresh(id);message('セッション名を保存しました。記録した撮影条件・IDは変わりません。')}catch(e){message(`セッション名を保存できませんでした：${e.message}`,true)}}
