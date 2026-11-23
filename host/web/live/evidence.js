// 取得画面: XIAO Evidence log list (read-only), grouped by boot.
// Split from ad3_bridge/index.html on 2026-11-14 without behaviour changes.
import {$, api} from './api.js';
import {line} from './graph.js';
import {snapshot} from './status.js';

export let evidence=null,evidencePage=0,evidenceLoading=false;
export const evidencePageSize=10;
export const bootOpen=new Map(),bootLimits=new Map();
export function jstText(value){return value.replace('T',' ').replace('+09:00',' JST')}
export const eventLabels={BOOT:'起動',DEVICE_DIAGNOSTIC:'診断',PASSIVE_VIBRATION_CANDIDATE:'振動候補',TIME_SYNC:'時刻同期',POLICY_DECISION:'方策の判断',OBSERVATION_ACTION_RESULT:'行動の結果'};
export const V=BllObservationView;
export function observationDetail(event){const r=event.result||{};
 if(event.event_type==='POLICY_DECISION')return [`判断：${V.actionText(r.action)}`,V.reasonTexts(r.reason_codes).join('\n')];
 if(event.event_type==='OBSERVATION_ACTION_RESULT')return [`${r.status} / 撮影 ${r.capture?.status}`,`${r.capture?.reason||''}\n幼虫：UNKNOWN（判定モデル未検証）`];
 return null}
