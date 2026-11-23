import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

import lowfreq_link
from lowfreq_link import LowFreqForwarder, parse_reply
from protocol import XiaoLink

FS = 100000


class LowFreqPort:
    """Answers like firmware 0.15.0: evidence-status, and lowfreq-* lines."""
    mode = 'ok'

    def __init__(self, **kwargs):
        self.pending = bytearray()
        self.commands = []

    def open(self): pass
    def reset_input_buffer(self): self.pending.clear()
    def flush(self): pass
    def close(self): pass

    def write(self, wire):
        self.commands.append(wire)
        text = wire.decode().strip()
        if text == 'evidence-status':
            self.pending.extend(('EVIDENCE_STATUS ' + json.dumps(dict(writable=True, device_id='xiao-0123456789ab',
                                 boot_id='a' * 32, passive_candidates=0)) + '\n').encode())
        elif self.mode == 'old':
            self.pending.extend(f'ECHO {text}\n'.encode())
        elif text.startswith('lowfreq-begin'):
            self.pending.extend(b'HEARTBEAT uptime_ms=1 led=on\nLFF_BEGIN run=' + text.split()[1].encode() +
                                b' core=lowfreq-forest-core-v1 trees=20 threshold=0.5781\n')
        elif text.startswith('lowfreq-rms'):
            if self.mode == 'error':
                self.pending.extend(b'LFF_ERROR reason=invalid_args\n')
            else:
                t = int(text.split()[1])
                span = ' span=100,600,0.812,2' if t == 875 else ''
                # a late park line arrives before the reply
                park = b'SERVO_PARK status=done pose=FRONT us=1500 elapsed_ms=900 (assumed)\n' if t == 625 else b''
                self.pending.extend(park + f'LFF t={t} p={-1 if t < 500 else 0.6} a={int(t >= 500)} arm=0{span}\n'.encode())
        elif text.startswith('lowfreq-anchor'):
            self.pending.extend(b'LFF_ANCHOR shift_ms=3\n')
        elif text == 'lowfreq-end':
            self.pending.extend(b'LFF_END run=0123abcd span=2000,2250,0.700,1\n')
        return len(wire)

    def readline(self, _limit):
        value = bytes(self.pending[:13])  # fragmented, as test_bridge.FakePort
        del self.pending[:13]
        return value


def link(mode='ok'):
    port = type('P', (LowFreqPort,), dict(mode=mode))
    return XiaoLink('MOCK_PORT', 'LIVE', serial_factory=port)


def blocks(seconds=3.0, n=20000):
    rng = np.random.default_rng(1)
    raw = rng.normal(0, 1e-4, (int(seconds * FS), 2))
    return [raw[i:i + n] for i in range(0, len(raw), n)]


