// 観察エピソード: muted video clip with the loop band following along.
// Split from ad3_bridge/episodes.html on 2026-11-14 without behaviour changes.
import {$, S, clock, el} from './util.js';

// ---- video clip with the loop band following along ----
export function stageAt(ep,t){const caps=(ep.steps||[]).filter(s=>s.t_s!=null);
 if(t<ep.t_start_s)return null;if(t<=ep.t_end_s)return 'felt';if(!caps.length)return 'decided';
 if(caps.some(s=>Math.abs(t-s.t_s)<=1.5))return 'checked';if(t<=ep.t_end_s+1)return 'decided';return t<=caps[caps.length-1].t_s?'acted':'checked'}
export function closePlayer(){const p=S.player;if(!p)return;p.video.pause();p.video.removeAttribute('src');p.video.load();p.node.remove();p.button.textContent='▶ 動画で見る（消音）';
 for(const n of p.box.querySelectorAll('.now'))n.classList.remove('now');S.player=null}
export function togglePlayer(box,ep,button){const same=S.player&&S.player.box===box;closePlayer();if(same)return;
 const offset=ep.clip_video_s[0]-ep.clip_s[0],node=el('div','player'),wrap=el('div','vwrap'),v=document.createElement('video');
 v.muted=true;v.playsInline=true;v.controls=true;v.preload='auto';v.src=`/api/review/video?run=${encodeURIComponent(S.run)}`;wrap.append(v);
 const side=el('div','vnote');const now=el('div','clock');side.append(now,el('p',null,''));
 side.lastChild.innerHTML='外部カメラ（OBS）の映像です。<b>音は消してあります</b>（人の声が入るため。コントロールで出せます）。<br>上の ①〜④ の枠が、映像の時刻に合わせて光ります（目安。動画とセンサーの時刻合わせは'+
  (S.data.video.offset_source==='SAVED_REVIEW'?'振り返り画面で確認済み':'PCの受信時刻からの推定で、数秒ずれることがあります')+'）。'+
  ((ep.steps||[]).some(s=>s.time_basis==='ESTIMATED_EVEN_SPACING')?'撮影の時刻は古い記録のため均等割りの推定で、数秒ずれることがあります。':'')+
  '<br>映像は振り返り用で、検出には使っていません。';
 node.append(wrap,side);box.append(node);button.textContent='■ 動画を閉じる';
 const box2=content=>{const c=S.data.video.content_box;if(!c||!v.videoWidth)return;const [x0,y0,x1,y1]=c,bw=x1-x0,bh=y1-y0;
  wrap.style.aspectRatio=`${v.videoWidth*bw} / ${v.videoHeight*bh}`;Object.assign(v.style,{width:(100/bw)+'%',left:(-x0/bw*100)+'%',top:(-y0/bh*100)+'%'})};
 v.addEventListener('loadedmetadata',()=>{box2();v.currentTime=Math.max(0,ep.clip_video_s[0]);v.play().catch(()=>{})});
 v.addEventListener('timeupdate',()=>{const t=v.currentTime-offset;if(v.currentTime>=ep.clip_video_s[1]&&!v.paused){v.pause()}
  const st=stageAt(ep,t);for(const li of box.querySelectorAll('.loop>li'))li.classList.toggle('now',li.dataset.stage===st);
  for(const im of box.querySelectorAll('.thumbs img'))im.classList.toggle('now',st==='checked'&&Math.abs(t-(+im.dataset.t))<=1.5);
  now.textContent=`センサーの時刻 ${clock(t)}（このエピソードは ${clock(ep.clip_s[0])}〜${clock(ep.clip_s[1])}）`});
 v.addEventListener('error',()=>{now.textContent='動画を再生できません（振り返り画面で確認してください）'});
 S.player={box,video:v,node,button}}
