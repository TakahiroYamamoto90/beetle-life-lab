// Opening a run, the cursor, the detail window, video offset and frame requests.
// Split from review.html on 2026-11-14 without behaviour changes.
import {$, api, err, localTime} from '../common/api.js';
import {S, audioOk, video} from './state.js';
import {decodeSpec, drawDetail, drawOverview, specNote} from './plot.js';
import {loadContext} from './context.js';
import {resync, stopPlay} from './player.js';
import {renderShotList, shots} from './shots.js';
import {renderTables} from './tables.js';
import {renderStatus} from './status.js';
import {statusPrefix} from '../common/review_status.js';

export async function loadRuns(keep){err('');try{const {runs}=await api('/api/review/runs'),sel=$('run'),prev=keep??sel.value;sel.replaceChildren(new Option('記録を選択',''));
 for(const r of runs){const marks=r.review_marks?`・振り返り印 ${r.review_marks}`:'';sel.add(new Option(`${statusPrefix(r.review_status)}${localTime(r.started_at_pc)}（${Math.round(r.duration_s)}秒${S.blind?'':'・候補 '+r.candidates}${marks}${r.video?'・動画あり':''}）${r.note?' '+r.note:''} ${r.run.slice(4,12)}${r.origin==='LIVE'?'':' ['+r.origin+']'}`,r.run))}
 sel.value=runs.some(r=>r.run===prev)?prev:''}catch(e){err('一覧を読めません: '+e.message)}}
// Run summary line; the candidate count is left out in blind labelling (2026-11-21).
export function renderRunInfo(){const ov=S.ov;if(!ov)return;
  $('runInfo').textContent=`${localTime(ov.started_at_pc)}開始・${ov.duration_s.toFixed(1)}秒・${ov.origin}${S.blind?'':'・振動イベント候補 '+ov.candidates.length+'件'}・取得中の印 ${ov.live_marks.length}件${ov.note?'・メモ: '+ov.note:''}。幼虫活動の判定: UNKNOWN${ov.video?'\n動画: '+(ov.video.file||ov.video.reason||ov.video.status||'なし'):''}`;$('runInfo').style.whiteSpace='pre-line';}
export function setupVideo(){const v=video();S.spec=null;S.dspec=null;S.dspecKey='';
 $('videoPanel').hidden=!v||!$('showVideo').checked;$('showVideoWrap').hidden=!v;for(const b of document.querySelectorAll('.specBlock'))b.hidden=!v;
 if(!v)return;S.offset=v.offset_s;$('offset').value=S.offset.toFixed(3);showOffsetInfo();
 if(!audioOk()){specNote(v.audio&&v.audio.reason||'音声を表示できません');return}
 specNote('読み込み中…');const run=S.run;api(`/api/review/audio?run=${run}&cols=1500`).then(s=>{if(S.run!==run)return;S.spec=decodeSpec(s);specNote('');drawOverview()}).catch(e=>specNote(e.message))}
export function showOffsetInfo(){const v=video();if(!v)return;const src={SAVED_REVIEW:'保存したずれ',ESTIMATED_PC_RECEIPT:`PC受信時刻からの推定（${v.offset_estimate_s}秒、目安）`,UNKNOWN_ZERO:'不明（0として表示）'}[v.offset_source]||v.offset_source;
 $('offsetInfo').textContent=`現在の基準: ${src}${Math.abs(S.offset-v.offset_s)>1e-6?'（未保存の変更あり）':''}／動画 ${v.duration_s.toFixed(1)}秒・${v.fps}fps・${v.width}×${v.height}${v.in_run_folder?'':'（OBSの保存先から読込）'}`}
export function setOffset(x){if(!video()||!isFinite(x))return;S.offset=Math.round(x*1000)/1000;$('offset').value=S.offset.toFixed(3);S.dspecKey='';showOffsetInfo();drawOverview();requestWave();requestFrame()}
export function requestFrame(){const v=video();if(!v||$('videoPanel').hidden||(S.play&&S.play.mode==='video'))return;clearTimeout(S.frameTimer);S.frameTimer=setTimeout(()=>{const vt=S.cursor+S.offset;
 if(vt<0||vt>v.duration_s){$('frame').removeAttribute('src');$('frameInfo').textContent=`AD3 ${S.cursor.toFixed(3)}秒 → 動画 ${vt.toFixed(3)}秒（録画の範囲外）`;return}
 $('frame').src=`/api/review/frame?run=${S.run}&v=${vt.toFixed(3)}&w=640`;$('frameInfo').textContent=`AD3 ${S.cursor.toFixed(3)}秒 → 動画 ${vt.toFixed(3)}秒（フレーム ${Math.round(vt*v.fps)}）`},90)}
