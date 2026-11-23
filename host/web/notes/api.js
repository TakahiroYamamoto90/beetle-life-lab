// 観察ノート: DOM lookup, messages, the local request token (meta tag) and JSON API calls.
// Split from vision_capture/index.html on 2026-11-14 without behaviour changes.

export const TOKEN=()=>document.querySelector('meta[name="bll-token"]').content;
export const $=id=>document.getElementById(id);
export function message(text,error=false){$('message').textContent=text;$('message').classList.toggle('error',error)}
export async function api(path,data){const options=data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-BLL-Token':TOKEN()},body:JSON.stringify(data)};const response=await fetch(path,options);const result=await response.json();if(!response.ok)throw Error(result.error||'操作に失敗しました');return result}