class LowFreqLinkTest(unittest.TestCase):
    def feed(self, fwd, x):
        count = 0
        for b in blocks():
            count += len(b)
            fwd.push(b, count)
            fwd.flush(x)

    def records(self, d):
        return [json.loads(line) for line in (Path(d) / lowfreq_link.FILE).read_text(encoding='utf-8').splitlines()]

    def test_windows_are_sent_and_answers_saved(self):
        with tempfile.TemporaryDirectory() as d:
            x = link()
            fwd = LowFreqForwarder('0123abcd' + '0' * 24, Path(d), FS)
            self.feed(fwd, x)
            fwd.flush(x, final=True)
            recs = self.records(d)
            self.assertEqual(recs[0]['kind'], 'BEGIN')
            self.assertEqual(recs[0]['run'], '0123abcd')
            windows = [r for r in recs if r['kind'] == 'WINDOW']
            self.assertEqual([w['t_ms'] for w in windows], [125 + 250 * i for i in range(12)])
            self.assertIsNone(windows[0]['p'])
            self.assertTrue(windows[-1]['active'])
            spans = [r for r in recs if r['kind'] == 'SPAN']
            self.assertEqual(spans, [dict(kind='SPAN', t0_s=.1, t1_s=.6, peak_p=.812, windows=2),
                                     dict(kind='SPAN', t0_s=2.0, t1_s=2.25, peak_p=.7, windows=1)])
            self.assertEqual(recs[-1]['kind'], 'END')
            self.assertEqual(fwd.summary()['status'], 'ENDED')
            self.assertEqual(fwd.summary()['windows_sent'], 12)
            self.assertEqual([a['kind'] for a in x.actuation], ['PARK_DONE'])  # noted, not lost
            sent = [c.decode().split()[0] for c in x.port.commands if c.startswith(b'lowfreq-')]
            self.assertEqual(sent[0], 'lowfreq-begin')
            self.assertEqual(sent[-1], 'lowfreq-end')
            self.assertTrue(all(len(c) <= 64 for c in x.port.commands if c.startswith(b'lowfreq-rms')))

    def test_backlog_catches_up_in_bounded_turns(self):
        with tempfile.TemporaryDirectory() as d:
            x = link()
            fwd = LowFreqForwarder('0123abcd' + '0' * 24, Path(d), FS)
            count = 0
            for b in blocks(seconds=15.0):   # an episode: nothing is sent meanwhile
                count += len(b)
                fwd.push(b, count)
            fwd.flush(x)
            self.assertEqual(fwd.summary()['windows_sent'], lowfreq_link.MAX_PER_FLUSH)
            fwd.flush(x, final=True)
            self.assertEqual(fwd.summary()['windows_sent'], 60)

    def test_old_firmware_is_recorded_and_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            x = link('old')
            fwd = LowFreqForwarder('0123abcd' + '0' * 24, Path(d), FS)
            self.feed(fwd, x)
            fwd.flush(x, final=True)
            self.assertEqual(fwd.summary()['status'], 'UNSUPPORTED')
            self.assertEqual(len([c for c in x.port.commands if c.startswith(b'lowfreq-')]), 1)
            self.assertEqual(self.records(d)[0]['kind'], 'UNSUPPORTED')

    def test_an_error_turns_it_off_without_raising(self):
        with tempfile.TemporaryDirectory() as d:
            x = link('error')
            fwd = LowFreqForwarder('0123abcd' + '0' * 24, Path(d), FS)
            self.feed(fwd, x)
            fwd.flush(x, final=True)
            self.assertEqual(fwd.summary()['status'], 'FAILED')
            self.assertIn('LFF_ERROR', fwd.summary()['errors'][0])
            self.assertEqual(len([c for c in x.port.commands if c.startswith(b'lowfreq-rms')]), 1)

    def test_arm_lines_read_while_idle_are_written_without_a_later_candidate(self):
        """2026-11-21: a park end read by an idle lowfreq exchange after the last candidate stayed
        in link.actuation and never reached actuation.jsonl. Now it is written while idle / at stop."""
        import time
        from manager import BridgeManager

        class Source:
            def start(self, index, range_v, stop):
                return dict(source='FAKE_AD3', sample_rate_hz=FS)
            def blocks(self, stop):
                for b in blocks(seconds=2.0):
                    time.sleep(.15)  # leaves idle turns to the delivery thread
                    yield b
            def close(self): pass

        class Link:
            def __init__(self, port, origin):
                self.state, self.actuation = {}, []
            def lowfreq(self, command):
                if command.startswith('lowfreq-begin'):
                    return 'LFF_BEGIN run=' + command.split()[1] + ' core=lowfreq-forest-core-v1 trees=20 threshold=0.5781'
                if command.startswith('lowfreq-anchor'):
                    return 'LFF_ANCHOR shift_ms=0'
                if command == 'lowfreq-end':
                    self.actuation.append(dict(kind='PARK_FAILED', received_at_pc='end'))
                    return 'LFF_END run=x'
                t = int(command.split()[1])
                if t == 125:  # the park end arrives during an idle window exchange
                    self.actuation.append(dict(kind='PARK_DONE', received_at_pc='idle'))
                return f'LFF t={t} p=-1.0000 a=0 arm=0'
            def close(self): pass

        with tempfile.TemporaryDirectory() as d:
            manager = BridgeManager(Path(d), source_factory=Source, link_factory=Link)
            manager.start(dict(mode='live', duration_s=2, forward=True, port='MOCK_PORT', wiring_confirmed=True))
            manager.thread.join(60)
            run = Path(d) / ('ad3-' + manager.snapshot()['run_id'])
            arm = [json.loads(line) for line in (run / 'actuation.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual([a['kind'] for a in arm], ['PARK_DONE', 'PARK_FAILED'])
            self.assertEqual([a['candidate_seq'] for a in arm], [None, None])  # no candidate in this run
            self.assertEqual([a['kind'] for a in manager.snapshot()['arm']], ['PARK_DONE', 'PARK_FAILED'])

    def test_parse_reply(self):
        r = parse_reply('LFF t=1125 p=0.6123 a=1 arm=0 span=500,875,0.700,2')
        self.assertEqual((r['t_ms'], r['p'], r['active'], r['arm']), (1125, .6123, True, False))
        self.assertEqual(r['span'], dict(t0_s=.5, t1_s=.875, peak_p=.7, windows=2))
        self.assertIsNone(parse_reply('LFF t=125 p=-1.0000 a=0 arm=1')['p'])
        with self.assertRaises(ValueError):
            link().lowfreq('passive-candidate x')


if __name__ == '__main__':
    unittest.main()
