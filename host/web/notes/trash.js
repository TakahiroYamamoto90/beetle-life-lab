// 観察ノート: deleting a session (into a recoverable trash) and restoring it.
// Deleting appends a history row only: the session, its images, labels and the image files
// under captures/sessions/<session id>/ stay as they are (POST /api/session-delete, -restore).
import {$, api} from './api.js';
import {refresh, state} from './control.js';
import {caseName, sessionKind} from './names.js';

function status(text,error=false){$('deleteStatus').textContent=text;$('deleteStatus').classList.toggle('error',error)}
function current(){return state.sessions.find(s=>s.id===$('sessionSelect').value)}
function label(session){const date=new Date(session.created).toLocaleString('ja-JP');
 return `${session.title||sessionKind(session)} · ${caseName(session.box_id)} · ${date}`}

export function renderTrash(){const session=current(),count=session?state.samples.filter(s=>s.session_id===session.id).length:0;
 $('deleteBtn').disabled=!session;
 $('deleteScope').textContent=session?`対象：${label(session)}（画像 ${count} 枚）`:'セッションが選ばれていません。';
 const items=state.deleted_sessions||[];$('trashPanel').hidden=!items.length;
 $('trashCount').textContent=String(items.length);$('trashRows').replaceChildren();
 for(const item of items){const row=document.createElement('li'),text=document.createElement('span'),button=document.createElement('button');
  text.textContent=`${label(item)}・画像 ${item.samples} 枚・削除 ${new Date(item.deleted_at).toLocaleString('ja-JP')}${item.delete_reason?`（${item.delete_reason}）`:''}`;
  button.type='button';button.className='secondary';button.textContent='元に戻す';button.addEventListener('click',()=>restore(item.id));
  row.append(text,button);$('trashRows').append(row)}}

export async function deleteSession(){const session=current();if(!session)return;
 const count=state.samples.filter(s=>s.session_id===session.id).length;
 status('削除しています…');
 try{await api('/api/session-delete',{session_id:session.id,reason:$('deleteReason').value.trim(),editor:$('reviewer').value.trim()});
  $('deleteReason').value='';$('deletePanel').open=false;await refresh();
  status(`「${label(session)}」（画像 ${count} 枚）を削除しました。下の「削除したセッション」から元に戻せます。`)}
 catch(e){status(`削除できませんでした：${e.message}`,true)}}

export async function restore(sessionId){status('元に戻しています…');
 try{const result=await api('/api/session-restore',{session_id:sessionId,editor:$('reviewer').value.trim()});
  await refresh(sessionId);status(`セッションを元に戻しました（画像 ${result.samples} 枚、ラベルと履歴もそのままです）。`)}
 catch(e){status(`元に戻せませんでした：${e.message}`,true)}}

export function installTrash(){$('deleteBtn').onclick=deleteSession}
