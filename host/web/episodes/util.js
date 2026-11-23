// 観察エピソード: shared state, wording helpers and small DOM helpers.
// Split from ad3_bridge/episodes.html on 2026-11-14 without behaviour changes.

export const $=id=>document.getElementById(id);
export const VIEW=globalThis.BllObservationView;
export const POSE={FRONT:'前から',TOP:'上から',BACK:'後ろから',FIXED:'固定カメラ'};
export const ANSWER={LARVA:'幼虫活動',DIG:'人工刺激（掘削）',NOISE:'外乱（声・物音）'};
export const STAGES=['felt','decided','acted','checked'];
export const S={run:null,data:null,player:null};
// ---- helpers ----
export function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
export function clock(t){if(t==null||!isFinite(t))return '—';const m=Math.floor(t/60),s=t-m*60;return `${m}:${s.toFixed(1).padStart(4,'0')}`}
export function localTime(iso){try{return new Date(iso).toLocaleString('ja-JP',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})}catch(_){return iso||''}}
export async function getJSON(url){const r=await fetch(url);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.statusText);return j}
// A rise ratio on a log scale: 1x (no change) is empty, 30x and above is full.
export function meter(kind,label,x){const row=el('div','meter '+kind);row.append(el('span',null,label));const bar=el('div','bar'),fill=el('i');
 const w=x==null?0:Math.max(0,Math.min(1,Math.log(Math.max(x,1))/Math.log(30)));fill.style.width=(w*100).toFixed(1)+'%';bar.append(fill);row.append(bar);
 row.append(el('span','x',x==null?'—':`×${x<10?x.toFixed(1):Math.round(x)}`));return row}
// A plain comparison of two measured rises; no threshold, no verdict.
export function compareText(ep){const a=ep.ch2_x,b=ep.ch1_x;if(a==null||b==null)return 'ケースの外の揺れは比べられません';
 if(b>a)return 'ケースの外の揺れのほうが大きく上がりました';if(a>b)return 'ケースの中の揺れのほうが大きく上がりました';return '中と外が同じくらい上がりました'}
export function deliveryText(status){return {not_sent_capture_failed:'撮影の失敗が続いていたため、観察係には送りませんでした',NOT_FORWARDED:'この記録では観察係（MCU）に送っていません',
 sending:'観察係の返事がありません',failed:'観察係に届きませんでした'}[status]||`観察係の返事：${status}`}
