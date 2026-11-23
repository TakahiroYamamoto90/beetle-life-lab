// 観察ノート: XIAO ADC (piezo) capture view and its live stream.
// Split from vision_capture/index.html on 2026-11-14 without behaviour changes.
import {$, api} from './api.js';
import {sessionInfo} from './cases.js';
import {busy, buttons, selected, setBusy, state, take} from './control.js';

export const DISPLAY_MAX_POINTS=80000;
export let displayWindowUs=3000000,piezoYMin=0,piezoYMax=4095;
export let piezoPoints=[];
export function applyPiezoScale(){
 const seconds=Number($('piezoWindow').value),low=Number($('piezoYMin').value),high=Number($('piezoYMax').value);
 if(![0.1,0.25,0.5,1,3,5,10].includes(seconds)||!$('piezoYMin').value.trim()||!$('piezoYMax').value.trim()||!Number.isInteger(low)||!Number.isInteger(high)||low<0||high>4095||low>=high){$('piezoScaleNote').textContent='Y軸は0〜4095の整数で、下限 < 上限となるよう指定してください。';return}
 displayWindowUs=seconds*1e6;piezoYMin=low;piezoYMax=high;
 $('piezoScaleNote').textContent=`表示: 直近${seconds}秒 / ADC ${low}〜${high}。範囲外の値は非表示です。CSV保存には影響しません。`;drawPiezo();
}
export function drawPiezo(){
 const canvas=$('piezoCanvas'),rect=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1;
 if(rect.width<=64||rect.height<=40)return;
 canvas.width=Math.round(rect.width*dpr);canvas.height=Math.round(rect.height*dpr);
 const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);
 const w=rect.width,h=rect.height,left=46,right=w-12,top=12,bottom=h-32,plotW=right-left,plotH=bottom-top;
 const yOf=v=>bottom-(v-piezoYMin)/(piezoYMax-piezoYMin)*plotH;
 ctx.clearRect(0,0,w,h);ctx.lineWidth=1;ctx.font='11px system-ui';
 for(let i=0;i<=4;i++){
  const v=piezoYMax-(piezoYMax-piezoYMin)*i/4,y=yOf(v);
  ctx.strokeStyle='#47615a';ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(right,y);ctx.stroke();
  ctx.fillStyle='#c1d0c5';ctx.textAlign='right';ctx.fillText(String(Number(v.toFixed(2))),left-6,y+4);
 }
 const latest=piezoPoints.length?piezoPoints[piezoPoints.length-1][0]:0;
 // Anchor the selected time window at the latest received sample.
 const start=latest-displayWindowUs;
 for(let i=0;i<=3;i++){
  const x=left+plotW*i/3;
  ctx.strokeStyle='#304c43';ctx.beginPath();ctx.moveTo(x,top);ctx.lineTo(x,bottom);ctx.stroke();
  ctx.fillStyle='#c1d0c5';ctx.textAlign=i===0?'left':i===3?'right':'center';ctx.fillText(`${Number(((i/3-1)*displayWindowUs/1e6).toFixed(3))} s`,x,h-14);
 }
 const buckets=new Map();
 for(const [t,v] of piezoPoints){
  if(t<start||t>latest)continue;
  const x=Math.min(Math.ceil(plotW)-1,Math.floor((t-start)/displayWindowUs*plotW));
  const b=buckets.get(x);
  if(b){b[0]=Math.min(b[0],v);b[1]=Math.max(b[1],v)}else buckets.set(x,[v,v]);
 }
 ctx.strokeStyle='#a9d88c';ctx.beginPath();
 for(const [x,[min,max]] of buckets){
  if(max<piezoYMin||min>piezoYMax)continue;
  const visibleMin=Math.max(min,piezoYMin),visibleMax=Math.min(max,piezoYMax),px=left+x;
  if(visibleMin===visibleMax){ctx.moveTo(px,yOf(visibleMin));ctx.lineTo(Math.min(right,px+1),yOf(visibleMin))}
  else{ctx.moveTo(px,yOf(visibleMax));ctx.lineTo(px,yOf(visibleMin))}
 }
 ctx.stroke();ctx.textAlign='right';ctx.fillStyle='#c1d0c5';ctx.fillText('時間（最新サンプル = 0 s）',right,h-1);
}
export function piezoStateText(value){return({idle:'待機中',starting:'MCUへ接続中',capturing:'取得中',stopping:'停止処理中',complete:'取得完了',partial:'一部欠測',error:'エラー'})[value]||value||'待機中'}
export function applyPiezoUpdate(update){state.piezo={...(state.piezo||{}),...update};if(update.points?.length){piezoPoints.push(...update.points);if(piezoPoints.length>DISPLAY_MAX_POINTS)piezoPoints=piezoPoints.slice(-DISPLAY_MAX_POINTS);drawPiezo()}$('piezoStateTag').textContent=piezoStateText(update.state);$('piezoStateTag').classList.toggle('error',update.state==='error'||update.state==='partial');const n=update.samples_received||0,elapsed=update.elapsed_us==null?(update.duration_s_host||0):update.elapsed_us/1e6,stats=update.minimum==null?'':` · min ${update.minimum} / max ${update.maximum} / RMS ${update.rms??'—'}`;let status=`${piezoStateText(update.state)} · ${n.toLocaleString()} samples · ${elapsed.toFixed(2)} s · raw 0–4095${stats}`;if(update.error)status+=` · ${update.error}`;$('piezoStatus').textContent=status;if(update.csv_url||update.metadata_url){$('piezoFiles').replaceChildren();for(const [url,label] of [[update.csv_url,'CSVを保存'],[update.metadata_url,'計測メタデータ']])if(url){const a=document.createElement('a');a.href=url;a.download='';a.textContent=label;$('piezoFiles').append(a)}}buttons()}
export async function startPiezo(){if(busy||state.busy)return;const raw=$('piezoDuration').value.trim();let duration=null;if(raw){duration=Number(raw);if(!Number.isInteger(duration)||duration<1||duration>4294967295){$('piezoStatus').textContent='時間は1〜4294967295秒の整数で指定してください。';return}}setBusy(true);buttons();piezoPoints=[];drawPiezo();$('piezoFiles').replaceChildren();try{const update=await api('/api/piezo/start',{port:$('portSelect').value,condition:$('piezoCondition').value,duration_s:duration,session_id:$('sessionSelect').value||null});applyPiezoUpdate(update);$('piezoStatus').textContent=duration===null?'無期限取得を開始しました。停止するまで記録します。':'取得を開始しました。'}catch(e){$('piezoStatus').textContent=`開始できませんでした: ${e.message}`;state.busy=false}finally{setBusy(false);buttons()}}
export async function stopPiezo(){try{applyPiezoUpdate(await api('/api/piezo/stop',{}))}catch(e){$('piezoStatus').textContent=`停止できませんでした: ${e.message}`}}

