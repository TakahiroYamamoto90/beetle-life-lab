"""Clock protocol fixtures are synthetic; tests never open hardware or set PC time."""
import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from evidence_client import validate_event, reduce_events, export
from time_sync import sync_on_port, evidence_times, ad3_candidate_time, iso_utc, iso_jst

DEVICE = 'xiao-0123456789ab'
BOOT = 'a' * 32
EPOCH = 1793469600000  # 2026-10-31T18:00:00Z


def event(seq, kind='BOOT', mono=500, boot=BOOT):
    return dict(schema_version='bll.evidence.v1', device_id=DEVICE, boot_id=boot, seq=seq,
                event_id=f'{DEVICE}/{boot}/{seq}', stream_id=f'live/{DEVICE}', origin='LIVE',
                source='XIAO', timestamp_utc=None, time_quality='UNSYNCED', monotonic_ms=mono,
                event_type=kind, quality=dict(valid=True, reason_codes=[]),
                result=dict(scope='DEVICE_DIAGNOSTIC_ONLY'), case_id=None, session_id=None,
                observation_id=None, request_id=None, image_ref=None, model_version=None,
                producer_version='MOCK_CLOCK_FIXTURE')


def clock_events():
    sync = event(2, 'TIME_SYNC', 1100)
    sync.update(source='PC_CLOCK', time_quality='ESTIMATED', timestamp_utc=iso_utc(EPOCH+100),
                clock_sync_event_id=sync['event_id'])
    sync['result'] = dict(scope='CLOCK_REFERENCE_ONLY', utc_unix_ms=EPOCH, device_monotonic_ms=1000,
                          transport_uncertainty_ms=11, pc_clock_accuracy='UNVERIFIED', sync_id='c'*32)
    after = event(3, 'DEVICE_DIAGNOSTIC', 1200)
    after.update(time_quality='ESTIMATED', timestamp_utc=iso_utc(EPOCH+200), clock_sync_event_id=sync['event_id'])
    return [event(1), sync, after]


class ClockPort:
    def __init__(self, wrong_boot=False, wrong_nonce=False):
        self.pending = bytearray()
        self.commands = []
        self.wrong_boot, self.wrong_nonce = wrong_boot, wrong_nonce

    def write(self, wire):
        self.commands.append(wire)
        parts = wire.decode().split()
        if parts[0] == 'evidence-clock-probe':
            nonce = '0'*32 if self.wrong_nonce else parts[1]
            boot = 'b'*32 if self.wrong_boot else BOOT
            self.pending.extend(f'EVIDENCE_CLOCK_PROBE nonce={nonce} boot_id={boot} monotonic_ms=1000\n'.encode())
        else:
            self.pending.extend(f'EVIDENCE_TIME_SYNC status=stored event_id={DEVICE}/{BOOT}/2 time_quality=ESTIMATED\n'.encode())
        return len(wire)

    def flush(self): pass
    def readline(self, _limit):
        value = bytes(self.pending[:9]); del self.pending[:9]
        return value


class ExportPort(ClockPort):
    def __init__(self, events):
        super().__init__(); self.events = events

    def write(self, wire):
        if wire == b'evidence-status\n':
            state = dict(reduce_events(self.events), mounted=True, writable=True, device_id=DEVICE,
                         boot_id=BOOT, time_quality='ESTIMATED', clock_sync_event_id=f'{DEVICE}/{BOOT}/2', clock_age_ms=200)
            self.pending.extend(('EVIDENCE_STATUS '+json.dumps(state)+'\n').encode())
        else:
            self.pending.extend(b'EVIDENCE_BEGIN after_index=0\n')
            for e in self.events: self.pending.extend(('EVIDENCE_EVENT '+json.dumps(e)+'\n').encode())
            self.pending.extend(f'EVIDENCE_END next_index={len(self.events)} has_more=0\n'.encode())
        return len(wire)


