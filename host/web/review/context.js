// Candidate context table (CH2/CH1/sound rise ratios, reference only).
// Split from review.html on 2026-11-14 without behaviour changes.
import {$, api} from '../common/api.js';
import {S} from './state.js';
import {setCursor} from './view.js';

// 外乱らしさ（参考, host/ad3_bridge/disturbance.py）: provisional thresholds from 13 runs on 2026-11-18.
const DZ_REASON={NO_LOW_BAND_RISE_AND_NOT_SHARP:'低域が上がらず鋭さも小さい',CH1_HIGH_BAND_EXTREME:'参照センサーの高域が極端',BASELINE_TOO_SHORT:'直前の基準が短い'};
export function dzText(z){if(!z)return'—';const n=`低域×${z.ch2_low_peak_x??'—'}・鋭さ${z.ch2_crest??'—'}・CH1高域×${z.ch1_high_peak_x??'—'}`;
 if(z.disturbance_like==null)return`判定なし（${z.reasons.map(r=>DZ_REASON[r]||r).join('、')}）`;
 return(z.disturbance_like?`外乱らしい（${z.reasons.map(r=>DZ_REASON[r]||r).join('、')}）`:'外乱らしくない')+'\n'+n}

export function ctxMessage(text){const body=$('ctxRows'),r=document.createElement('tr'),d=document.createElement('td');d.colSpan=9;d.textContent=text;r.append(d);body.replaceChildren(r)}
export function loadContext(run){S.ctx=null;ctxMessage('計算中…');
 api('/api/review/candidates?run='+run).then(c=>{if(S.run!==run)return;S.ctx=c;const body=$('ctxRows');body.replaceChildren();const fx=v=>v==null?'—':'×'+v;
  if(!c.candidates.length){ctxMessage('候補なし');return}
  for(const x of c.candidates){const r=document.createElement('tr');r.style.cursor='pointer';r.onclick=()=>setCursor(x.t_start_s);
   const why=x.xiao?x.xiao.reason_codes.filter(k=>k!=='LIGHT_UNKNOWN'&&k!=='MODEL_UNVERIFIED'):[];
   const xi=x.xiao?x.xiao.action+(why.length?' ('+why.join(', ')+')':''):'未送信';
   for(const t of [x.candidate_seq,`${x.t_start_s.toFixed(2)}–${x.t_end_s.toFixed(2)}`,x.peak_score==null?'—':x.peak_score.toFixed(1),fx(x.ch2_x),fx(x.ch1_x),x.ch1_over_ch2==null?'—':x.ch1_over_ch2,dzText(x.disturbance),c.sound.available?fx(x.sound_x):'音声なし',xi]){const d=document.createElement('td');d.textContent=t;d.style.whiteSpace='pre-line';r.append(d)}
   if(x.disturbance&&x.disturbance.disturbance_like)r.classList.add('dz');body.append(r)}})
 .catch(e=>{if(S.run===run)ctxMessage('計算できません: '+e.message)})}

// Top-level listeners of this part; main.js calls install() once, in the original order.
export function install(){
$('ctxCsv').onclick=()=>{const c=S.ctx;if(!c)return;const q=v=>v==null?'':'"'+String(v).replace(/"/g,'""')+'"';
 const lines=['candidate_seq,t_start_s,t_end_s,peak_score,ch2_x,ch1_x,ch1_over_ch2,ch2_low_peak_x,ch2_crest,ch1_high_peak_x,disturbance_like,disturbance_reasons,disturbance_source,sound_x,xiao_action,xiao_reasons'];
 for(const x of c.candidates){const z=x.disturbance||{};lines.push([x.candidate_seq,x.t_start_s,x.t_end_s,x.peak_score,x.ch2_x,x.ch1_x,x.ch1_over_ch2,z.ch2_low_peak_x,z.ch2_crest,z.ch1_high_peak_x,z.disturbance_like,(z.reasons||[]).join(' '),z.source,x.sound_x,x.xiao&&x.xiao.action,x.xiao&&x.xiao.reason_codes.join(' ')].map(q).join(','))}
 const a=document.createElement('a');a.href=URL.createObjectURL(new Blob(['\ufeff'+lines.join('\r\n')+'\r\n'],{type:'text/csv'}));a.download=`${c.run}-candidate_context.csv`;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)};
}
