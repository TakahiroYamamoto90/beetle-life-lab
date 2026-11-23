// 観察ノート: entry point, event wiring and start-up (original order).
// Split from vision_capture/index.html on 2026-11-14 without behaviour changes.
import {lightbox} from '../common/lightbox.js';
import {$, api, message} from './api.js';
import {saveSessionTitle, sessionInfo} from './cases.js';
import {queueSave, refresh, state} from './control.js';
import {renderGallery, renderSessionOptions} from './gallery.js';
import {installMoves} from './moves.js';
import {installTrash} from './trash.js';
import {install_piezo} from './piezo.js';


install_piezo();
installMoves();
installTrash();
$('unreviewedOnly').addEventListener('change',renderGallery);
$('sessionUnreviewedOnly').addEventListener('change',()=>{const before=$('sessionSelect').value;renderSessionOptions(before);if($('sessionSelect').value!==before)sessionInfo()});
$('label').addEventListener('change',()=>queueSave('annotation'));
for(const id of ['reviewer','reviewNote'])$(id).addEventListener('change',()=>queueSave('annotation'));
$('evalOnly').addEventListener('change',()=>queueSave('role'));
$('refreshBtn').onclick=async()=>{try{await refresh();message(state.ports.length?'接続を更新しました。':'MCUが見つかりません。USB接続を確認してください。',!state.ports.length)}catch(e){message(e.message,true)}};
$('sessionForm').onsubmit=async event=>{event.preventDefault();try{const data=Object.fromEntries(new FormData(event.target));const session=await api('/api/sessions',data);await refresh(session.id);$('newSession').open=false;message('セッションを作成しました。保存する画像は全て「未確認」で始まります。')}catch(e){message(e.message,true)}};
$('annotateBtn').onclick=()=>queueSave('annotation');
$('sessionTitleBtn').onclick=saveSessionTitle;
refresh().then(()=>{if(!state.sessions.length)$('collectionSettings').open=true;if((state.cases||[]).some(c=>c.role==='unassigned'))$('flowPanel').open=true;message(state.ports.length?'MCUが見つかりました。まずプレビューで撮影条件を確認してください。':'MCUが見つかりません。USB接続を確認してください。',!state.ports.length)}).catch(e=>message(e.message,true));
$('image').addEventListener('click',()=>{const src=$('image').getAttribute('src');if(!src)return;const items=[...document.querySelectorAll('#gallery .thumb img')].map(im=>({src:im.getAttribute('src'),pose:im.dataset.pose,caption:im.closest('.thumb')?.innerText.replace(/\s*\n\s*/g,'・')||''}));const base=src.split('?')[0],i=items.findIndex(x=>x.src.split('?')[0]===base);lightbox(i>=0?items:[{src,pose:$('image').dataset.pose,caption:$('imageInfo').textContent}],Math.max(0,i))});
