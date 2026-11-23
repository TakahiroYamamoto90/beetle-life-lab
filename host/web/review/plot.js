// Canvas drawing: overview graphs, detail waveforms, spectrograms, bands and label lines.
// Split from review.html on 2026-11-14 without behaviour changes.
import {$} from '../common/api.js';
import {KIND, PAD, POSE_COLOR, S, video} from './state.js';
import {drawShots, renderShotPanel, shots} from './shots.js';

export function decodeSpec(s){const raw=atob(s.data),a=new Uint8Array(raw.length);for(let i=0;i<raw.length;i++)a[i]=raw.charCodeAt(i);return{...s,bytes:a}}
export const CMAP=[[0,0,4],[40,11,84],[101,21,110],[159,42,99],[212,72,66],[245,125,21],[250,193,39],[252,255,164]];
export function cmap(v){const x=v/255*(CMAP.length-1),i=Math.min(CMAP.length-2,Math.floor(x)),f=x-i,a=CMAP[i],b=CMAP[i+1];return[a[0]+(b[0]-a[0])*f,a[1]+(b[1]-a[1])*f,a[2]+(b[2]-a[2])*f]}
export function specNote(text){for(const n of document.querySelectorAll('.specNote'))n.textContent=text?'　'+text:''}
export function specPlot(c,spec,x0,x1){const [g,w,h]=canvasCtx(c);g.clearRect(0,0,w,h);const X=t=>PAD.l+(t-x0)/(x1-x0)*(w-PAD.l-PAD.r);
 g.fillStyle='#e9eeea';g.fillRect(PAD.l,PAD.t,w-PAD.l-PAD.r,h-PAD.t-PAD.b);
 if(spec){const off=document.createElement('canvas');off.width=spec.cols;off.height=spec.rows;const og=off.getContext('2d'),img=og.createImageData(spec.cols,spec.rows);
  for(let i=0;i<spec.bytes.length;i++){const [r,gg,b]=cmap(spec.bytes[i]);img.data[4*i]=r;img.data[4*i+1]=gg;img.data[4*i+2]=b;img.data[4*i+3]=255}og.putImageData(img,0,0);
  const ta=spec.t_video[0]-S.offset,tb=spec.t_video[spec.cols-1]-S.offset;g.save();g.beginPath();g.rect(PAD.l,PAD.t,w-PAD.l-PAD.r,h-PAD.t-PAD.b);g.clip();
  g.drawImage(off,X(ta),PAD.t,Math.max(1,X(tb)-X(ta)),h-PAD.t-PAD.b);g.restore()}
 const fb=spec?spec.f_bottom_hz:40,ft=spec?spec.f_top_hz:16000,Y=f=>PAD.t+(1-Math.log(f/fb)/Math.log(ft/fb))*(h-PAD.t-PAD.b);
 axes(g,w,h,x0,x1,[100,1000,10000].filter(f=>f>fb&&f<ft),f=>f>=1000?f/1000+'k':String(f),Y);
 const ov=S.ov;g.save();g.globalAlpha=.9;decorations(g,X,x0,x1,h);g.restore();c._t=x=>x0+(x-PAD.l)/(w-PAD.l-PAD.r)*(x1-x0)}
