// 取得画面: entry point and start-up (original order).
// Split from ad3_bridge/index.html on 2026-11-14 without behaviour changes.
import {$, api} from './api.js';
import {loadCollection} from './collection.js';
import {V} from './evidence.js';
import {poll} from './status.js';
import {install_graph} from './graph.js';
import {install_run} from './run.js';
import {install_evidence} from './evidence.js';
import {install_collection} from './collection.js';
import {install_status} from './status.js';

async function loadScenarios(){try{const data=await api('/demo_scenarios.json');$('scenarios').replaceChildren();
 for(const scenario of data.scenarios){const h=document.createElement('h4');h.textContent=scenario.title;const note=document.createElement('p');note.innerHTML='';const small=document.createElement('small');small.textContent=scenario.note;note.append(small);
  const table=document.createElement('table'),head=document.createElement('tr');for(const t of ['入力（同じ種類の振動候補）','PROPOSED（履歴に基づく）','理由','BASELINE B'])
  {const th=document.createElement('th');th.textContent=t;head.append(th)}table.append(head);
  for(const step of scenario.steps){const tr=document.createElement('tr');for(const text of [step.input,V.actionText(step.proposed),V.reasonTexts(step.reasons.filter(r=>r!=='PASSIVE_CANDIDATE_NEW'&&r!=='MODEL_UNVERIFIED')).join('・'),V.actionText(step.baseline_b)])
   {const td=document.createElement('td');td.textContent=text;tr.append(td)}table.append(tr)}
  const wrap=document.createElement('div');wrap.className='tablewrap';wrap.append(table);$('scenarios').append(h,note,wrap)}
}catch(err){$('scenarios').textContent='比較デモを読み込めません: '+err.message}}

install_graph();
install_run();
install_evidence();
install_collection();
loadScenarios();
(async()=>{try{const d=await api('/api/devices');$('devices').textContent=`WaveForms SDK ${d.sdk_version} / ${d.devices.map(x=>x.name).join(', ')||'AD3未接続'}`;for(const item of d.devices.filter(x=>x.device_type===10)){const o=document.createElement('option');o.value=item.index;o.textContent=item.name+' #'+item.index;$('device').append(o)}for(const p of d.ports){const o=document.createElement('option');o.value=p;o.textContent=p;$('port').append(o)}}catch(err){$('devices').textContent=err.message}loadCollection();await poll();setInterval(poll,500)})();
install_status();
