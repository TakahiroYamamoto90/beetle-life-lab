// 観察ノート: display names of labels, poses, sessions, cases and dataset roles.
// Split from vision_capture/index.html on 2026-11-14 without behaviour changes.
import {$} from './api.js';
import {state} from './control.js';

export const labels={unreviewed:'未確認',larva_visible:'幼虫が見える',background:'幼虫が見えない',uncertain:'判別不能',unusable:'使用不能',exclude:'除外'};
export const POSES={FIXED:'固定カメラ',FRONT:'前面（0°）',TOP:'上面（90°）',BACK:'背面（180°）',ARM:'アーム（前面・上面・背面）'};
export function poseName(pose){return POSES[pose]||pose||'固定カメラ'}
export const CASE_ROLE_TEXT={unassigned:'役割未設定',training:'学習用',evaluation:'評価用'};
export function caseOf(id){return (state.cases||[]).find(c=>c.case_id===id)}
export function caseName(id){if(!id)return 'ケース未指定';const c=caseOf(id);return c?.display_name?`${c.display_name}（${id}）`:id}
export function isLoop(session){return session?.acquisition==='OBSERVATION_LOOP'}
export function sessionKind(session){return session.purpose==='setup'?'設置・動作確認':isLoop(session)?'観測ループ':'手動撮影'}
export function sessionTag(session){return isLoop(session)&&session.camera_pose!=='ARM'?` ${poseName(session.camera_pose)}`:''}
// The case shown for a session: the recorded one, or where its images were moved (訂正).
export function sessionCaseText(session){const cases=new Set(state.samples.filter(s=>s.session_id===session.id).map(s=>s.case_id??session.box_id));
 if(!cases.size||(cases.size===1&&cases.has(session.box_id)))return caseName(session.box_id);
 return cases.size===1?`${caseName([...cases][0])}（訂正）`:`${[...cases].map(caseName).join('・')}（一部訂正）`}
export function sessionLabel(session){const date=new Date(session.created).toLocaleString('ja-JP');return session.title?`${session.title} · ${sessionCaseText(session)}`:`${sessionKind(session)}${sessionTag(session)} · ${sessionCaseText(session)} · ${date}`}
export function sessionOf(item){return state.sessions.find(s=>s.id===item?.session_id)}
export function trainedOf(item){return (state.trained||{})[item?.id]||[]}
// Where an image may be used. Trained beats everything; then the per-image exception; then the case.
export function roleOf(item){if(trainedOf(item).length)return 'trained';if(item.dataset_role==='evaluation_only')return 'eval_image';const role=caseOf(item.case_id??sessionOf(item)?.box_id)?.role;return role==='evaluation'?'eval_case':role==='training'?'training':'unassigned'}
export function badge(text,kind=''){const b=document.createElement('span');b.className='badge'+(kind?' '+kind:'');b.textContent=text;return b}