export function evidenceRow(event){
 const result=event.result||{},tr=document.createElement('tr'),time=evidence.event_times?.[event.event_id],ad3=evidence.ad3_times?.[event.event_id];
 const cells=[`+${(event.monotonic_ms/1000).toFixed(3)} 秒`,
  (eventLabels[event.event_type]||event.event_type)+' / '+event.source+' / '+event.origin,
  time?`${jstText(time.timestamp_jst)}\n推定（${time.basis==='STORED_UTC'?'保存UTC・PC時計基準':'同期点から換算'}）`:'UTC未同期',
  event.event_type==='PASSIVE_VIBRATION_CANDIDATE'?(ad3?`${jstText(ad3.start_jst)}\n– ${jstText(ad3.end_jst)}\n推定（PC開始要求基準）\n`:'')+`${(result.source_start_ms/1000).toFixed(3)}–${(result.source_end_ms/1000).toFixed(3)} 秒\nPC取得開始基準`:'—',
  event.event_type==='PASSIVE_VIBRATION_CANDIDATE'?`${(result.peak_rms_uV/1000).toFixed(3)} mV RMS\nスコア ${(result.peak_score_milli/1000).toFixed(3)}`:'—'];
 const detail=observationDetail(event);if(detail){cells[3]=detail[0];cells[4]=detail[1]}
 for(const text of cells){const td=document.createElement('td');td.textContent=text;tr.append(td)}
 const td=document.createElement('td'),details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');
 summary.textContent='イベントJSON';pre.textContent=JSON.stringify(event,null,2);details.append(summary,pre);td.append(details);
 if(result.source_run_id){const a=document.createElement('a');a.href=`/files/ad3-${result.source_run_id}/manifest.json`;a.textContent='PC側の取得条件';td.append(a)}
 tr.append(td);return tr;
}
export function renderBootRows(group,body,kind){
 const rows=BllEvidenceGroups.visibleRows(group,kind),limit=bootLimits.get(group.key)||100;
 body.replaceChildren();const wrap=document.createElement('div'),table=document.createElement('table'),thead=document.createElement('thead'),head=document.createElement('tr'),tbody=document.createElement('tbody');wrap.className='tablewrap';
 for(const title of ['起動後','種類・由来','MCU受信時刻（JST）','AD3候補区間','ピーク','詳細']){const th=document.createElement('th');th.scope='col';th.textContent=title;head.append(th)}
 thead.append(head);for(const row of rows.slice(0,limit))tbody.append(evidenceRow(row.event));table.append(thead,tbody);wrap.append(table);body.append(wrap);
 if(rows.length>limit){const more=document.createElement('button');more.type='button';more.textContent=`さらに${Math.min(100,rows.length-limit)}件を表示（残り${rows.length-limit}件）`;more.onclick=()=>{bootLimits.set(group.key,limit+100);renderBootRows(group,body,kind)};body.append(more)}
}
export function renderEvidence(){
 const groups=BllEvidenceGroups.groupEvidence(evidence),kind=$('evidenceType').value,page=BllEvidenceGroups.pageGroups(groups,kind,evidencePage,evidencePageSize);
 evidencePage=page.page;$('evidenceGroups').replaceChildren();
 for(const group of page.groups){
  if(!bootOpen.has(group.key))bootOpen.set(group.key,group.current);
  const details=document.createElement('details'),summary=document.createElement('summary'),title=document.createElement('span'),meta=document.createElement('span'),body=document.createElement('div');
  details.className='boot-group'+(group.current?' boot-current':'');details.open=bootOpen.get(group.key);title.className='boot-title';
  title.textContent=group.bootEvent?(group.bootTime?`起動：${jstText(group.bootTime.timestamp_jst)}（BOOT記録・推定）`:'起動日時：未同期'):'起動記録なし';
  if(group.current){const tag=document.createElement('span');tag.className='tag';tag.style.marginLeft='10px';tag.textContent=evidence.saved_export?'書き出し時点の起動':'読出し時点の起動';title.append(tag)}
  const counts=group.counts,matched=BllEvidenceGroups.visibleRows(group,kind).length;
  meta.className='boot-meta';meta.textContent=`${group.deviceId} / 起動ID ${group.bootId}\n${group.clockLabel} / 全${group.rows.length}件：起動 ${counts.BOOT||0}・時刻同期 ${counts.TIME_SYNC||0}・診断 ${counts.DEVICE_DIAGNOSTIC||0}・振動候補 ${counts.PASSIVE_VIBRATION_CANDIDATE||0}`+
   (kind==='all'?'':` / 絞り込み該当 ${matched}件`)+`\n最終記録：起動後 ${(group.lastEvent.monotonic_ms/1000).toFixed(3)} 秒`+(group.lastTime?` / ${jstText(group.lastTime.timestamp_jst)}（推定）`:' / UTC未同期');
  meta.style.whiteSpace='pre-line';summary.append(title,meta);details.append(summary,body);let built=false;
  const build=()=>{if(details.open&&!built){renderBootRows(group,body,kind);built=true}};build();
  details.addEventListener('toggle',()=>{bootOpen.set(group.key,details.open);build()});$('evidenceGroups').append(details);
 }
 $('evidencePrevious').disabled=evidencePage===0;$('evidenceNext').disabled=evidencePage>=page.pages-1;
 $('evidencePage').textContent=page.total?`${page.total} 起動中 ${evidencePage*evidencePageSize+1}–${Math.min((evidencePage+1)*evidencePageSize,page.total)} 起動（新しい順）`:'該当する起動ログなし';
}
// Source of the list: '' = the XIAO over serial, otherwise the name of a saved PC export.
export function savedSelected(){return !!$('evidenceSource').value}
// Buttons that need the serial port wait for the acquisition; a saved export can be read anytime.
export function evidenceButtons(busy){$('readEvidence').disabled=evidenceLoading||(!savedSelected()&&busy);$('syncClock').disabled=evidenceLoading||busy||savedSelected()}
const jst=iso=>iso?new Date(iso).toLocaleString('ja-JP',{timeZone:'Asia/Tokyo'})+' JST':'不明';
function deviceStatusText(){const s=evidence.device_state;
 return `${s.device_id} / 合計 ${s.events} 件：起動 ${s.boot_events}、診断 ${s.diagnostic_events}、振動候補 ${s.passive_candidates||0}、時刻同期 ${s.time_sync_events||0}\n`+
  `保存状態 ${s.storage_reason} / 活動 UNKNOWN / 現在の時計：${s.time_quality==='ESTIMATED'?'PC時刻基準（推定）':'未同期'}\n`+
  `PCでの読出し時刻 ${jst(evidence.received_at_pc)}（一覧は読出し時点のスナップショット）`}
