// 取得画面: case IDs from 観察ノート for the image import.
// Split from ad3_bridge/index.html on 2026-11-14 without behaviour changes.
import {$, api} from './api.js';

export async function loadCollection(){try{const c=await api('/api/collection/context');const sel=$('caseSelect');sel.length=1;for(const item of c.cases){const o=document.createElement('option');o.value=item.case_id;o.textContent=item.case_id+(item.display_name?' — '+item.display_name:'')+` (${item.samples}枚)`;sel.append(o)}
 if(!$('case').value)$('case').value=c.last_case_id||'';sel.value=[...sel.options].some(o=>o.value===$('case').value)?$('case').value:'';
 $('lightList').replaceChildren(...c.illuminations.map(v=>{const o=document.createElement('option');o.value=v;return o}));if(!$('illumination').value)$('illumination').value=c.last_illumination||'';
 if(c.error)$('collectionHint').textContent='観察ノート: '+c.error}catch(err){$('collectionHint').textContent='観察ノートのケース一覧を読めません: '+err.message}}

// Top-level listeners of this part; main.js calls install_collection() once, in the original order.
export function install_collection(){
$('caseSelect').onchange=()=>{if($('caseSelect').value)$('case').value=$('caseSelect').value};
$('case').oninput=()=>{$('caseSelect').value=[...$('caseSelect').options].some(o=>o.value===$('case').value)?$('case').value:''};
}
