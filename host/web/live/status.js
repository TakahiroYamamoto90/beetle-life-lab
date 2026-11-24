// 取得画面: polling the bridge state, observation decisions and images.
// Split from ad3_bridge/index.html on 2026-11-14 without behaviour changes.
import {lightbox} from '../common/lightbox.js';
import {observationPose, orient} from '../common/orient.js';
import {$, api} from './api.js';
import {V, evidenceButtons, evidenceLoading} from './evidence.js';
import {draw} from './graph.js';
import {mode} from './run.js';

export let snapshot={points:[],features:[]};
export async function poll(){try{const s=await api('/api/state');snapshot=s;$('start').disabled=s.running||s.reading_evidence||evidenceLoading;evidenceButtons(s.running||s.reading_evidence);$('stop').disabled=!s.running;for(const id of ['mode','device','range','duration','wiring','forward','port'])$(id).disabled=s.running;if(!s.running)mode();
const capturing=s.running&&s.state==='capturing',digs=(s.annotations||[]).filter(a=>a.kind!=='NOISE'),digOpen=digs.length>0&&digs[digs.length-1].kind==='DIG_START';
$('markDigStart').disabled=!capturing||digOpen;$('markDigEnd').disabled=!capturing||!digOpen;$('markNoise').disabled=!capturing;
$('origin').textContent=s.origin==='SYNTHETIC'?'MOCK / SYNTHETIC':s.origin||'未開始';$('origin').className='tag'+(s.origin==='SYNTHETIC'?' mock':'');
$('status').textContent=`状態: ${s.state} / 検出器: ${s.detector_state||'未開始'}${s.detector_resets?`（15秒超の候補で作り直し ${s.detector_resets} 回）`:''}\n経過: ${(s.elapsed_s||0).toFixed(2)} 秒 / サンプル: ${s.samples||0}\n候補: ${s.candidate_count||0} / MCU保存ACK: ${s.forwarded||0} / 活動: UNKNOWN${videoText(s.video)}${importText(s.collection_import)}`;
if(s.error)$('error').textContent=s.error;$('downloads').replaceChildren();if(s.run_id&&!s.running){for(const [name,label] of [['manifest.json','条件・品質'],['samples.f64','原波形 f64'],['features.csv','特徴量 CSV'],['candidates.jsonl','候補 JSONL'],...((s.annotations||[]).length?[['annotations.jsonl','操作記録']]:[]),...((s.candidates||[]).some(c=>c.context&&!c.context.error)?[['candidate_context.jsonl','候補のCH1/CH2比（参考）']]:[]),...(s.forwarding?[['delivery.jsonl','転送記録']]:[])]){const a=document.createElement('a');a.href=`/files/ad3-${s.run_id}/${name}`;a.textContent=label;a.style.marginRight='16px';$('downloads').append(a)}}
renderObservations(s);
$('events').replaceChildren();for(const event of [...s.candidates].reverse()){const row=document.createElement('tr');const cx=event.context||{},fx=v=>v==null?'—':'×'+v;for(const text of [event.candidate_seq,`${event.t_start_s.toFixed(2)}–${event.t_end_s.toFixed(2)}`,(event.peak_rms_V*1000).toFixed(3)+' mV',event.peak_score.toFixed(2),fx(cx.ch2_x),fx(cx.ch1_x)]){const cell=document.createElement('td');cell.textContent=text;row.append(cell)}$('events').append(row)}draw();}catch(err){$('error').textContent=err.message}}
export function importText(c){if(!c)return'';if(c.status==='IMPORTED')return`\n観察ノート: ${c.imported}枚を未確認として取り込み（ケース ${c.case_id}）`+(c.skipped&&c.skipped.length?` / 対象外 ${c.skipped.length}件`:'');if(c.status==='SKIPPED_NO_CASE')return'\n観察ノート: '+c.reason;return'\n観察ノートへの取り込みに失敗: '+(c.error||c.status)}
export function videoText(v){if(!v||v.status==='OFF')return'';const label={REQUESTED:'録画の準備中',RECORDING:'OBSで録画中',MOVING:'録画ファイルを計測フォルダへ移動中',RECORDED:'OBSの録画を保存',UNAVAILABLE:'録画できません（計測は続行）',STOP_FAILED:'録画の停止に失敗（OBSで手動停止してください）'}[v.status]||v.status;
 return '\n動画: '+label+(v.reason?' — '+v.reason:'')+(v.stop_error?' — '+v.stop_error:'')+(v.move_error?'\n動画の移動に失敗（OBSの保存先に残っています）: '+v.move_error:'')+(v.output_path?'\n動画ファイル: '+v.output_path:'')}
