"""Feed the XIAO's low-frequency forest (firmware 0.15.0+, reference only, 2026-11-21).

During a LIVE forwarded capture the bridge computes the causal 250 ms CH1/CH2 1-40 Hz RMS
(lowfreq_activity.LowTracker: measured values, not decisions) and sends each window to the XIAO
("lowfreq-rms"); the XIAO runs lowfreq_forest_core.h, masks its own arm motions and answers with
the forest output. The answers go to xiao_lowfreq.jsonl in the run folder (review page).

Never in the way of the observation loop: windows are sent only while the delivery thread is idle
(no candidate waiting or in flight); during an episode they wait and catch up afterwards. Any
failure turns this off for the run and is recorded; the capture, the candidates and the policy are
unaffected. Nothing is written to the XIAO's Evidence log. Older firmware echoes the unknown
command, which is recorded as UNSUPPORTED.
"""
import collections
import json
import threading
import time

import lowfreq_activity

SCHEMA = 'bll.xiao_lowfreq.v1'
FILE = 'xiao_lowfreq.jsonl'
NOTE = ('Reference only: the XIAO ran the low-frequency forest (lowfreq_forest_core.h) on the 250 ms '
        '1-40 Hz RMS windows the bridge computed from the AD3; arm = the XIAO\'s own servo motion. Not a '
        'detection, not in the Evidence log, not used by the Observation Policy.')
ANCHOR_S = 30.0          # refresh the AD3 -> XIAO clock anchor this often
MAX_QUEUED = 4 * 60 * 20  # 20 min of windows waiting (an episode takes < 4 min)
MAX_PER_FLUSH = 40       # windows per idle turn (~0.2 s), so a candidate never waits long behind a catch-up


def parse_reply(line):
    """'LFF t=.. p=.. a=.. arm=..[ span=t0,t1,peak,n]' -> dict (raises on anything else)."""
    head, _, rest = line.partition(' ')
    fields = dict(part.split('=', 1) for part in rest.split() if '=' in part)
    out = dict(kind=head, **fields)
    if head == 'LFF':
        out = dict(kind='LFF', t_ms=int(fields['t']), p=float(fields['p']), active=fields['a'] == '1',
                   arm=fields['arm'] == '1')
        if out['p'] < 0:
            out['p'] = None
    if 'span' in fields:
        t0, t1, peak, n = fields['span'].split(',')
        out['span'] = dict(t0_s=int(t0) / 1000, t1_s=int(t1) / 1000, peak_p=float(peak), windows=int(n))
    return out


class LowFreqForwarder:
    def __init__(self, run_id, directory, sample_rate):
        self.run_tag = run_id[:8]
        self.path = directory / FILE
        self.rate = sample_rate
        self.tracker = lowfreq_activity.LowTracker()
        self.taken = 0
        self.rows = collections.deque()
        self.lock = threading.Lock()
        self.clock = None            # (samples so far, monotonic time they arrived)
        self.state = 'NEW'
        self.last_anchor = 0.0
        self.stats = dict(windows_sent=0, spans=0, dropped=0, max_backlog=0, errors=[])

    # ---- acquisition thread ----
    def push(self, values, samples_after):
        if self.state in ('UNSUPPORTED', 'FAILED', 'ENDED'):
            return
        self.tracker.push(values)
        new = self.tracker.rows[self.taken:]
        self.taken = len(self.tracker.rows)
        with self.lock:
            for t, ch1, ch2 in new:
                self.rows.append((round(t * 1000), max(0, round(ch1 * 1e9)), max(0, round(ch2 * 1e9))))
            while len(self.rows) > MAX_QUEUED:
                self.rows.popleft()
                self.stats['dropped'] += 1
            self.stats['max_backlog'] = max(self.stats['max_backlog'], len(self.rows))
            self.clock = (samples_after, time.monotonic())

    # ---- delivery thread (owns the serial port) ----
    def _ad3_now_ms(self):
        samples, at = self.clock
        return round(samples / self.rate * 1000 + (time.monotonic() - at) * 1000)

    def _log(self, records):
        with self.path.open('a', encoding='utf-8') as stream:
            for r in records:
                stream.write(json.dumps(r, ensure_ascii=False, allow_nan=False) + '\n')

    def flush(self, link, final=False):
        if self.state in ('UNSUPPORTED', 'FAILED', 'ENDED') or not hasattr(link, 'lowfreq'):
            return
        records = []
        try:
            if self.state == 'NEW':
                with self.lock:
                    ready = self.clock is not None
                if not ready:
                    return
                ad3_now = self._ad3_now_ms()
                reply = link.lowfreq(f'lowfreq-begin {self.run_tag} {ad3_now}')
                if reply is None:
                    self.state = 'UNSUPPORTED'
                    records.append(dict(kind='UNSUPPORTED', schema=SCHEMA, reason='firmware echoed lowfreq-begin (before 0.15.0)'))
                    return
                begin = parse_reply(reply)
                if begin['kind'] != 'LFF_BEGIN':
                    raise RuntimeError(reply)
                self.state, self.last_anchor = 'ACTIVE', time.monotonic()
                records.append(dict(kind='BEGIN', schema=SCHEMA, note=NOTE, ad3_ms=ad3_now,
                                    **{k: v for k, v in begin.items() if k != 'kind'}))
            elif time.monotonic() - self.last_anchor >= ANCHOR_S:
                ad3_now = self._ad3_now_ms()
                anchor = parse_reply(link.lowfreq(f'lowfreq-anchor {ad3_now}') or '')
                if anchor['kind'] != 'LFF_ANCHOR':
                    raise RuntimeError('Unexpected anchor reply')
                self.last_anchor = time.monotonic()
                records.append(dict(kind='ANCHOR', ad3_ms=ad3_now, shift_ms=int(anchor.get('shift_ms', 0))))
            sent = 0
            while final or sent < MAX_PER_FLUSH:
                with self.lock:
                    if not self.rows:
                        break
                    t, ch1, ch2 = self.rows.popleft()
                reply = parse_reply(link.lowfreq(f'lowfreq-rms {t} {ch1} {ch2}') or '')
                if reply['kind'] != 'LFF' or reply['t_ms'] != t:
                    raise RuntimeError('Unexpected window reply')
                self.stats['windows_sent'] += 1
                sent += 1
                records.append(dict(kind='WINDOW', t_ms=t, p=reply['p'], active=reply['active'], arm=reply['arm']))
                if 'span' in reply:
                    self.stats['spans'] += 1
                    records.append(dict(kind='SPAN', **reply['span']))
            if final:
                end = parse_reply(link.lowfreq('lowfreq-end') or '')
                if end['kind'] != 'LFF_END':
                    raise RuntimeError('Unexpected end reply')
                if 'span' in end:
                    self.stats['spans'] += 1
                    records.append(dict(kind='SPAN', **end['span']))
                records.append(dict(kind='END'))
                self.state = 'ENDED'
        except Exception as exc:  # reference only: never stops the capture or the delivery
            self.state = 'FAILED'
            self.stats['errors'].append(f'{type(exc).__name__}: {exc}'[:200])
            records.append(dict(kind='ERROR', error=self.stats['errors'][-1]))
        finally:
            if records:
                self._log(records)

    def summary(self):
        return dict(schema=SCHEMA, file=FILE, status=self.state, **self.stats, note=NOTE)