export function canvasCtx(c){const r=c.getBoundingClientRect(),dpr=devicePixelRatio||1;c.width=Math.round(r.width*dpr);c.height=Math.round(r.height*dpr);const g=c.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);return[g,r.width,r.height]}
export function intervals(marks,startKind,endKind,end){const out=[];let open=null;for(const m of [...marks].sort((x,y)=>x.t_s-y.t_s)){if(m.kind===startKind)open=m.t_s;else if(m.kind===endKind&&open!==null){out.push([open,m.t_s]);open=null}}if(open!==null)out.push([open,end]);return out}
export function decorations(g,X,x0,x1,h){const ov=S.ov,end=ov.duration_s,band=(a,b,color)=>{const p=Math.max(x0,a),q=Math.min(x1,b);if(q>p){g.fillStyle=color;g.fillRect(X(p),PAD.t,Math.max(1,X(q)-X(p)),h-PAD.t-PAD.b)}};
 const v=video();if(v){const p=Math.max(x0,-S.offset),q=Math.min(x1,v.duration_s-S.offset);if(q>p){g.fillStyle='#2a9d8f';g.fillRect(X(p),PAD.t,X(q)-X(p),3)}}
 if(!S.blind){for(const c of ov.candidates)band(c.t_start_s,c.t_end_s,'rgba(232,140,40,.28)');
 // low-frequency activity-likeness (PC reference, provisional; lowfreq_activity.py): teal strip near the top
 for(const s of (ov.low_activity&&ov.low_activity.activity_like_spans)||[]){const p=Math.max(x0,s.t0_s),q=Math.min(x1,s.t1_s);if(q>p){g.fillStyle='rgba(14,124,134,.85)';g.fillRect(X(p),PAD.t+4,Math.max(1,X(q)-X(p)),5)}}
 // the same by the small forest (lowfreq_forest.py, PC reference, provisional): violet strip just below
 for(const s of (ov.forest_activity&&ov.forest_activity.activity_like_spans)||[]){const p=Math.max(x0,s.t0_s),q=Math.min(x1,s.t1_s);if(q>p){g.fillStyle='rgba(122,72,196,.9)';g.fillRect(X(p),PAD.t+11,Math.max(1,X(q)-X(p)),5)}}
 // the same forest as computed on the XIAO during the capture (xiao_lowfreq.jsonl, 0.15.0+): navy strip below
 for(const s of (ov.xiao_forest_activity&&ov.xiao_forest_activity.activity_like_spans)||[]){const p=Math.max(x0,s.t0_s),q=Math.min(x1,s.t1_s);if(q>p){g.fillStyle='rgba(28,52,140,.95)';g.fillRect(X(p),PAD.t+18,Math.max(1,X(q)-X(p)),5)}}}
 for(const [a,b] of intervals(ov.review_marks,'LARVA_START','LARVA_END',end))band(a,b,'rgba(70,160,90,.25)');
 for(const [a,b] of intervals(ov.review_marks,'DIG_START','DIG_END',end))band(a,b,'rgba(240,200,40,.35)');
 for(const [a,b] of intervals(ov.review_marks,'NOISE_START','NOISE_END',end))band(a,b,'rgba(138,76,171,.18)');
 for(const [a,b] of intervals(ov.live_marks,'DIG_START','DIG_END',end))band(a,b,'rgba(240,200,40,.15)');
 // arm moving (machine record, not a label): grey band with a dark strip at the bottom
 for(const s of ov.actuation||[]){const p=Math.max(x0,s.t0_s),q=Math.min(x1,Math.max(s.t1_s,s.t0_s+.05));if(q<=p)continue;
  g.fillStyle='rgba(80,80,80,.13)';g.fillRect(X(p),PAD.t,Math.max(1,X(q)-X(p)),h-PAD.t-PAD.b);g.fillStyle='rgba(60,60,60,.7)';g.fillRect(X(p),h-PAD.b-4,Math.max(1,X(q)-X(p)),4);
  if(x1-x0<40&&X(q)-X(p)>40){g.fillStyle='#444';g.font='10px system-ui';g.fillText(s.kind==='PARK'?'アーム戻り':'アーム→'+(s.pose||'')+(s.kind==='MOVE_AND_CAPTURE'?'（撮影含む・推定）':s.time_basis==='XIAO_DECISION_TO_CAPTURE_START'?'（推定）':''),X(p)+2,h-PAD.b-7)}}
 const line=(t,color,dash,text)=>{if(t<x0||t>x1)return;g.save();g.strokeStyle=color;g.setLineDash(dash);g.lineWidth=1.5;g.beginPath();g.moveTo(X(t),PAD.t);g.lineTo(X(t),h-PAD.b);g.stroke();g.restore();if(text){g.fillStyle=color;g.font='11px system-ui';g.fillText(text,X(t)+3,PAD.t+11)}};
 const color={LARVA_START:'#2f8a45',LARVA_END:'#2f8a45',DIG_START:'#a88512',DIG_END:'#a88512',NOISE:'#8a4cab',NOISE_START:'#8a4cab',NOISE_END:'#8a4cab',SYNC_TAP:'#3f6fb5'};
 for(const m of ov.live_marks)line(m.t_s,color[m.kind]||'#777',[5,4],'');
 for(const m of ov.review_marks)line(m.t_s,color[m.kind]||'#777',[],(x1-x0)<40?KIND[m.kind]:'');
 for(const x of shots())if(x.t_s>=x0&&x.t_s<=x1){g.fillStyle=POSE_COLOR[x.pose]||'#555';g.beginPath();g.moveTo(X(x.t_s)-4,PAD.t);g.lineTo(X(x.t_s)+4,PAD.t);g.lineTo(X(x.t_s),PAD.t+7);g.fill()}
 line(S.cursor,'#111',[2,2],'')}
export function axes(g,w,h,x0,x1,ticksY,fmtY,Y){g.fillStyle='#52675b';g.font='11px system-ui';g.strokeStyle='#e3ebe5';g.lineWidth=1;
 for(const v of ticksY){const y=Y(v);g.beginPath();g.moveTo(PAD.l,y);g.lineTo(w-PAD.r,y);g.stroke();g.fillText(fmtY(v),4,y+4)}
 const span=x1-x0,step=[.1,.2,.5,1,2,5,10,20,30,60,120,300,600].find(s=>span/s<=10)||600;
 for(let t=Math.ceil(x0/step)*step;t<=x1;t+=step){const x=PAD.l+(t-x0)/(x1-x0)*(w-PAD.l-PAD.r);g.fillText((step<1?t.toFixed(1):t.toFixed(0))+'s',x-8,h-4)}}