// Top-level listeners of this part; main.js calls install_piezo() once, in the original order.
export function install_piezo(){
const piezoEvents=new EventSource('/api/piezo/stream');
piezoEvents.addEventListener('state',event=>applyPiezoUpdate(JSON.parse(event.data)));
piezoEvents.addEventListener('samples',event=>applyPiezoUpdate(JSON.parse(event.data)));
piezoEvents.onerror=()=>{$('piezoStatus').textContent='信号表示サーバーへ再接続中です。取得データはPC側で継続保存されます。'};
$('piezoScaleApply').onclick=applyPiezoScale;
$('piezoWindow').onchange=applyPiezoScale;
$('piezoScaleReset').onclick=()=>{$('piezoWindow').value='3';$('piezoYMin').value='0';$('piezoYMax').value='4095';applyPiezoScale()};
$('piezoStartBtn').onclick=startPiezo;
$('piezoStopBtn').onclick=stopPiezo;
window.addEventListener('resize',drawPiezo);
$('piezoPanel').addEventListener('toggle',()=>{if($('piezoPanel').open)drawPiezo()});
$('previewBtn').onclick=()=>take('preview');
$('saveBtn').onclick=()=>take('save');
$('classifySaveBtn').onclick=()=>take('classify_save');
$('sessionSelect').onchange=sessionInfo;
$('portSelect').onchange=buttons;
}