class TimeSyncTests(unittest.TestCase):
    def state(self):
        return dict(clock_sync_supported=True, writable=True, device_id=DEVICE, boot_id=BOOT)

    def test_round_trip_midpoint_and_commit_are_bound_to_boot(self):
        port = ClockPort()
        wall = iter([EPOCH*1000000, EPOCH*1000000+20000000])
        mono = iter([100000000, 120000000])
        result = sync_on_port(port, self.state(), lambda: next(wall), lambda: next(mono))
        self.assertEqual(result['reference_utc'], iso_utc(EPOCH+10))
        self.assertEqual(result['transport_uncertainty_ms'], 11)
        self.assertIn(f'{EPOCH+10} 11'.encode(), port.commands[1])
        self.assertEqual(result['time_quality'], 'ESTIMATED')
        self.assertEqual(result['pc_clock_accuracy'], 'UNVERIFIED')

    def test_delayed_probe_pc_clock_jump_or_wrong_boot_never_commit(self):
        cases = [(ClockPort(), 2000000000, 2000000000),
                 (ClockPort(), 20000000, 100000000),
                 (ClockPort(wrong_boot=True), 20000000, 20000000),
                 (ClockPort(wrong_nonce=True), 20000000, 20000000)]
        for port, rtt, wall_delta in cases:
            with self.subTest(rtt=rtt, wall_delta=wall_delta):
                wall = iter([EPOCH*1000000, EPOCH*1000000+wall_delta])
                mono = iter([100000000, 100000000+rtt])
                with self.assertRaises(RuntimeError):
                    sync_on_port(port, self.state(), lambda: next(wall), lambda: next(mono))
                self.assertEqual(len(port.commands), 1)

    def test_jst_rollover_same_boot_estimates_and_original_data_unchanged(self):
        events = clock_events() + [event(1, boot='b'*32), event(4, mono=3602000)]
        saved = copy.deepcopy(events)
        mapped = evidence_times(events)
        self.assertEqual(mapped[events[2]['event_id']]['timestamp_jst'], '2026-11-01T03:00:00.200+09:00')
        self.assertEqual(mapped[events[0]['event_id']]['basis'], 'SAME_BOOT_REFERENCE')
        self.assertNotIn(events[3]['event_id'], mapped)
        self.assertNotIn(events[4]['event_id'], mapped)
        self.assertEqual(events, saved)
        self.assertEqual(iso_jst(1835481599999), '2028-03-01T08:59:59.999+09:00')

    def test_export_validates_references_schema_and_utc_arithmetic(self):
        import jsonschema
        events = clock_events()
        schema = json.loads((ROOT/'schemas/evidence_v1.schema.json').read_text())
        for e in events:
            validate_event(e); jsonschema.validate(e, schema)
        state, downloaded, mirror = export(ExportPort(events))
        self.assertEqual(downloaded, events)
        self.assertEqual(mirror['time_sync_events'], 1)
        self.assertEqual(mirror['time_quality'], 'ESTIMATED')
        bad = copy.deepcopy(events); bad[2]['timestamp_utc'] = iso_utc(EPOCH+201)
        with self.assertRaisesRegex(ValueError, 'UTC/reference mismatch'): export(ExportPort(bad))
        with self.assertRaises(ValueError): export(ExportPort([events[0], events[2]]))
        bad = copy.deepcopy(events[1]); bad['result']['pc_clock_accuracy'] = 'NTP_VERIFIED'
        with self.assertRaises(ValueError): validate_event(bad)

    def test_ad3_source_clock_is_separate_from_xiao_receipt_and_old_runs_stay_unknown(self):
        candidate = dict(result=dict(source_run_id='d'*32, source_start_ms=2000, source_end_ms=3000))
        manifest = dict(run_id='d'*32, origin='LIVE', acquisition=dict(sample_clock_reference=dict(
            basis='PC_CONFIGURE_BRACKET', pc_utc_before_ms=EPOCH, pc_utc_after_ms=EPOCH+20)))
        value = ad3_candidate_time(candidate, manifest)
        self.assertEqual(value['start_jst'], '2026-11-01T03:00:02.010+09:00')
        self.assertEqual(value['hardware_start_accuracy'], 'UNVERIFIED')
        self.assertIsNone(ad3_candidate_time(candidate, dict(manifest, origin='SYNTHETIC')))
        self.assertIsNone(ad3_candidate_time(candidate, dict(manifest, acquisition={})))


if __name__ == '__main__': unittest.main()
