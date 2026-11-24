/* Display-only wording for XIAO observation decisions. Never changes evidence. */
(function (root) {
  'use strict';

  // Short Japanese explanations of the firmware reason codes (observation_core.h).
  const REASON_TEXT = Object.freeze({
    PASSIVE_CANDIDATE_NEW: '新しい振動候補',
    STATE_INCOMPLETE: '履歴の再構築が不完全',
    RECORDED_SOURCE_NO_ACTUATION: '記録データからは機器を動かさない',
    STORAGE_NOT_READY: '記録できないため行動しない',
    SAFETY_BLOCKED: '安全条件で停止',
    ACTION_IN_PROGRESS: '前の観測が未完了',
    DEVICE_BUSY: '機器が使用中',
    TRIGGER_FROM_PREVIOUS_BOOT: '再起動前の候補（経過時間が不明）',
    TRIGGER_STALE: '候補が古い',
    LOW_LIGHT: '暗い（撮影しても判別できない）',
    LIGHT_UNKNOWN: '明るさは未計測',
    CAMERA_COOLDOWN_ACTIVE: 'カメラの休止時間中',
    VISION_REPEATEDLY_FAILED: '撮影失敗が続いている',
    NO_CALIBRATED_VIEW: '使える視点がない',
    ALL_VIEWS_RECENTLY_CHECKED: 'どの視点も最近確認済み（新しい情報が少ない）',
    HISTORY_TIME_UNCOMPARABLE: '前回起動の撮影は経過時間が不明',
    PREVIOUS_ATTEMPT_FAILED: '前回の撮影は失敗',
    FIXED_RECENTLY_CHECKED: '固定カメラは最近確認済み',
    FRONT_RECENTLY_CHECKED: '前面は最近確認済み',
    TOP_RECENTLY_CHECKED: '上面は最近確認済み',
    BACK_RECENTLY_CHECKED: '背面は最近確認済み',
    FIXED_NOT_RECENTLY_OBSERVED: '固定カメラはしばらく見ていない',
    FRONT_NOT_RECENTLY_OBSERVED: '前面はしばらく見ていない',
    TOP_NOT_RECENTLY_OBSERVED: '上面はしばらく見ていない',
    BACK_NOT_RECENTLY_OBSERVED: '背面はしばらく見ていない',
    BASELINE_EVERY_CANDIDATE: '候補ごとに必ず撮影（記憶なし）',
    MODEL_UNVERIFIED: '視覚モデルは未検証',
    EPISODE_FOLLOW_UP: '同じ振動候補の続きで別の視点を確認',
    EPISODE_BUDGET_EXHAUSTED: 'この候補での撮影回数の上限に到達',
    SELF_ACTUATION_RECENT: 'アームが動いた直後のため待機（自分の動きによるノイズ＝ego-noiseの可能性）',
    LARVA_VISIBLE_STOP: '画像AIが幼虫を見つけたため、残りの方向は撮影しない',
    LARVA_LAST_SEEN_HERE: '前回、画像AIが幼虫を見つけた方向をもう一度確認（2分以内・連続3回まで）',
  });

  const ACTION_TEXT = Object.freeze({
    WAIT: '待機', CAPTURE_FIXED: '固定カメラで撮影', CAPTURE_FRONT: '前面を撮影',
    CAPTURE_TOP: '上面を撮影', CAPTURE_BACK: '背面を撮影', NONE: '判断なし',
  });

  const POSE_TEXT = Object.freeze({ FIXED: '固定カメラ', FRONT: '前面（0°）', TOP: '上面（90°）', BACK: '背面（180°）' });

  // Which arm view a decision targeted (from OBSERVATION_DECISION target_pose).
  function poseText(observation) {
    const fromAction = /^CAPTURE_(FIXED|FRONT|TOP|BACK)$/.exec(observation?.action || '')?.[1];
    const pose = observation?.target_pose || observation?.vision_ai?.pose || fromAction;
    return pose ? (POSE_TEXT[pose] || pose) : '—';
  }

  function reasonTexts(codes) {
    return (codes || []).map(code => REASON_TEXT[code] || code);
  }

  function actionText(action) {
    return ACTION_TEXT[action] || action || '—';
  }

  // One line for "WHY THIS ACTION?". Context reasons only; the trigger is implied.
  function whyText(observation) {
    const hidden = new Set(['PASSIVE_CANDIDATE_NEW', 'MODEL_UNVERIFIED', 'LIGHT_UNKNOWN']);
    const shown = (observation?.reason_codes || []).filter(code => !hidden.has(code));
    return `${reasonTexts(shown).join('・') || '新しい振動候補'} → ${actionText(observation?.action)}`;
  }

  // Never claim detection: a capture is an image, a failure is "could not observe".
  function resultText(observation) {
    const result = observation?.result;
    if (observation?.status === 'persist_failed') return '判断を保存できず行動しなかった';
    if (observation?.status === 'no_decision') return '判断なし（記録不可など）';
    if (!result) return observation?.action === 'WAIT' ? '撮影せず待機（記録済み）' : '結果未受信';
    if (result.capture === 'SUCCESS') return '撮影成功（幼虫の有無は未判定）';
    if (result.capture === 'NOT_ATTEMPTED') return `撮影せず：${result.reason}（観測できていない）`;
    return `撮影失敗：${result.reason}（観測できなかった。不在ではない）`;
  }

  // Vision V2 model output on the captured frame (0.14.0+). Reference only: the class
  // name is the model's output, not a finding; presence stays UNKNOWN everywhere.
  const CLASS_TEXT = Object.freeze({ VISIBLE_LARVA: '幼虫が見える', NO_VISIBLE_LARVA: '幼虫が見えない' });
  const VIEW_TEXT = Object.freeze({
    IN_TRAINING_VIEW: '',
    NEAR_TRAINING_VIEW: '（学習時の固定視点に近いが同一ではない）',
    OUTSIDE_TRAINING_VIEWS: '（学習外の視点・参考にならない可能性）',
  });

  function modelLabel(model) {
    if (!model) return '視覚モデル';
    return `視覚モデル ${model.recipe || ''} ${model.version}`.replace(/\s+/g, ' ');
  }

  function modelText(observation) {
    const ai = observation?.vision_ai;
    if (!ai) return observation?.result?.capture === 'SUCCESS' ? '推論なしの撮影' : '—';
    const p = ai.prediction || {};
    if (p.status === 'MODEL_NOT_VERIFIED') return 'モデル未確認のため表示しません';
    const view = VIEW_TEXT[ai.view_vs_training] ?? '';
    if (p.status !== 'MODEL_OUTPUT_UNVERIFIED' || !p.top)
      return `${modelLabel(ai.model)}（参考）：判定不能（${p.reason || '出力なし'}）`;
    const scores = (p.candidates || []).map(c => `${CLASS_TEXT[c.class_name] || c.class_name} ${c.score}`).join(' / ');
    return `${modelLabel(ai.model)}（参考）：${CLASS_TEXT[p.top.class_name] || p.top.class_name}` +
      `${scores ? `［${scores}］` : ''}${view}`;
  }

  function modelStatusText(check) {
    if (!check) return '';
    if (check.status === 'VERIFIED')
      return `${modelLabel(check.model)}（照合済み・参考表示。実機での正解率は未確立）`;
    return `視覚モデル：モデル未確認（${check.reason || '不明'}）。推論は表示しません`;
  }

  function compareCounts(snapshot) {
    const baseline = snapshot?.baseline_b_captures || 0;
    const proposed = snapshot?.proposed_captures || 0;
    return { baselineB: baseline, proposedCaptures: proposed,
      proposedWaits: snapshot?.proposed_waits || 0, avoided: Math.max(0, baseline - proposed) };
  }

  const api = Object.freeze({ REASON_TEXT, CLASS_TEXT, POSE_TEXT, poseText, reasonTexts, actionText, whyText, resultText,
    modelText, modelStatusText, compareCounts });
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.BllObservationView = api;
})(globalThis);
