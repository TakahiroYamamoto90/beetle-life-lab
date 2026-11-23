// Synthetic display fixtures only. No browser, hardware or live log writes.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const view = require('./observation_view.js');

test('every firmware reason code has Japanese text', () => {
  const header = fs.readFileSync(path.join(__dirname, '../../firmware/xiao_esp32s3/include/observation_core.h'), 'utf8');
  const table = header.slice(header.indexOf('static const char* const names[]'));
  const codes = [...table.slice(0, table.indexOf('};')).matchAll(/"([A-Z_]+)"/g)].map(m => m[1]);
  assert.ok(codes.length >= 27);
  for (const code of codes) assert.ok(view.REASON_TEXT[code], code);
});

test('why text explains context, result text never claims detection', () => {
  const capture = { action: 'CAPTURE_FIXED', status: 'decided',
    reason_codes: ['PASSIVE_CANDIDATE_NEW', 'FIXED_NOT_RECENTLY_OBSERVED', 'MODEL_UNVERIFIED'],
    result: { capture: 'SUCCESS', reason: 'none' } };
  assert.equal(view.whyText(capture), '固定カメラはしばらく見ていない → 固定カメラで撮影');
  assert.match(view.resultText(capture), /未判定/);
  const wait = { action: 'WAIT', status: 'decided', reason_codes: ['PASSIVE_CANDIDATE_NEW', 'ALL_VIEWS_RECENTLY_CHECKED'] };
  assert.match(view.whyText(wait), /最近確認済み.*待機/);
  const failed = { action: 'CAPTURE_FIXED', status: 'decided', reason_codes: [],
    result: { capture: 'FAILED', reason: 'timeout' } };
  assert.match(view.resultText(failed), /不在ではない/);
  for (const text of [view.resultText(capture), view.resultText(wait), view.resultText(failed)])
    assert.doesNotMatch(text, /発見|検出|生存|死亡/);
});

test('baseline comparison counts and unknown codes pass through', () => {
  assert.deepEqual(view.compareCounts({ baseline_b_captures: 4, proposed_captures: 2, proposed_waits: 2 }),
    { baselineB: 4, proposedCaptures: 2, proposedWaits: 2, avoided: 2 });
  assert.deepEqual(view.reasonTexts(['NEW_FUTURE_CODE']), ['NEW_FUTURE_CODE']);
});

test('committed demo scenarios are SYNTHETIC and use known reasons', () => {
  const data = JSON.parse(fs.readFileSync(path.join(__dirname, 'demo_scenarios.json'), 'utf8'));
  assert.equal(data.origin, 'SYNTHETIC');
  for (const scenario of data.scenarios)
    for (const step of scenario.steps)
      for (const code of step.reasons) assert.ok(view.REASON_TEXT[code], code);
});

test('model output is labelled reference-only, with view caveats and no detection words', () => {
  const model = { name: 'larva_visibility_int8_vela', version: '1.4.0', recipe: 'v12-192' };
  const shot = (pose, view, prediction, m = model) => ({ result: { capture: 'SUCCESS' },
    vision_ai: { pose, view_vs_training: view, model: m, prediction } });
  const visible = { status: 'MODEL_OUTPUT_UNVERIFIED', top: { class_name: 'VISIBLE_LARVA', score: 87 },
    candidates: [{ class_name: 'VISIBLE_LARVA', score: 87 }] };
  const front = view.modelText(shot('FRONT', 'NEAR_TRAINING_VIEW', visible));
  assert.match(front, /v12-192 1\.4\.0（参考）：幼虫が見える/);
  assert.match(front, /同一ではない/);
  assert.match(view.modelText(shot('TOP', 'OUTSIDE_TRAINING_VIEWS', visible)), /学習外の視点/);
  const hidden = view.modelText(shot('FRONT', 'NEAR_TRAINING_VIEW', { status: 'MODEL_NOT_VERIFIED' }, null));
  assert.equal(hidden, 'モデル未確認のため表示しません');
  assert.match(view.modelText(shot('FRONT', 'NEAR_TRAINING_VIEW', { status: 'UNKNOWN', reason: 'tied_class_scores' })), /判定不能/);
  assert.equal(view.modelText({ result: { capture: 'SUCCESS' } }), '推論なしの撮影');
  assert.equal(view.modelText({ action: 'WAIT' }), '—');
  assert.match(view.modelStatusText({ status: 'VERIFIED', model }), /照合済み.*正解率は未確立/);
  assert.match(view.modelStatusText({ status: 'UNVERIFIED', reason: 'inspect_timeout' }), /モデル未確認/);
  assert.equal(view.modelStatusText(null), '');
  for (const text of [front, hidden, view.modelStatusText({ status: 'VERIFIED', model })])
    assert.doesNotMatch(text, /発見|検出|生存|死亡/);
});

test('pose text names the arm view; waits have none', () => {
  assert.equal(view.poseText({ target_pose: 'FRONT' }), '前面（0°）');
  assert.equal(view.poseText({ target_pose: 'TOP' }), '上面（90°）');
  assert.equal(view.poseText({ target_pose: 'BACK' }), '背面（180°）');
  assert.equal(view.poseText({ target_pose: null, vision_ai: { pose: 'BACK' } }), '背面（180°）');
  assert.equal(view.poseText({ action: 'WAIT', target_pose: null }), '—');
});