export async function openRun(run){stopPlay(true);try{history.replaceState(null,'',run?`?run=${run}`:location.pathname)}catch(_){}S.playerFailed=false;S.run=run||null;S.ov=null;S.wave=null;S.waveKey='';S.spec=null;S.dspec=null;$('viewer').hidden=$('listSection').hidden=!run;$('runInfo').textContent='';renderStatus();if(!run)return;
 $('runInfo').textContent='読み込み中…（長い記録は低周波の計算に数秒かかります）';
 try{const ov=await api('/api/review/overview?run='+run);if(S.run!==run)return;S.ov=ov;renderStatus();{const sh=ov.shadow_triggers,n=$('shadowNote');if(n){const L={LOWFREQ_RATIO_3:'1–40 Hz 比≥3',FOREST_PC:'フォレスト（PC）',FOREST_XIAO:'フォレスト（MCU）'};n.textContent=sh?'並走・参考（観察のきっかけは今も B2 だけです）：B2 候補 '+sh.b2.count+' 件（'+sh.b2.per_min+'/分）'+Object.entries(sh.sources).map(([k,v])=>'｜'+L[k]+'：'+(v.status==='OK'?v.count+' 回（'+v.per_min+'/分、B2 と別の時刻 '+v.without_b2+'、アーム直後 '+v.after_arm+'）':'なし')).join('')+(sh.forest_in_training?'　※フォレストはこの記録で学習済み':''):''}}{const f=ov.forest_activity||{},n=$('forestNote');if(n)n.textContent=f.status==='NO_MODEL'?'　※フォレストのモデルがありません':f.in_training?'　※この記録はフォレストの学習に使ったため、紫の帯は答えを見た結果です（in-sample）':'';const x=ov.xiao_forest_activity;if(n&&x)n.textContent+=x.status==='UNSUPPORTED'?'　※MCU のファームウェアが古く、MCU では計算していません':x.status==='ENDED'?`　MCU：${x.windows} 窓を計算・区間 ${x.activity_like_spans.length}（アーム動作で除外 ${x.arm_windows} 窓）`:`　MCU：途中で停止（${x.status}${x.errors.length?'：'+x.errors[0]:''}）`}
  renderRunInfo();
  S.shotImg={};$('shotsBlock').hidden=!shots().length;{const panel=$('shotPanel'),v=ov.video&&ov.video.available;if(v)document.querySelector('#videoPanel .vside').append(panel);else $('videoPanel').after(panel)}$('shotPanel')._key='';renderShotList();setupVideo();$('cursor').max=ov.duration_s;setCursor(Math.min(S.cursor,ov.duration_s));renderTables();loadContext(run)}catch(e){err('記録を開けません: '+e.message);$('runInfo').textContent=''}}
export function scrollToDetail(){const head=$('dCh2').previousElementSibling,stick=document.querySelector('.stick');
 scrollTo({top:head.getBoundingClientRect().top+scrollY-(stick?stick.offsetHeight:0)-8,behavior:'smooth'})}
export function setCursor(t){if(!S.ov)return;S.cursor=Math.max(0,Math.min(S.ov.duration_s,Math.round(t*1000)/1000));$('cursor').value=S.cursor.toFixed(3);if(S.play)resync();drawOverview();requestWave();requestFrame()}
export function windowRange(){const span=+$('span').value,d=S.ov.duration_s;
 if(S.play){let w=S.playWin;if(!w||S.cursor<w[0]||S.cursor>w[1]-span*.1){const a=Math.max(0,Math.min(S.cursor-span*.1,d-span));w=S.playWin=[a,Math.min(d,a+span)]}return w}
 let a=S.cursor-span/2;a=Math.max(0,Math.min(a,d-span));return[Math.max(0,a),Math.min(d,a+span)]}
export function requestWave(){clearTimeout(S.timer);S.timer=setTimeout(async()=>{const [a,b]=windowRange(),key=`${S.run}:${a.toFixed(3)}:${b.toFixed(3)}`;
 if(key!==S.waveKey){try{const w=await api(`/api/review/wave?run=${S.run}&t0=${a}&t1=${b}`);if(key.startsWith(S.run+':')){S.wave=w;S.waveKey=key}}catch(e){err('波形を読めません: '+e.message)}}
 const skey=`${key}:${S.offset}`;if(audioOk()&&skey!==S.dspecKey){const v=video(),v0=Math.max(0,a+S.offset),v1=Math.min(v.duration_s,b+S.offset);
  if(v1>v0){try{const s=await api(`/api/review/audio?run=${S.run}&v0=${v0}&v1=${v1}&cols=600`);S.dspec=decodeSpec(s);S.dspecKey=skey}catch(e){S.dspec=null;specNote(e.message)}}else{S.dspec=null;S.dspecKey=skey}}
 drawDetail()},120)}
