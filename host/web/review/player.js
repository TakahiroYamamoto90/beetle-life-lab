// Playback: the <video> clock (or the wall clock) drives the cursor. Nothing is saved.
// Split from review.html on 2026-11-14 without behaviour changes.
import {$, api, err} from '../common/api.js';
import {S, video} from './state.js';
import {drawDetail, drawOverview} from './plot.js';
import {requestWave, setCursor, windowRange} from './view.js';
import {renderShotPanel} from './shots.js';

// Playback: the <video> clock drives the cursor (AD3 time = video time - offset); without a
// playable video, the wall clock does. The view only follows; nothing is saved.
export function showPlayer(on){$('frame').hidden=on;$('playerWrap').hidden=!on;if(on)layoutPlayer()}
export function layoutPlayer(){const v=video(),wrap=$('playerWrap'),p=$('player');if(!v||!v.width)return;const b=v.content_box||[0,0,1,1];
 const W=Math.min(480,$('videoPanel').clientWidth||480),bw=(b[2]-b[0])*v.width,bh=(b[3]-b[1])*v.height,k=W/bw;
 wrap.style.width=W+'px';wrap.style.height=Math.round(bh*k)+'px';p.style.width=v.width*k+'px';p.style.height=v.height*k+'px';p.style.left=-b[0]*v.width*k+'px';p.style.top=-b[1]*v.height*k+'px'}
export function videoUsable(t){const v=video();return v&&$('showVideo').checked&&!S.playerFailed&&t+S.offset>=0&&t+S.offset<v.duration_s-.05}
export function useVideo(t){const p=$('player');if(p.dataset.run!==S.run){p.src=`/api/review/video?run=${S.run}`;p.dataset.run=S.run}
 p.playbackRate=+$('rate').value;p.currentTime=Math.max(0,t+S.offset);S.play.mode='video';showPlayer(true);
 p.play().catch(e=>{if(!S.play)return;
  if(e.name==='NotAllowedError'&&!p.muted){p.muted=true;err('ブラウザが音声付きの自動再生を止めたため、消音で再生しています（もう一度押すと音も出ます）');p.play().catch(()=>{});return}
  if(e.name==='AbortError')return;S.playerFailed=true;err('動画を再生できません（グラフだけ進めます）: '+e.message);useClock(S.cursor)})}
export function useClock(t){S.play.mode='clock';S.play.c0=t;S.play.w0=performance.now();$('player').pause();showPlayer(false)}
export function resync(){if(!S.play)return;videoUsable(S.cursor)?useVideo(S.cursor):useClock(S.cursor)}
export function startPlay(){if(!S.ov||S.play)return;err('');$('player').muted=false;if(S.cursor>=S.ov.duration_s-.05)setCursor(0);S.play={mode:'clock'};S.playWin=null;
 $('play').textContent='❚❚ 停止';resync();S.play.raf=requestAnimationFrame(tick)}
export function stopPlay(quiet){if(!S.play)return;cancelAnimationFrame(S.play.raf);S.play=null;S.playWin=null;$('player').pause();showPlayer(false);$('play').textContent='▶ 再生';
 if(!quiet&&S.ov)setCursor(S.cursor)}
export function tick(){if(!S.play)return;const p=$('player'),v=video();let t;
 if(S.play.mode==='video'){if(p.ended||p.currentTime>=v.duration_s-.05){if(S.cursor<S.ov.duration_s-.05){useClock(S.cursor)}else{stopPlay();return}}t=S.play.mode==='video'&&!p.seeking?p.currentTime-S.offset:S.cursor}
 else{t=S.play.c0+(performance.now()-S.play.w0)/1000*(+$('rate').value);if(videoUsable(t)&&!(S.playerFailed))useVideo(t)}
 if(t>=S.ov.duration_s){S.cursor=S.ov.duration_s;stopPlay();return}
 S.cursor=Math.max(0,Math.round(t*1000)/1000);$('cursor').value=S.cursor.toFixed(3);const now=performance.now(),before=S.playWin;
 const [a,b]=windowRange();if(!before||before[0]!==a)requestWave();
 if(now-S.drawnDetail>66){S.drawnDetail=now;drawDetail();renderShotPanel()}if(now-S.drawnOv>250){S.drawnOv=now;drawOverview()}
 if(video())$('frameInfo').textContent=`再生中 AD3 ${S.cursor.toFixed(2)}秒 → 動画 ${(S.cursor+S.offset).toFixed(2)}秒${S.play.mode==='video'?'':'（動画の範囲外：グラフのみ）'}`;
 S.play.raf=requestAnimationFrame(tick)}