export function logPlot(c,series,opts={}){const [g,w,h]=canvasCtx(c),x0=0,x1=S.ov.duration_s;g.clearRect(0,0,w,h);
 let lo=Infinity,hi=-Infinity;for(const s of series)for(const v of s.v)if(v>0){lo=Math.min(lo,v);hi=Math.max(hi,v)}
 if(!isFinite(lo)){g.fillStyle='#52675b';g.fillText('データなし',PAD.l,40);return}
 if(opts.threshold){lo=Math.min(lo,opts.threshold/2);hi=Math.max(hi,opts.threshold*2)}
 if(opts.floor)lo=Math.max(lo,opts.floor);if(opts.max)hi=Math.min(hi,opts.max)
 const L0=Math.floor(Math.log10(lo)),L1=Math.ceil(Math.log10(hi)),X=t=>PAD.l+(t-x0)/(x1-x0)*(w-PAD.l-PAD.r),Y=v=>h-PAD.b-(Math.log10(v)-L0)/(L1-L0||1)*(h-PAD.t-PAD.b);
 const ticks=[];for(let k=L0;k<=L1;k++)ticks.push(10**k);axes(g,w,h,x0,x1,ticks,v=>v>=1?String(v):v.toExponential(0),Y);decorations(g,X,x0,x1,h);
 if(opts.threshold){g.save();g.setLineDash([6,4]);g.strokeStyle='#666';g.beginPath();g.moveTo(PAD.l,Y(opts.threshold));g.lineTo(w-PAD.r,Y(opts.threshold));g.stroke();g.restore()}
 for(const s of series){g.strokeStyle=s.color;g.lineWidth=1;g.beginPath();let pen=false;for(let i=0;i<s.t.length;i++){const v=s.v[i];if(!(v>0)){pen=false;continue}const x=X(s.t[i]),y=Y(Math.max(v,10**L0));pen?g.lineTo(x,y):g.moveTo(x,y);pen=true}g.stroke()}
 c._x=t=>X(t);c._t=x=>x0+(x-PAD.l)/(w-PAD.l-PAD.r)*(x1-x0)}
export function drawOverview(){if(!S.ov)return;const o=S.ov;
 logPlot($('ovBand'),[{t:o.band.t,v:o.band.ch1,color:'#2e6fb7'},{t:o.band.t,v:o.band.ch2,color:'#c0392b'}]);
 logPlot($('ovLow'),[{t:o.low.t,v:o.low.ch1,color:'#2e6fb7'},{t:o.low.t,v:o.low.ch2,color:'#c0392b'}]);
 if(!S.blind)logPlot($('ovScore'),[{t:o.band.t,v:o.band.score,color:'#20342d'}],{threshold:4,floor:.1});
 // observation trigger (2026-11-23): forest probability and 1-40 Hz ratio per 250 ms window
 const ts=o.trigger_series;if(!S.blind&&ts&&$('ovForestP')){logPlot($('ovForestP'),[{t:ts.t,v:ts.forest_p,color:'#7a48c4'}],{threshold:ts.threshold,floor:.01,max:1});
  logPlot($('ovRatio'),[{t:ts.t,v:ts.ratio,color:'#0e7c86'}],{threshold:ts.ratio_min,floor:.1});
  const n=$('triggerSource');if(n)n.textContent=ts.source==='LIVE_RECORD'?'　取得中に記録した値（この値で MCU に送りました）':'　この記録は B2 で観察しました。値は振り返りで計算したもの'}
 if(video())specPlot($('ovSpec'),S.spec,0,o.duration_s);drawShots();renderShotPanel()}
export function wavePlot(c,mins,maxs,color){const [g,w,h]=canvasCtx(c);g.clearRect(0,0,w,h);const W=S.wave;if(!W){return}
 const x0=W.t0,x1=W.t1,fixed=$('yscale').value;let m=fixed==='auto'?Math.max(...maxs.map(Math.abs),...mins.map(Math.abs))*1.1||1:+fixed;
 const X=t=>PAD.l+(t-x0)/(x1-x0)*(w-PAD.l-PAD.r),Y=v=>PAD.t+(1-(Math.max(-m,Math.min(m,v))+m)/(2*m))*(h-PAD.t-PAD.b);
 axes(g,w,h,x0,x1,[-m,0,m],v=>(Math.abs(v)<1?v.toFixed(2):v.toFixed(1)),Y);decorations(g,X,x0,x1,h);
 g.strokeStyle=color;g.lineWidth=1;g.beginPath();for(let i=0;i<W.t.length;i++){const x=X(W.t[i]);g.moveTo(x,Y(maxs[i]));g.lineTo(x,Y(mins[i])+.5)}g.stroke();
 c._t=x=>x0+(x-PAD.l)/(w-PAD.l-PAD.r)*(x1-x0)}
export function drawDetail(){if(!S.wave)return;wavePlot($('dCh2'),S.wave.ch2_min,S.wave.ch2_max,'#c0392b');wavePlot($('dCh1'),S.wave.ch1_min,S.wave.ch1_max,'#2e6fb7');
 if(video())specPlot($('dSpec'),S.dspec,S.wave.t0,S.wave.t1)}
export function pxOf(c,t){const a=c._t(PAD.l),b=c._t(PAD.l+100);return PAD.l+(t-a)/(b-a)*100}
