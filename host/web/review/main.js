// Entry point: event wiring and start-up. Install order = the original inline order.
// Split from review.html on 2026-11-14 without behaviour changes.
import {lightbox} from '../common/lightbox.js';
import {$, api, err, typingTarget} from '../common/api.js';
import {S, video} from './state.js';
import {drawDetail, drawOverview} from './plot.js';
import {loadRuns, openRun, renderRunInfo, requestFrame, scrollToDetail, setCursor, setOffset, showOffsetInfo} from './view.js';
import {layoutPlayer, startPlay, stopPlay, useClock} from './player.js';
import {mark, startRange} from './labels_range.js';
import {hoverEdge, startEdge} from './labels_edge.js';
import {startShift} from './labels_shift.js';
import {graphMenu} from './labels_delete.js';
import {install as install_context} from './context.js';
import {install as install_labels_range} from './labels_range.js';
import {install as install_labels_edge} from './labels_edge.js';
import {install as install_labels_shift} from './labels_shift.js';
import {install as install_labels_delete} from './labels_delete.js';
import {install as install_status} from './status.js';

let swallowSpaceUp=false;

install_context();
install_labels_range();
install_labels_edge();
install_labels_shift();
install_labels_delete();
install_status();
for(const b of document.querySelectorAll('.vside button[data-d]'))b.addEventListener('click',()=>setOffset(S.offset+(+b.dataset.d)));
$('offset').addEventListener('change',e=>setOffset(+e.target.value));
{const b=$('blind'),apply=()=>{document.body.classList.toggle('blind',S.blind);b.checked=S.blind};apply();
 b.addEventListener('change',()=>{S.blind=b.checked;try{localStorage.setItem('bll.review.blind',S.blind?'1':'0')}catch(_){}apply();drawOverview();drawDetail();renderRunInfo();loadRuns(S.run||'')})}
$('showVideo').addEventListener('change',()=>{$('videoPanel').hidden=!video()||!$('showVideo').checked;requestFrame()});
$('frame').addEventListener('click',()=>{const src=$('frame').getAttribute('src');if(!src)return;lightbox([{src:src.replace(/&w=\d+/,'&w=1280'),caption:$('frameInfo').textContent}],0)});
$('frame').addEventListener('error',()=>{$('frameInfo').textContent+='（フレームを読めません）'});
$('saveOffset').addEventListener('click',async()=>{try{const r=await api('/api/review/annotate',{blind:S.blind,run:S.run,kind:'VIDEO_OFFSET',offset_s:S.offset,note:$('note').value});
 S.ov.video.offset_s=r.video_offset.offset_s;S.ov.video.offset_source='SAVED_REVIEW';showOffsetInfo();$('saveStatus').textContent=`動画のずれ ${r.video_offset.offset_s} 秒を保存しました`}catch(e){err('ずれを保存できません: '+e.message)}});
for(const id of ['ovBand','ovLow','ovScore','ovSpec','ovShots','dCh2','dCh1','dSpec']){$(id).addEventListener('mousedown',startRange);$(id).addEventListener('mousedown',startEdge);$(id).addEventListener('mousedown',startShift);$(id).addEventListener('mousemove',hoverEdge);$(id).addEventListener('contextmenu',graphMenu);$(id).addEventListener('click',e=>{const c=e.currentTarget;if(S.suppressClick||e.ctrlKey||e.metaKey||!c._t||!S.ov)return;const r=c.getBoundingClientRect();setCursor(c._t(e.clientX-r.left))})}
for(const id of ['ovBand','ovLow','ovScore','ovSpec','ovShots'])$(id).addEventListener('dblclick',e=>{const c=e.currentTarget;if(!c._t||!S.ov)return;const r=c.getBoundingClientRect();setCursor(c._t(e.clientX-r.left));scrollToDetail()});
$('play').addEventListener('click',()=>S.play?stopPlay():startPlay());
$('rate').addEventListener('change',()=>{if(!S.play)return;$('player').playbackRate=+$('rate').value;if(S.play.mode==='clock')useClock(S.cursor)});
$('player').addEventListener('error',()=>{if(!$('player').getAttribute('src'))return;S.playerFailed=true;if(S.play){err('動画を再生できません（グラフだけ進めます）');useClock(S.cursor)}});
for(const b of document.querySelectorAll('.labels button[data-kind]'))b.addEventListener('click',()=>mark(b.dataset.kind));
$('run').addEventListener('change',e=>openRun(e.target.value));$('reload').addEventListener('click',()=>loadRuns());
$('cursor').addEventListener('change',e=>setCursor(+e.target.value||0));$('span').addEventListener('change',()=>setCursor(S.cursor));$('yscale').addEventListener('change',drawDetail);
document.addEventListener('keyup',e=>{if(swallowSpaceUp&&(e.key===' '||e.code==='Space')){e.preventDefault();swallowSpaceUp=false}},true);
document.addEventListener('keydown',e=>{if(!S.ov||typingTarget(document.activeElement)||e.ctrlKey||e.metaKey||e.altKey)return;
 if(e.key===' '||e.code==='Space'||e.key==='k'||e.key==='K'){e.preventDefault();if(e.repeat)return;
  if(e.key===' '||e.code==='Space'){swallowSpaceUp=true;if(document.activeElement&&document.activeElement!==document.body)document.activeElement.blur()}
  S.play?stopPlay():startPlay();return}
 if(e.key==='ArrowLeft'||e.key==='ArrowRight'){e.preventDefault();setCursor(S.cursor+(e.key==='ArrowLeft'?-1:1)*(e.shiftKey?1:.05))}});
addEventListener('resize',()=>{drawOverview();drawDetail();if(S.play&&S.play.mode==='video')layoutPlayer()});
// Open the newest episode by default (the list is newest first); ?run=ad3-... opens that one,
// and choosing a run keeps it in the address so a reload stays on it.
(async()=>{await loadRuns();const q=new URLSearchParams(location.search),sel=$('run'),want=q.get('run'),at=parseFloat(q.get('t')),values=[...sel.options].map(o=>o.value).filter(Boolean);
 const pick=values.includes(want)?want:values[0];if(pick&&!S.run){sel.value=pick;await openRun(pick);
  // /review?run=…&t=… (from the episode view): put the cursor there and show the detail graph
  if(pick===want&&isFinite(at)&&S.ov){setCursor(at);scrollToDetail()}}})();
