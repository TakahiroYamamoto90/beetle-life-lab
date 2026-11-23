// 取得画面: live waveform/RMS/score graphs and the operator marks.
// Split from ad3_bridge/index.html on 2026-11-14 without behaviour changes.
import {$, api} from './api.js';
import {poll, snapshot} from './status.js';

export function line(canvas,points,end,span,manual,threshold){const ratio=devicePixelRatio||1,w=canvas.clientWidth,h=210;canvas.width=w*ratio;canvas.height=h*ratio;const c=canvas.getContext('2d');c.scale(ratio,ratio);
const left=65,right=w-15,top=16,bottom=h-30,start=Math.max(0,end-span),visible=points.filter(p=>p[0]>=start&&p[0]<=end);let lo=0,hi=1;
if(manual){lo=-manual;hi=manual}else if(visible.length){lo=Math.min(0,...visible.map(p=>p[1]));hi=Math.max(...visible.map(p=>p[2]));const pad=Math.max((hi-lo)*.1,1e-6);lo-=pad;hi+=pad}if(threshold)hi=Math.max(hi,threshold*1.1);
const x=t=>left+(t-start)/span*(right-left),y=v=>bottom-(v-lo)/(hi-lo)*(bottom-top);c.strokeStyle='#dce5df';c.fillStyle='#40584a';c.font='11px system-ui';
for(let i=0;i<=4;i++){const v=lo+(hi-lo)*i/4;c.beginPath();c.moveTo(left,y(v));c.lineTo(right,y(v));c.stroke();c.fillText(v.toPrecision(3),3,y(v)+3);const t=start+span*i/4;c.fillText(t.toFixed(1)+'s',x(t)-10,h-8)}
c.save();c.beginPath();c.rect(left,top,right-left,bottom-top);c.clip();
for(const r of digIntervals(end)){if(r[1]<start||r[0]>end)continue;c.fillStyle='rgba(240,200,40,.22)';c.fillRect(x(Math.max(r[0],start)),top,x(Math.min(r[1],end))-x(Math.max(r[0],start)),bottom-top)}
for(const a of snapshot.annotations||[]){if(a.t_s<start||a.t_s>end)continue;const noise=a.kind==='NOISE';c.strokeStyle=noise?'#7a3fa0':'#a07800';c.lineWidth=2;c.beginPath();c.moveTo(x(a.t_s),top);c.lineTo(x(a.t_s),bottom);c.stroke();c.lineWidth=1;c.fillStyle=c.strokeStyle;c.fillText(noise?'物音':a.kind==='DIG_START'?'掘削開始':'掘削終了',x(a.t_s)+3,top+10)}
if(threshold){c.setLineDash([5,4]);c.strokeStyle='#a34e27';c.beginPath();c.moveTo(left,y(threshold));c.lineTo(right,y(threshold));c.stroke();c.setLineDash([])}
c.strokeStyle='#267454';c.beginPath();if(visible.every(p=>p[1]===p[2])){visible.forEach((p,i)=>{if(i)c.lineTo(x(p[0]),y(p[1]));else c.moveTo(x(p[0]),y(p[1]))})}else{for(const p of visible){c.moveTo(x(p[0]),y(p[1]));c.lineTo(x(p[0]),y(p[2]))}}c.stroke();c.restore();}
export function digIntervals(end){const out=[];let open=null;for(const a of snapshot.annotations||[]){if(a.kind==='DIG_START'&&open===null)open=a.t_s;else if(a.kind==='DIG_END'&&open!==null){out.push([open,a.t_s]);open=null}}if(open!==null)out.push([open,end]);return out}
export async function mark(kind){try{const r=await api('/api/annotate',{kind});$('markStatus').textContent=`記録: ${r.t_s.toFixed(1)} 秒 ${kind==='DIG_START'?'掘削開始':kind==='DIG_END'?'掘削終了':'物音'}`;await poll()}catch(err){$('markStatus').textContent='記録できません: '+err.message}}
export function draw(){const s=snapshot,span=Number($('seconds').value),end=s.elapsed_s||0;line($('wave'),s.points.map(p=>[p[0],p[1]*1000,p[2]*1000]),end,span,$('scale').value==='auto'?null:Number($('scale').value));
line($('rms'),s.features.map(p=>[p.t_center_s,p.rms_V*1000,p.rms_V*1000]),end,span,null);
// observation trigger (2026-11-23): [t, forest_p, ratio, active, masked]; null = not computable yet / arm
{const tw=s.trigger_windows||[];if($('forestP'))line($('forestP'),tw.filter(w=>w[1]!==null).map(w=>[w[0],w[1],w[1]]),end,span,null,s.trigger_threshold||null);
 if($('lowRatio'))line($('lowRatio'),tw.filter(w=>w[2]!==null).map(w=>[w[0],w[2],w[2]]),end,span,null,s.trigger_ratio_min||3);}
line($('score'),s.features.filter(p=>p.score!==null).map(p=>[p.t_center_s,p.score,p.score]),end,span,null,4);}

// Top-level listeners of this part; main.js calls install_graph() once, in the original order.
export function install_graph(){
$('markDigStart').onclick=()=>mark('DIG_START');
$('markDigEnd').onclick=()=>mark('DIG_END');
$('markNoise').onclick=()=>mark('NOISE');
}
