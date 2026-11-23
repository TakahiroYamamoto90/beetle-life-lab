// Annotation progress of the open run: 未着手 → 作業中 (any saved change) → 完了 (only by this button).
// 完了 and reopening append to review_status.jsonl (POST /api/review/status); labels never change it.
import {$, api, err} from '../common/api.js';
import {STATUS_JA, setOptionStatus} from '../common/review_status.js';
import {S} from './state.js';

export function renderStatus(){const st=S.ov&&S.ov.review_status;$('statusBox').hidden=!st;if(!st)return;
 const tag=$('statusTag');tag.textContent=STATUS_JA[st.status]||st.status;tag.className='status s-'+st.status;
 $('statusBtn').textContent=st.status==='DONE'?'完了を取り消す（作業中に戻す）':'アノテーション完了にする';
 $('statusBtn').title=st.status==='DONE'?'':'この記録の正解ラベル付けが終わったら押します。あとで取り消せます';
 setOptionStatus($('run'),S.run,st.status)}

export function install(){
 $('statusBtn').addEventListener('click',async()=>{if(!S.ov)return;const run=S.run,status=S.ov.review_status.status==='DONE'?'IN_PROGRESS':'DONE';$('statusBtn').disabled=true;
  try{const r=await api('/api/review/status',{run,status});if(S.run===run){S.ov.review_status=r.review_status;renderStatus();
   $('saveStatus').textContent=status==='DONE'?'アノテーションを完了にしました':'完了を取り消しました（作業中）'}}
  catch(e){err('状態を変更できません: '+e.message)}finally{$('statusBtn').disabled=false}});
 // Every saved label, removal or video offset answers with the run's status (未着手 → 作業中).
 addEventListener('bll-posted',e=>{const {url,body,data}=e.detail;if(url==='/api/review/annotate'&&data.review_status&&S.ov&&body.run===S.run){S.ov.review_status=data.review_status;renderStatus()}})}
