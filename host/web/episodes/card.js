// 観察エピソード: one card per vibration candidate (felt / decided / acted / checked).
// Split from ad3_bridge/episodes.html on 2026-11-14 without behaviour changes.
import {lightbox} from '../common/lightbox.js';
import {orient} from '../common/orient.js';
import {togglePlayer} from './player.js';
import {$, ANSWER, POSE, S, STAGES, VIEW, clock, compareText, deliveryText, el, meter} from './util.js';

// ---- one episode card ----
export function card(ep,index){const box=el('article','ep');box.dataset.index=index;box.dataset.seq=ep.candidate_seq;
 const h=el('h2');h.append(el('span','t',clock(ep.t_start_s)),'気になる揺れ',el('span','chip unknown','幼虫かどうか：わからない'));box.append(h);
 const loop=el('ol','loop');const li={};for(const k of STAGES){li[k]=el('li');li[k].dataset.stage=k;loop.append(li[k])}
 const steps=ep.steps||[],first=steps[0];
 // felt
 li.felt.append(el('h3',null,'① 感じた'),el('div','main','振動センサーが反応'),meter('in','ケースの中',ep.ch2_x),meter('out','ケースの外',ep.ch1_x),el('small',null,compareText(ep)));
 if(ep.arm){const t=ep.arm.relation==='DURING'?(ep.arm.kind==='MOVE_AND_CAPTURE'?'アームの移動〜撮影の間の揺れです':'アームが動いている最中の揺れです'):(ep.arm.kind==='MOVE_AND_CAPTURE'?`アームの移動〜撮影が終わって ${ep.arm.seconds} 秒後の揺れです`:`アームが止まって ${ep.arm.seconds} 秒後の揺れです`);
  const n=el('div','armnote',t+'（自分の動きの音の可能性）');li.felt.append(n)}
 // low-frequency activity-likeness around this episode (PC reference, provisional threshold; not a detection)
 if(ep.low_activity){const L=ep.low_activity;li.felt.append(el('div','lownote'+(L.length?' on':''),L.length?`低周波の活動らしさ（参考）：この場面で ${L.length} 回（最大 ${Math.max(...L.map(x=>x.peak_x)).toFixed(1)} 倍）`:'低周波の活動らしさ（参考）：この場面では反応なし'))}
 // decided
 li.decided.append(el('h3',null,'② 判断した'));
 if(first){li.decided.append(el('div','main',first.action==='WAIT'?'今は撮らない':'撮って確かめる'),el('small',null,'観察係（決まったルールで判断）：'+VIEW.whyText(first)))}
 else li.decided.append(el('div','main','判断なし'),el('small',null,deliveryText(ep.delivery)));
 // acted
 li.acted.append(el('h3',null,'③ 動いた'));const caps=steps.filter(s=>s.action&&s.action.startsWith('CAPTURE_'));
 if(caps.length){li.acted.append(el('div','main',`アームを回して ${caps.length} 方向から撮影`));const p=el('div','poses');
  for(const s of caps)p.append(el('span','pose'+(s.capture==='SUCCESS'?'':' fail'),(POSE[s.pose]||s.pose||'?')+(s.capture==='SUCCESS'?'':'（失敗）')));li.acted.append(p)}
 else li.acted.append(el('div','main',first&&first.action==='WAIT'?'動かずに待った':'動いていない'));
 // checked
 li.checked.append(el('h3',null,'④ 確かめた'));const shots=caps.filter(s=>s.image_url);
 if(shots.length){li.checked.append(el('div','main',`写真 ${shots.length} 枚`));const th=el('div','thumbs');
  const items=shots.map(s=>({src:s.image_url,pose:s.pose,caption:`${POSE[s.pose]||s.pose} ・ ${clock(s.t_s)}\n写真に幼虫が写っているかは、まだ人も機械も判定していません`}));
  shots.forEach((s,i)=>{const im=el('img');im.src=s.image_url;orient(im,s.pose);im.alt=POSE[s.pose]||'';im.loading='lazy';im.dataset.t=s.t_s;im.onclick=()=>lightbox(items,i);th.append(im)});
  li.checked.append(th,el('small',null,'写っているかは未判定'))}
 else li.checked.append(el('div','main','写真なし'),el('small',null,caps.length?'撮影できなかった（いない、という意味ではありません）':'今回は確かめていません'));
 box.append(loop);
 // answer + actions
 const ans=el('div','answer');ans.append(el('span',null,'あなたの答え：'));
 if(ep.answers.length)for(const a of ep.answers)ans.append(el('span','chip '+a,ANSWER[a]||a));else ans.append(el('span','muted','まだ答え合わせしていません'));
 const act=el('span','actions');
 if(S.data.video.available&&ep.clip_video_s){const b=el('button',null,'▶ 動画で見る（消音）');b.type='button';b.onclick=()=>togglePlayer(box,ep,b);act.append(b)}
 const a=el('a',null,'根拠を見る・答え合わせ →');a.href=`/review?run=${encodeURIComponent(S.run)}&t=${ep.t_start_s.toFixed(3)}`;act.append(a);ans.append(act);box.append(ans);
 return box}
