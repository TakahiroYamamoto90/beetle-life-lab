// Synthetic display fixtures only. No browser, hardware or live log writes.
const test = require('node:test');
const assert = require('node:assert/strict');
const { groupEvidence, visibleRows, pageGroups } = require('./evidence_groups.js');

const DEVICE = 'xiao-0123456789ab';
function event(boot, seq, type = 'DEVICE_DIAGNOSTIC', mono = seq * 100, device = DEVICE) {
  return { device_id: device, boot_id: boot, seq, event_type: type, monotonic_ms: mono,
    event_id: `${device}/${boot}/${seq}`, origin: 'SYNTHETIC', source: 'MOCK' };
}
function snapshot(events, currentBoot = 'b') {
  return { events, device_state: { device_id: DEVICE, boot_id: currentBoot, time_quality: 'ESTIMATED' },
    event_times: {} };
}
function freeze(value) {
  if (value && typeof value === 'object') {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

test('groups by device and boot; append history orders boots, not corrected UTC', () => {
  const oldBoot = event('a', 1, 'BOOT', 0), newBoot = event('b', 1, 'BOOT', 0);
  const data = snapshot([oldBoot, event('a', 2), newBoot, event('b', 2, 'TIME_SYNC')]);
  data.event_times[oldBoot.event_id] = { timestamp_jst: '2026-11-02T12:00:00+09:00' };
  data.event_times[newBoot.event_id] = { timestamp_jst: '2026-11-01T12:00:00+09:00' };
  const groups = groupEvidence(data);
  assert.deepEqual(groups.map(g => g.bootId), ['b', 'a']);
  assert.equal(groups[0].current, true);
  assert.equal(groups[1].current, false);
  assert.equal(groups[0].bootTime, data.event_times[newBoot.event_id]);
  assert.equal(groups[0].clockLabel, 'PC時刻基準（推定）');
});

test('same boot token on different devices never merges', () => {
  const data = snapshot([event('b', 1, 'BOOT'), event('b', 1, 'BOOT', 0, 'xiao-ffffffffffff')]);
  const groups = groupEvidence(data);
  assert.equal(groups.length, 2);
  assert.equal(groups.filter(g => g.current).length, 1);
});

test('a boot timeline is ascending with seq resolving equal monotonic times', () => {
  const groups = groupEvidence(snapshot([
    event('b', 1, 'BOOT', 0), event('b', 4, 'TIME_SYNC', 300),
    event('b', 3, 'DEVICE_DIAGNOSTIC', 100), event('b', 2, 'TIME_SYNC', 100),
  ]));
  assert.deepEqual(groups[0].rows.map(r => r.event.seq), [1, 2, 3, 4]);
  assert.equal(groups[0].counts.TIME_SYNC, 2);
  assert.equal(groups[0].lastEvent.seq, 4);
});

test('filters preserve complete boot context and counts', () => {
  const data = snapshot([
    event('a', 1, 'BOOT'), event('a', 2),
    event('b', 1, 'BOOT'), event('b', 2, 'TIME_SYNC'), event('b', 3, 'PASSIVE_VIBRATION_CANDIDATE'),
  ]);
  const page = pageGroups(groupEvidence(data), 'PASSIVE_VIBRATION_CANDIDATE', 0);
  assert.equal(page.total, 1);
  assert.equal(page.allTotal, 2);
  assert.equal(visibleRows(page.groups[0], 'PASSIVE_VIBRATION_CANDIDATE').length, 1);
  assert.equal(page.groups[0].bootEvent.event_type, 'BOOT');
  assert.equal(page.groups[0].rows.length, 3);
  assert.equal(page.groups[0].counts.TIME_SYNC, 1);
});

test('pagination keeps a large boot intact and clamps page after filtering', () => {
  const events = [];
  for (let boot = 0; boot < 12; boot++) {
    for (let seq = 1; seq <= 40; seq++) events.push(event(String(boot), seq, seq === 1 ? 'BOOT' : 'DEVICE_DIAGNOSTIC'));
  }
  const groups = groupEvidence(snapshot(events, '11'));
  const first = pageGroups(groups, 'all', 0), second = pageGroups(groups, 'all', 1);
  assert.equal(first.groups.length, 10);
  assert.equal(second.groups.length, 2);
  assert.ok([...first.groups, ...second.groups].every(g => g.rows.length === 40));
  assert.equal(pageGroups(groups, 'TIME_SYNC', 8).page, 0);
  assert.equal(pageGroups(groups, 'all', -1).page, 0);
  assert.equal(pageGroups(groups, 'all', 99).page, 1);
});

test('unsynced and missing BOOT do not invent start or shutdown timestamps', () => {
  const data = snapshot([event('a', 1, 'BOOT', 5), event('a', 2, 'DEVICE_DIAGNOSTIC', 1200),
    event('b', 2, 'TIME_SYNC', 1000)]);
  data.device_state.time_quality = 'UNSYNCED';
  const groups = groupEvidence(data);
  assert.equal(groups[0].bootEvent, null);
  assert.equal(groups[0].bootTime, null);
  assert.equal(groups[0].clockLabel, '同期履歴あり・読出し時点は未同期');
  assert.equal(groups[1].clockLabel, '未同期');
  assert.equal(groups[1].lastEvent.monotonic_ms, 1200);
  assert.equal(groups[1].bootTime, null);
  assert.equal(groups[1].shutdownTime, undefined);
});

test('grouping, filtering and paging never mutate original export or download data', () => {
  const data = freeze(snapshot([event('a', 1, 'BOOT'), event('a', 2), event('b', 1, 'BOOT')]));
  const before = JSON.stringify(data);
  const groups = groupEvidence(data);
  pageGroups(groups, 'DEVICE_DIAGNOSTIC', 0);
  assert.equal(JSON.stringify(data), before);
  assert.equal(groups[0].rows[0].event, data.events[2]);
});

test('empty evidence and unmatched filters produce an empty first page', () => {
  assert.deepEqual(groupEvidence(null), []);
  const page = pageGroups(groupEvidence(snapshot([])), 'all', 3);
  assert.equal(page.page, 0);
  assert.equal(page.total, 0);
  assert.deepEqual(page.groups, []);
});
