// 取得画面: DOM lookup, the local request token (meta tag) and JSON API calls.
// Split from ad3_bridge/index.html on 2026-11-14 without behaviour changes.

export const token=()=>document.querySelector('meta[name="bll-token"]').content, $=id=>document.getElementById(id);
export async function api(path,data){const options=data?{method:'POST',headers:{'Content-Type':'application/json','X-BLL-Token':token()},body:JSON.stringify(data)}:{};
const response=await fetch(path,options),body=await response.json();if(!response.ok)throw Error(body.error||response.status);return body;}