export function renderObservations(s){
 const mode=$('observationMode');
 if(s.forwarding&&s.observation_supported===true){mode.textContent='LIVE：MCUが保存済みの履歴から判断';mode.className='tag'}
 else if(s.forwarding&&s.observation_supported===false){mode.textContent='MCUのFWは観測判断に未対応（0.10.0以降が必要）';mode.className='tag mock'}
 else if(s.origin==='SYNTHETIC'){mode.textContent='MOCK：MCUへは送らないため判断なし';mode.className='tag mock'}
 else{mode.textContent='MCU転送なし（判断なし）';mode.className='tag'}
 const c=V.compareCounts(s);$('baselineCount').textContent=`${c.baselineB} 回`;
 $('proposedCount').textContent=`撮影 ${c.proposedCaptures} 回 / 待機 ${c.proposedWaits} 回`;
 const list=s.observations||[],latest=list[list.length-1];
 if(latest){$('latestWhy').textContent=`なぜこの行動？ — ${V.whyText(latest)}`;$('latestResult').textContent=V.resultText(latest)}
 const om=$('obsModeTag'),o=s.observation_mode,oj={follow:'追従（見えた方向をもう一度）',hold:'追従・その場で待つ（見えた方向でアームを止める）',explore:'探索（しばらく見ていない方向から）'};om.hidden=!o;if(o){om.textContent=o.status==='ERROR'?'観察の方式：MCUに設定できませんでした（'+o.error+'）':'観察の方式：'+(oj[o.effective]||o.effective)+(o.status==='UNSUPPORTED_FIRMWARE'?(o.effective!==o.requested?'（このFWは切り替え未対応のため、選んだ方式では動きません）':'（このFWの固定の方式）'):'');om.className='tag'+(o.status!=='APPLIED'&&o.effective!==o.requested?' mock':'')}
 const mm=$('modelMode'),mtext=V.modelStatusText(s.vision_model);mm.textContent=mtext;mm.hidden=!mtext;mm.className='tag'+(s.vision_model&&s.vision_model.status!=='VERIFIED'?' mock':'');
 const shot=[...list].reverse().find(o=>o.image_url);
 if(shot){$('latestImage').src=shot.image_url;orient($('latestImage'),observationPose(shot));$('latestImage').hidden=false;$('latestImage').title=shot.image_verified?'MCUのハッシュと一致':'ハッシュ不一致または未検証';$('latestModel').textContent=V.modelText(shot);$('latestPose').textContent=`最新の画像：${V.poseText(shot)}`}
 $('observations').replaceChildren();
 for(const o of [...list].reverse()){const tr=document.createElement('tr');
  for(const text of [o.candidate_seq,V.poseText(o),V.whyText(o),V.actionText(o.baseline_b_action),V.resultText(o)]){const td=document.createElement('td');td.textContent=text;tr.append(td)}
  const td=document.createElement('td');if(o.image_url){const img=document.createElement('img');img.src=o.image_url;orient(img,observationPose(o));img.className='obs-thumb zoomable';img.title='クリックで拡大';img.onclick=()=>openObservationImages(o.image_url);img.alt='観測画像';td.append(img);if(!o.image_verified){const w=document.createElement('div');w.textContent='ハッシュ不一致';td.append(w)}}else td.textContent='—';
  tr.append(td);const ai=document.createElement('td');ai.textContent=V.modelText(o);tr.append(ai);$('observations').append(tr)}
}
export function openObservationImages(src){const list=(snapshot&&snapshot.observations||[]).filter(o=>o.image_url);const items=list.map(o=>({src:o.image_url,pose:observationPose(o),caption:`候補#${o.candidate_seq}・${V.poseText(o)}・${V.modelText(o)}`}));lightbox(items.length?items:[{src}],Math.max(0,items.findIndex(x=>x.src===src)))}

// Top-level listeners of this part; main.js calls install_status() once, in the original order.
export function install_status(){
$('latestImage').addEventListener('click',()=>openObservationImages($('latestImage').getAttribute('src')));
}
