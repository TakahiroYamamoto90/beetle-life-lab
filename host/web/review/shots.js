// XIAO arm shots on the time axis and the shot panel/list.
// Split from review.html on 2026-11-14 without behaviour changes.
import {lightbox} from '../common/lightbox.js';
import {drawOriented, orient} from '../common/orient.js';
import {$} from '../common/api.js';
import {BASIS, PAD, POSE_COLOR, POSE_SHORT, S} from './state.js';
import {axes, canvasCtx, decorations} from './plot.js';
import {setCursor} from './view.js';

export function shots(){return S.ov&&S.ov.shots?S.ov.shots.filter(x=>x.t_s!=null):[]}
export function shotImage(x){if(!x.image_url)return null;S.shotImg=S.shotImg||{};let im=S.shotImg[x.action_id];
 if(!im){im=new Image();im.onload=()=>drawShots();im.src=x.image_url;S.shotImg[x.action_id]=im}return im.complete&&im.naturalWidth?im:null}
export function drawShots(){const c=$('ovShots');if(!S.ov||$('shotsBlock').hidden)return;const [g,w,h]=canvasCtx(c),x0=0,x1=S.ov.duration_s,X=t=>PAD.l+(t-x0)/(x1-x0)*(w-PAD.l-PAD.r);
 g.clearRect(0,0,w,h);axes(g,w,h,x0,x1,[],v=>'',v=>0);decorations(g,X,x0,x1,h);const list=shots(),size=Math.min(h-PAD.t-PAD.b-4,64);
 let gap=Infinity;for(let i=1;i<list.length;i++)gap=Math.min(gap,X(list[i].t_s)-X(list[i-1].t_s));const thumbs=gap>size*.8;
 for(const x of list){const cx=X(x.t_s),col=POSE_COLOR[x.pose]||'#555',im=thumbs?shotImage(x):null,top=PAD.t+2;
  if(im){drawOriented(g,im,cx-size/2,top,size,size,x.pose)}else{g.fillStyle=col;g.globalAlpha=.25;g.fillRect(cx-size/2,top,size,size);g.globalAlpha=1}
  g.save();g.strokeStyle=col;g.lineWidth=3;if(x.time_basis==='ESTIMATED_EVEN_SPACING')g.setLineDash([5,3]);g.strokeRect(cx-size/2,top,size,size);g.restore();
  g.fillStyle=col;g.font='bold 12px system-ui';g.fillText(POSE_SHORT[x.pose]||'?',cx-size/2+3,top+13)}
 if(S.cursor>=x0&&S.cursor<=x1){g.save();g.strokeStyle='#111';g.lineWidth=2;g.setLineDash([3,2]);g.beginPath();g.moveTo(X(S.cursor),PAD.t);g.lineTo(X(S.cursor),h-PAD.b);g.stroke();g.restore()}
 c._t=x=>x0+(x-PAD.l)/(w-PAD.l-PAD.r)*(x1-x0)}
export function openShot(x){const list=shots().filter(s=>s.image_url);lightbox(list.map(s=>({src:s.image_url,pose:s.pose,caption:`${s.pose}・${s.t_s.toFixed(2)}秒・候補#${s.candidate_seq}・時刻: ${BASIS[s.time_basis]||s.time_basis}`+(s.model_reference&&s.model_reference.class_name?`\n視覚モデル（未検証・参考）: ${s.model_reference.class_name} ${s.model_reference.score}`:''),action:{label:'この撮影時刻へ移動',run:()=>setCursor(s.t_s)}})),Math.max(0,list.findIndex(s=>s.action_id===x.action_id)))}
export function latestShots(t){const out={};for(const x of shots())if(x.t_s<=t+1e-6&&x.image_url)out[x.pose]=x;return out}
export function shotCard(x,label,now){const d=document.createElement('div');d.className='shot'+(x?'':' none')+(x&&x.time_basis==='ESTIMATED_EVEN_SPACING'?' est':'');
 const im=document.createElement('img');im.alt=label;if(x&&x.image_url){im.src=x.image_url;orient(im,x.pose);im.className='zoomable';im.title='クリックで拡大';im.onclick=e=>{e.stopPropagation();openShot(x)}}d.append(im);const cap=document.createElement('div');
 const head=document.createElement('b');head.textContent=label;head.style.color=POSE_COLOR[label]||'#333';cap.append(head);
 if(x){const m=x.model_reference&&x.model_reference.class_name?` 視覚モデル（参考）:${x.model_reference.class_name==='VISIBLE_LARVA'?'見える':'見えない'} ${x.model_reference.score}`:'';
  cap.append(` ${x.t_s.toFixed(1)}秒`+(now!=null?`（${(now-x.t_s).toFixed(1)}秒前）`:'')+`\n#${x.candidate_seq}・${BASIS[x.time_basis]||x.time_basis}${m}`);cap.style.whiteSpace='pre-line';
  d.title='画像を押すと拡大、文字を押すとこの撮影時刻へ移動';d.onclick=()=>setCursor(x.t_s)}else cap.append(' まだ撮影なし');d.append(cap);return d}
export function renderShotPanel(){const p=$('shotPanel');if(!shots().length){p.hidden=true;return}p.hidden=false;const last=latestShots(S.cursor),poses=['FRONT','TOP','BACK'];
 if(shots().some(x=>x.pose==='FIXED'))poses.push('FIXED');const key=poses.map(k=>last[k]?last[k].action_id:'-').join('|')+'@'+S.cursor.toFixed(1);if(p._key===key)return;p._key=key;
 const title=document.createElement('div');title.style.width='100%';title.textContent='能動観測：カーソル時刻までの最新の視点（押すとその時刻へ）';p.replaceChildren(title,...poses.map(k=>shotCard(last[k],k,S.cursor)))}
export function renderShotList(){const list=shots(),wrap=$('shotListWrap'),box=$('shotList');wrap.hidden=!list.length;box.replaceChildren();const groups=new Map();
 for(const x of list){if(!groups.has(x.candidate_seq))groups.set(x.candidate_seq,[]);groups.get(x.candidate_seq).push(x)}
 for(const [seq,items] of groups){const row=document.createElement('div');row.className='episode';const h=document.createElement('h4');
  h.textContent=`候補 #${seq} の観測エピソード（${items[0].t_s.toFixed(1)}〜${items[items.length-1].t_s.toFixed(1)}秒、${items.length}枚）`;row.append(h,...items.map(x=>shotCard(x,x.pose,null)));box.append(row)}}