function savedStatusText(){const x=evidence.saved_export,m=evidence.host_mirror,s=evidence.device_state||{};
 const hash=x.hash_verified===true?'一致（MCUが送った行のまま）':x.hash_verified===false?'不一致（ファイルが書き出し後に変わった可能性）':'記録なし（旧形式の書き出し）';
 return `保存済みの書き出し（PCのコピー・読み取り専用。MCUには接続していません）\n`+
  `ファイル ${x.name} / 書き出し時刻 ${jst(evidence.received_at_pc)} / ハッシュ ${hash}\n`+
  `${s.device_id||'機器ID不明'} / 合計 ${m.events} 件：起動 ${m.boot_events}、診断 ${m.diagnostic_events}、振動候補 ${m.passive_candidates}、時刻同期 ${m.time_sync_events}、方策の判断 ${m.decision_events}、行動の結果 ${m.action_result_events}\n`+
  `書き出し時点の保存状態 ${s.storage_reason||'不明'} / 活動 UNKNOWN`+(x.rotated_after?'\nこの書き出しの直後に、MCUのログを空にしました（ローテーション）。ここに写っているのは空にする前の全件です。':'')}
export async function loadExports(){const sel=$('evidenceSource'),keep=sel.value;
 try{const {exports}=await api('/api/xiao/exports');sel.replaceChildren(new Option('MCU本体（シリアルで読み出し）',''));
  for(const x of exports)sel.add(new Option(`保存済み：${x.received_at_pc?jst(x.received_at_pc):x.name}・${x.events??'?'}件${x.rotated?'・この後ローテーション':''}${x.verbatim?'':'・旧形式'}`,x.name));
  sel.value=[...sel.options].some(o=>o.value===keep)?keep:''}
 catch(err){$('evidenceStatus').textContent='保存済みの一覧を読めません: '+err.message}
 evidenceButtons(!!snapshot.running)}
export async function loadEvidence(sync){const saved=!sync&&savedSelected();evidenceLoading=true;evidenceButtons(true);
 $('evidenceStatus').textContent=sync?'PCのUTCで同期しています…':saved?'保存済みの書き出しを読み込んでいます…':'MCUの保存ログを読み出しています…';
 try{evidence=saved?await api('/api/xiao/exports/read?name='+encodeURIComponent($('evidenceSource').value)):await api(sync?'/api/xiao/time-sync':'/api/xiao/evidence',{port:$('port').value});
  evidencePage=0;bootOpen.clear();bootLimits.clear();
  $('evidenceStatus').textContent=saved?savedStatusText():deviceStatusText();$('evidenceStatus').style.whiteSpace='pre-line';$('downloadEvidence').disabled=false;renderEvidence()}
 catch(err){$('evidenceStatus').textContent=err.message+(evidence?'（前回取得した一覧を表示しています）':'')}
 finally{evidenceLoading=false;evidenceButtons(!!snapshot.running)}}

// Top-level listeners of this part; main.js calls install_evidence() once, in the original order.
export function install_evidence(){
$('readEvidence').onclick=()=>loadEvidence(false);
$('refreshExports').onclick=loadExports;
$('evidenceSource').onchange=()=>evidenceButtons(!!snapshot.running);
loadExports();
$('syncClock').onclick=()=>loadEvidence(true);
$('evidenceType').onchange=()=>{evidencePage=0;renderEvidence()};
$('evidencePrevious').onclick=()=>{evidencePage--;renderEvidence()};
$('evidenceNext').onclick=()=>{evidencePage++;renderEvidence()};
$('downloadEvidence').onclick=()=>{if(!evidence)return;const blob=new Blob([evidence.events.map(x=>JSON.stringify(x)).join('\n')+'\n'],{type:'application/x-ndjson'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`xiao-evidence-${Date.now()}.jsonl`;a.click();URL.revokeObjectURL(url)};
}
