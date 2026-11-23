// Annotation progress of a saved AD3 run (review_status from /api/review/runs and /overview):
// 未着手 until anything is saved in the review, then 作業中; 完了 only when a person marks it.
// Shown in front of each run in the run pull-downs (振り返り・観察エピソード・デモ再生).
export const STATUS_JA={NOT_STARTED:'未着手',IN_PROGRESS:'作業中',DONE:'完了'};
export function statusPrefix(status){return `【${STATUS_JA[status]||'状態不明'}】`}
// Replace the prefix of an option already in a pull-down (after a label is saved or 完了 changes).
export function setOptionStatus(select,run,status){for(const o of select.options)if(o.value===run)o.textContent=statusPrefix(status)+o.textContent.replace(/^【[^】]*】/,'')}
