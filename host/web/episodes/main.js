// 観察エピソード: entry point (run list, open a run, render).
// Split from ad3_bridge/episodes.html on 2026-11-14 without behaviour changes.
import {card} from './card.js';
import {closePlayer} from './player.js';
import {installTrash, renderDeleteScope, renderTrash} from './trash.js';
import {$, S, clock, el, getJSON, localTime} from './util.js';
import {statusPrefix} from '../common/review_status.js';

// ---- page ----
function render(){const d=S.data,list=$('episodes');closePlayer();list.replaceChildren();
 const s=d.summary;$('summary').hidden=$('honest').hidden=false;
 $('headline').textContent=!s.candidates?'この記録の間、気になる揺れはありませんでした（静か）':
  `気になる揺れが ${s.candidates} 回ありました。幼虫かどうかは、わかりません。`+(s.answered?`（${s.answered} 回は答え合わせ済み）`:'');
 $('facts').replaceChildren();for(const [n,label] of [[s.candidates,'気になる揺れ'],[s.captures,'撮った写真'],[s.waits,'撮らずに待った'],[s.answered,'答え合わせ済み'],[d.low_activity?.spans??'—','低周波の活動らしさ（参考）']]){const f=el('div','fact');f.append(el('b',null,String(n)),el('span',null,label));$('facts').append(f)}
 $('runInfo').textContent=`${localTime(d.started_at_pc)} から ${clock(d.duration_s)}`+(d.case_id?` ・ ケース ${d.case_id}`:'')+(d.note?` ・ ${d.note}`:'')+(d.video.available?' ・ 動画あり':'');
 if(!d.episodes.length){list.append(el('div','empty','このあいだ、振動センサーは「気になる揺れ」を見つけませんでした。'+(d.low_activity?.spans?`ただし、低周波の活動らしさ（PC参考・暫定）は ${d.low_activity.spans} 回・計 ${Math.round(d.low_activity.active_s)} 秒ありました（振り返り画面の青緑の帯）。`:'')));return}
 d.episodes.forEach((ep,i)=>list.append(card(ep,i)))}
async function openRun(run,seq){S.run=run;renderDeleteScope(run);$('error').textContent='';$('toReview').href=`/review?run=${encodeURIComponent(run)}`;try{history.replaceState(null,'',`?run=${run}`+(seq!=null?`&seq=${seq}`:''))}catch(_){}
 try{S.data=await getJSON(`/api/review/episodes?run=${encodeURIComponent(run)}`);render();focusEpisode(seq)}catch(e){$('error').textContent='読み込めません: '+e.message}}
// ?seq=N (from the demo page): the card of that candidate is scrolled into view and outlined.
function focusEpisode(seq){if(seq==null)return;const c=document.querySelector(`article.ep[data-seq="${CSS.escape(String(seq))}"]`);if(!c)return;
 for(const x of document.querySelectorAll('article.ep.focus'))x.classList.remove('focus');c.classList.add('focus');c.scrollIntoView({block:'start'})}

// want: the run to open (null = the newest; the ?run= of the address on first load).
async function loadRuns(want,seq){try{const {runs}=await getJSON('/api/review/runs');const sel=$('run');sel.replaceChildren();
 for(const r of runs){const o=el('option',null,`${statusPrefix(r.review_status)}${localTime(r.started_at_pc)} ・ ${clock(r.duration_s)} ・ 揺れ ${r.candidates} 回`+(r.case_id?` ・ ${r.case_id}`:'')+(r.note?` ・ ${r.note}`:''));o.value=r.run;sel.append(o)}
 if(!runs.length){S.run=null;S.data=null;closePlayer();renderDeleteScope(null);$('summary').hidden=$('honest').hidden=true;$('runInfo').textContent='';
  $('episodes').replaceChildren(el('div','empty','まだ記録がありません。取得画面で計測してください。'));return}
 const pick=runs.some(r=>r.run===want)?want:runs[0].run;sel.value=pick;sel.onchange=()=>openRun(sel.value);await openRun(pick,pick===want?seq:null)}
 catch(e){$('error').textContent='記録の一覧を読めません: '+e.message}}
installTrash(loadRuns);
{const q=new URLSearchParams(location.search);loadRuns(q.get('run'),q.get('seq'))}renderTrash();
