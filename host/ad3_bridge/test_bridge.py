"""Synthetic and protocol mock tests. No real acquisition or serial port is used."""
import copy
import ctypes as ct
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import shutil
import uuid

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from acquisition import WaveForms, AcquisitionError
from processing import Processor
from protocol import XiaoLink, candidate_payload, read_evidence_snapshot
from manager import BridgeManager
from server import handler_for
from evidence_client import validate_event, reduce_events


@contextmanager
def capture_fixture():
    # Inherit workspace ACLs. Python 3.13 TemporaryDirectory's 0700 Windows ACL
    # can exclude a managed sandbox token even for a directory it just created.
    root = ROOT / 'captures/test_tmp'
    root.mkdir(parents=True, exist_ok=True)
    directory = root / ('ad3-test-' + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield str(directory)
    finally:
        directory.resolve().relative_to(root.resolve())
        shutil.rmtree(directory)


def fixture_candidate():
    return dict(t_start_s=7.12, t_end_s=9.3, peak_rms_V=.0028, peak_score=12.3,
                quality='DEVELOPMENT_CANDIDATE')


def fixture_evidence():
    device, boot = 'xiao-0123456789ab', 'a' * 32
    return dict(schema_version='bll.evidence.v1', device_id=device, boot_id=boot, seq=2,
                event_id=f'{device}/{boot}/2', stream_id=f'live/{device}', origin='LIVE', source='PC_AD3',
                timestamp_utc=None, time_quality='UNSYNCED', monotonic_ms=1000,
                event_type='PASSIVE_VIBRATION_CANDIDATE', quality=dict(valid=True, reason_codes=[]),
                result=dict(scope='PASSIVE_CANDIDATE_ONLY', detector='B2_guarded_mad', source_run_id='b' * 32,
                            candidate_seq=1, source_start_ms=7120, source_end_ms=9300, peak_rms_uV=2800,
                            peak_score_milli=12300, config_sha256='c' * 64,
                            source_time_quality='SAMPLE_CLOCK_ONLY', clipping_verified=False),
                case_id=None, session_id=None, observation_id=None, request_id=None, image_ref=None,
                model_version=None, producer_version='0.8.0-ad3-passive-bridge')


class FakePort:
    def __init__(self, **kwargs):
        self.pending = bytearray()
        self.closed = False
        self.commands = []
        self.ack_status = 'stored'

    def open(self): pass
    def reset_input_buffer(self): self.pending.clear()
    def flush(self): pass
    def close(self): self.closed = True

    def write(self, wire):
        self.commands.append(wire)
        if wire == b'evidence-status\n':
            self.pending.extend(('EVIDENCE_STATUS ' + json.dumps(dict(writable=True,
                device_id='xiao-0123456789ab', boot_id='a' * 32, passive_candidates=0)) + '\n').encode())
        else:
            parts = wire.decode().split()
            self.pending.extend(f'PASSIVE_ACK run={parts[1]} candidate={parts[2]} status={self.ack_status} event_id=xiao-0123456789ab/{"a" * 32}/2\n'.encode())
        return len(wire)

    def readline(self, _limit):
        value = bytes(self.pending[:11])  # deliberately fragmented
        del self.pending[:11]
        return value


class BridgeTests(unittest.TestCase):
    def test_evidence_view_is_read_only_even_when_storage_is_not_writable(self):
        events = [fixture_evidence()]
        ports = []
        class SavedPort(FakePort):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                ports.append(self)
            def write(self, wire):
                self.commands.append(wire)
                if wire == b'evidence-status\n':
                    state = dict(reduce_events(events), mounted=True, writable=False,
                                 device_id=events[0]['device_id'])
                    self.pending.extend(('EVIDENCE_STATUS '+json.dumps(state)+'\n').encode())
                elif wire == b'evidence-read 0\n':
                    self.pending.extend(('EVIDENCE_BEGIN after_index=0\nEVIDENCE_EVENT '+json.dumps(events[0])+
                                         '\nEVIDENCE_END next_index=1 has_more=0\n').encode())
                else:
                    raise AssertionError('Viewer must never send a write command')
                return len(wire)
        state, result, mirror = read_evidence_snapshot('MOCK_PORT', serial_factory=SavedPort)
        self.assertEqual(result, events)
        self.assertFalse(state['writable'])
        self.assertEqual(mirror['passive_candidates'], 1)
        self.assertTrue(ports[0].closed)
        self.assertEqual(ports[0].commands, [b'evidence-status\n', b'evidence-read 0\n'])

    def test_evidence_read_reserves_device_and_releases_it_on_failure(self):
        with capture_fixture() as directory:
            def reader(port):
                self.assertEqual(port, 'MOCK_PORT')
                self.assertTrue(manager.snapshot()['reading_evidence'])
                with self.assertRaises(BlockingIOError):
                    manager.start(dict(mode='mock', duration_s=1))
                raise RuntimeError('MOCK_READ_FAILURE')
            manager = BridgeManager(directory, evidence_reader=reader)
            with self.assertRaisesRegex(RuntimeError, 'MOCK_READ_FAILURE'):
                manager.read_evidence('MOCK_PORT')
            self.assertFalse(manager.snapshot()['reading_evidence'])
            manager.state['running'] = True
            with self.assertRaises(BlockingIOError): manager.read_evidence('MOCK_PORT')

    def test_saved_exports_are_listed_and_read_without_the_device(self):
        with capture_fixture() as directory:
            root = Path(directory)
            def no_device(*args, **kwargs): raise AssertionError('saved exports must not touch the XIAO')
            manager = BridgeManager(root / 'ad3', link_factory=no_device, evidence_reader=no_device)
            line = json.dumps(fixture_evidence(), separators=(',', ':'))
            data = (line + '\n').encode()
            new = root / 'evidence' / ('evidence-' + 'd' * 32 + '.jsonl')
            new.parent.mkdir(parents=True)
            new.write_bytes(data)
            new.with_suffix('.json').write_text(json.dumps(dict(
                received_at_pc='2026-11-15T01:00:00+00:00', device_state=dict(device_id='xiao-0123456789ab', events=1),
                events_file_format='DEVICE_LINES_VERBATIM', log_events=1, log_sha256=hashlib.sha256(data).hexdigest(),
                rotation=dict(response=dict(removed_events='1')))), encoding='utf-8')
            old = root / 'flash_backup' / 'evidence-before-x' / ('evidence-' + 'e' * 32 + '.jsonl')
            old.parent.mkdir(parents=True)
            old.write_text(json.dumps(fixture_evidence()) + '\n', encoding='utf-8')  # older format, no metadata
            rows = manager.saved_evidence_list()
            self.assertEqual([r['name'] for r in rows],
                             ['evidence/evidence-' + 'd' * 32 + '.jsonl', 'flash_backup/evidence-before-x/evidence-' + 'e' * 32 + '.jsonl'])
            self.assertTrue(rows[0]['rotated'] and rows[0]['verbatim'])
            shown = manager.read_saved_evidence(rows[0]['name'])
            self.assertEqual((shown['delivery_mode'], shown['saved_export']['hash_verified']), ('SAVED_EXPORT', True))
            self.assertEqual(shown['events'], [fixture_evidence()])
            self.assertEqual(shown['host_mirror']['passive_candidates'], 1)
            self.assertIsNone(manager.read_saved_evidence(rows[1]['name'])['saved_export']['hash_verified'])
            new.write_bytes(data.replace(b'"seq":2', b'"seq":2 '))  # edited after the export
            self.assertFalse(manager.read_saved_evidence(rows[0]['name'])['saved_export']['hash_verified'])
            for bad in ('../ad3/x.jsonl', 'evidence/../evidence/evidence-' + 'd' * 32 + '.jsonl', None, 'evidence/other.jsonl'):
                with self.assertRaises(LookupError): manager.read_saved_evidence(bad)
            old.write_text(json.dumps(dict(fixture_evidence(), origin='SYNTHETIC')) + '\n', encoding='utf-8')
            with self.assertRaises(ValueError): manager.read_saved_evidence(rows[1]['name'])

    def test_processor_is_chunk_invariant_and_bounded(self):
        rng = np.random.default_rng(1)
        values = rng.normal(0, .001, (800000, 2))
        values[650000:700000, 1] *= 2
        outputs = []
        for chunksize in (5000, 7333):
            processor = Processor()
            rows, events = [], []
            for index in range(0, len(values), chunksize):
                new_rows, new_events = processor.push(values[index:index + chunksize], index)
                rows.extend(new_rows); events.extend(new_events)
            self.assertLess(len(processor.pending), processor.n)
            self.assertLessEqual(len(processor.tail), 5)
            self.assertEqual(processor.detector.trace, [])
            self.assertEqual(processor.detector.events, [])
            outputs.append((rows, events))
        for key in ('rms_V', 'smoothed_V', 'score'):
            a = [r[key] for r in outputs[0][0]]
            b = [r[key] for r in outputs[1][0]]
            self.assertEqual(a, b)
        self.assertEqual(outputs[0][1], outputs[1][1])

    def test_long_candidate_is_kept_incomplete_and_the_detector_restarts(self):
        """A level step that never returns (or long digging) used to stop the acquisition after
        15 s. Now the candidate is kept as INCOMPLETE (never sent to the XIAO) and the frozen
        detector restarts with a new warm-up; earlier windows are unchanged."""
        # A steady 600 Hz tone (like the mains-hum floor) that steps up 10 % at 7 s and stays.
        rng = np.random.default_rng(2)
        t = np.arange(3000000) / 100000
        values = rng.normal(0, 1e-6, (len(t), 2)) + np.sin(2 * np.pi * 600 * t)[:, None] * 1e-4
        values[700000:, 1] *= 1.1
        p, rows, events = Processor(), [], []
        for index in range(0, len(values), 5000):
            new_rows, new_events = p.push(values[index:index + 5000], index)
            rows.extend(new_rows); events.extend(new_events)
        long, = [e for e in events if e['quality'] == 'INCOMPLETE']
        self.assertEqual(long['end_reason'], 'FAULT:MAX_DURATION_MANUAL_RESET_REQUIRED')
        self.assertAlmostEqual(long['t_start_s'], 7.0, delta=.3)
        self.assertGreater(long['t_end_s'] - long['t_start_s'], 14.9)
        self.assertEqual([r['reason'] for r in p.resets], ['MAX_DURATION_MANUAL_RESET_REQUIRED'])
        self.assertAlmostEqual(p.resets[0]['t_s'], long['t_end_s'], delta=.05)
        after = [r for r in rows if r['t_center_s'] > p.resets[0]['t_s']]
        self.assertEqual(after[0]['state'], 'WARMUP')
        self.assertEqual(after[-1]['state'], 'MONITORING')
        self.assertTrue(any(r['state'] == 'FAULT' for r in rows))
        with self.assertRaises(ValueError):
            candidate_payload('a' * 32, 1, long, 'b' * 64)
        self.assertEqual(p.metadata['fault_policy']['reset_and_continue'], ['MAX_DURATION_MANUAL_RESET_REQUIRED'])

    def test_gap_and_nonfinite_are_not_normal(self):
        p = Processor()
        with self.assertRaises(ValueError): p.push(np.ones((5000, 2)), 1)
        with self.assertRaises(ValueError): p.push(np.full((5000, 2), np.nan), 0)
        p = Processor()
        with self.assertRaisesRegex(ValueError, 'CALIBRATION_NO_SIGNAL'):
            for index in range(0, 600000, 5000): p.push(np.zeros((5000, 2)), index)

    def test_payload_and_incomplete_guard(self):
        payload = candidate_payload('a' * 32, 1, fixture_candidate(), 'b' * 64)
        self.assertLess(len('passive-candidate ' + payload), 256)
        self.assertIn('1 7120 9300 2800 12300', payload)
        for mutation in ({'quality': 'INCOMPLETE'}, {'peak_score': float('nan')}, {'t_end_s': 1000}):
            with self.assertRaises((ValueError, OverflowError)):
                candidate_payload('a' * 32, 1, dict(fixture_candidate(), **mutation), 'b' * 64)

    def test_live_ack_is_validated_and_fragmented_reads_work(self):
        link = XiaoLink('MOCK_PORT', 'LIVE', serial_factory=FakePort)
        self.assertEqual(link.send('b' * 32, 1, fixture_candidate(), 'c' * 64)['status'], 'stored')
        link.port.ack_status = 'duplicate'
        self.assertEqual(link.send('b' * 32, 1, fixture_candidate(), 'c' * 64)['status'], 'duplicate')
        link.port.ack_status = 'validated_only'
        with self.assertRaises(RuntimeError): link.send('b' * 32, 1, fixture_candidate(), 'c' * 64)
        link.close()

    def test_mock_and_recorded_cannot_open_serial(self):
        for origin in ('SYNTHETIC', 'RECORDED'):
            with self.assertRaises(ValueError): XiaoLink('MOCK_PORT', origin, serial_factory=FakePort)
        with capture_fixture() as directory:
            manager = BridgeManager(directory)
            with self.assertRaises(ValueError):
                manager.start(dict(mode='mock', duration_s=1, forward=True, port='MOCK_PORT'))

    def test_sdk_loss_is_a_fault_before_any_data_is_emitted(self):
        sdk = object.__new__(WaveForms)
        sdk.handle = ct.c_int(1)
        def call(name, *args):
            if name == 'FDwfAnalogInStatusRecord':
                args[1]._obj.value = 100
                args[2]._obj.value = 3
                args[3]._obj.value = 0
        sdk.call = call
        with self.assertRaisesRegex(AcquisitionError, 'lost=3'):
            next(sdk.blocks(threading.Event()))

    def test_sdk_readback_waits_for_deferred_configuration(self):
        sdk = object.__new__(WaveForms)
        sdk.handle = ct.c_int(1)
        sdk.version = 'MOCK_SDK'
        sdk.path = 'MOCK_DLL'
        sdk.devices = lambda: [dict(index=0, device_type=10)]
        applied = False
        started = False

        def call(name, *args):
            nonlocal applied, started
            if name == 'FDwfAnalogInConfigure':
                if args[1:] == (1, 0): applied = True
                if args[1:] == (0, 1): started = True
            elif name.endswith('Get'):
                self.assertTrue(applied, 'Readback must follow Configure')
                self.assertFalse(started, 'Validate before acquisition starts')
                if name == 'FDwfAnalogInFrequencyGet': args[-1]._obj.value = 100000.0
                elif name == 'FDwfAnalogInChannelFilterGet': args[-1]._obj.value = 1
                elif name == 'FDwfAnalogInChannelRangeGet': args[-1]._obj.value = 5.0 if args[1] == 0 else 1.0

        class NoWait:
            def wait(self, _seconds): return False
        sdk.call = call
        metadata = sdk.start(0, 1, NoWait())
        self.assertTrue(started)
        self.assertEqual(metadata['sample_rate_hz'], 100000)
        self.assertEqual(metadata['channel_ranges_v'], [5, 1])

    def test_candidate_keeps_activity_unknown_and_rejects_false_provenance(self):
        event = fixture_evidence()
        validate_event(event)
        state = reduce_events([event, event])
        self.assertEqual(state['passive_candidates'], 1)
        self.assertEqual(state['current_activity'], 'UNKNOWN')
        self.assertIsNone(state['last_confirmed_movement_utc'])
        for mutate in (lambda e: e.update(origin='RECORDED'), lambda e: e.update(source='XIAO'),
                       lambda e: e['result'].update(source_end_ms=0),
                       lambda e: e['result'].update(clipping_verified=True)):
            changed = copy.deepcopy(event); mutate(changed)
            with self.assertRaises(ValueError): validate_event(changed)

    def test_schema_accepts_supported_event_and_rejects_wrong_source(self):
        import jsonschema
        schema = json.loads((ROOT / 'schemas/evidence_v1.schema.json').read_text())
        event = fixture_evidence()
        jsonschema.validate(event, schema)
        event['source'] = 'XIAO'
        with self.assertRaises(jsonschema.ValidationError): jsonschema.validate(event, schema)

    def test_mock_saves_files_and_http_blocks_remote_posts(self):
        with capture_fixture() as directory:
            manager = BridgeManager(directory)
            manager.start(dict(mode='mock', duration_s=1, forward=False))
            manager.thread.join(timeout=10)
            state = manager.snapshot()
            self.assertEqual(state['state'], 'complete')
            self.assertEqual(state['origin'], 'SYNTHETIC')
            self.assertEqual(state['forwarded'], 0)
            capture = Path(directory) / ('ad3-' + state['run_id'])
            manifest = json.loads((capture / 'manifest.json').read_text())
            self.assertEqual(manifest['samples'], 100000)
            self.assertEqual((capture / 'samples.f64').stat().st_size, 1600000)
            self.assertEqual(manifest['origin'], 'SYNTHETIC')
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(manager, 'test-token'))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                conn = HTTPConnection('127.0.0.1', server.server_port)
                conn.request('GET', '/api/state', headers={'Host': 'evil.example'})
                response = conn.getresponse(); self.assertEqual(response.status, 403); response.read()
                conn.request('POST', '/api/start', '{}', {'Content-Type': 'application/json'})
                response = conn.getresponse(); self.assertEqual(response.status, 403); response.read()
                conn.request('GET', '/api/state')
                response = conn.getresponse(); self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())['origin'], 'SYNTHETIC')
                conn.request('GET', '/evidence_groups.js')
                response = conn.getresponse(); self.assertEqual(response.status, 200)
                self.assertIn('application/javascript', response.getheader('Content-Type'))
                self.assertIn(b'groupEvidence', response.read())
                conn.close()
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_operator_annotations_are_pc_only_and_only_while_capturing(self):
        with capture_fixture() as directory:
            manager = BridgeManager(directory)
            with self.assertRaises(BlockingIOError):
                manager.annotate('DIG_START')
            manager.start(dict(mode='mock', duration_s=3, forward=False))
            deadline = time.monotonic() + 5
            while manager.snapshot().get('state') != 'capturing' or manager.snapshot().get('samples', 0) == 0:
                self.assertLess(time.monotonic(), deadline); time.sleep(.02)
            with self.assertRaises(ValueError):
                manager.annotate('LARVA_PRESENT')
            first = manager.annotate('DIG_START')
            manager.annotate('DIG_END')
            self.assertEqual(first['source'], 'HUMAN_OPERATOR')
            self.assertGreater(first['t_s'], 0)
            manager.thread.join(timeout=10)
            with self.assertRaises(BlockingIOError):
                manager.annotate('NOISE')
            state = manager.snapshot()
            self.assertEqual([a['kind'] for a in state['annotations']], ['DIG_START', 'DIG_END'])
            capture = Path(directory) / ('ad3-' + state['run_id'])
            lines = [json.loads(x) for x in (capture / 'annotations.jsonl').read_text().splitlines()]
            self.assertEqual([x['seq'] for x in lines], [1, 2])
            manifest = json.loads((capture / 'manifest.json').read_text())
            self.assertEqual(manifest['annotations']['count'], 2)
            self.assertNotIn('annotation', (capture / 'candidates.jsonl').read_text())


if __name__ == '__main__':
    unittest.main()
