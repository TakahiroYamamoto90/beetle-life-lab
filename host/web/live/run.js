// 取得画面: acquisition form (mode, start, stop).
// Split from ad3_bridge/index.html on 2026-11-14 without behaviour changes.
import {$, api} from './api.js';
import {draw} from './graph.js';
import {poll} from './status.js';

export function mode(){const mock=$('mode').value==='mock';if(mock)$('forward').checked=false;$('forward').disabled=mock;$('wiring').disabled=mock;}

// Top-level listeners of this part; main.js calls install_run() once, in the original order.
export function install_run(){
$('mode').onchange=mode;
$('seconds').onchange=draw;
$('scale').onchange=draw;
window.addEventListener('resize',draw);
$('form').onsubmit=async e=>{e.preventDefault();$('error').textContent='';try{await api('/api/start',{mode:$('mode').value,duration_s:Number($('duration').value),device_index:Number($('device').value||0),range_v:Number($('range').value),wiring_confirmed:$('wiring').checked,forward:$('forward').checked,video:$('video').checked,port:$('port').value,case_id:$('case').value.trim(),session_id:$('session').value,note:$('note').value,illumination:$('illumination').value.trim()});await poll()}catch(err){$('error').textContent=err.message}};
$('stop').onclick=async()=>{try{await api('/api/stop',{});}catch(err){$('error').textContent=err.message}};
}
