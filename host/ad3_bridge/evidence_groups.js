/* Display-only grouping. Keep the evidence array and event objects unchanged. */
(function (root) {
  'use strict';

  function groupEvidence(snapshot) {
    const groups = new Map();
    const state = snapshot?.device_state || {};
    for (const [index, event] of (snapshot?.events || []).entries()) {
      const key = `${event.device_id}/${event.boot_id}`;
      if (!groups.has(key)) {
        groups.set(key, {
          key, deviceId: event.device_id, bootId: event.boot_id, firstIndex: index,
          current: event.device_id === state.device_id && event.boot_id === state.boot_id,
          rows: [], counts: {}, bootEvent: null, bootTime: null, lastEvent: null, lastTime: null,
        });
      }
      const group = groups.get(key);
      group.rows.push({ event, index });
      group.counts[event.event_type] = (group.counts[event.event_type] || 0) + 1;
    }
    for (const group of groups.values()) {
      group.rows.sort((a, b) => a.event.monotonic_ms - b.event.monotonic_ms ||
        a.event.seq - b.event.seq || a.index - b.index);
      group.bootEvent = group.rows.find(row => row.event.event_type === 'BOOT')?.event || null;
      group.lastEvent = group.rows[group.rows.length - 1].event;
      group.bootTime = group.bootEvent ? snapshot.event_times?.[group.bootEvent.event_id] || null : null;
      group.lastTime = snapshot.event_times?.[group.lastEvent.event_id] || null;
      const synced = (group.counts.TIME_SYNC || 0) > 0;
      group.clockLabel = group.current && synced ?
        (state.time_quality === 'ESTIMATED' ? 'PC時刻基準（推定）' : '同期履歴あり・読出し時点は未同期') :
        (synced ? '時刻同期あり（履歴・推定）' : '未同期');
    }
    // Use append history for boot order: PC clock corrections must not reorder boots.
    return [...groups.values()].sort((a, b) => b.firstIndex - a.firstIndex);
  }

  function visibleRows(group, kind) {
    return kind === 'all' ? group.rows : group.rows.filter(row => row.event.event_type === kind);
  }

  function pageGroups(groups, kind, page, pageSize = 10) {
    if (!Number.isInteger(pageSize) || pageSize < 1) throw new Error('Invalid boot page size');
    const matching = groups.filter(group => visibleRows(group, kind).length > 0);
    const pages = Math.max(1, Math.ceil(matching.length / pageSize));
    const current = Math.max(0, Math.min(page, pages - 1));
    return {
      groups: matching.slice(current * pageSize, (current + 1) * pageSize),
      page: current, pages, total: matching.length, allTotal: groups.length,
    };
  }

  const api = Object.freeze({ groupEvidence, visibleRows, pageGroups });
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.BllEvidenceGroups = api;
})(globalThis);
