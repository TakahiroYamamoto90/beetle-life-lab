// DOM lookup, the local request token (from <meta name="bll-token">), JSON API calls, errors.
// Split from review.html on 2026-11-14 without behaviour changes.

export const $=id=>document.getElementById(id);
// Read when a request is made, so the module also loads outside a page (tests).
export const TOKEN=()=>document.querySelector('meta[name="bll-token"]').content;
export function err(text){$('error').textContent=text||''}
// A successful POST is announced as a 'bll-posted' event (e.g. the review status follows saved labels).
export async function api(url,body){const r=await fetch(url,body?{method:'POST',headers:{'Content-Type':'application/json','X-BLL-Token':TOKEN()},body:JSON.stringify(body)}:{});
 const data=await r.json().catch(()=>({error:r.statusText}));if(!r.ok)throw new Error(data.error||r.statusText);
 if(body&&typeof dispatchEvent==='function'&&typeof CustomEvent==='function')dispatchEvent(new CustomEvent('bll-posted',{detail:{url,body,data}}));return data}
export function localTime(iso){if(!iso)return'時刻不明';const d=new Date(iso);return isNaN(d)?iso:d.toLocaleString('ja-JP')}
// Shortcuts. Typing fields keep their keys; on a focused button or checkbox, Space and K
// toggle playback instead of pressing that control again (a label button must not add a
// second label). Space on a button fires its click on keyup, so that keyup is swallowed too.
export function typingTarget(el){if(!el)return false;if(el.tagName==='TEXTAREA'||el.tagName==='SELECT'||el.isContentEditable)return true;
 return el.tagName==='INPUT'&&!['checkbox','radio','button','submit','range'].includes((el.type||'').toLowerCase())}
