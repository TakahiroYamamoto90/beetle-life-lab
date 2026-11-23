"""Host protocol mocks only: not a LittleFS power-loss validation."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from evidence_client import export,log_digest,reduce_events,rotate,saved_matches,validate_event


def event(seq=1,boot='a'*32,kind='BOOT'):
    device='xiao-0123456789ab'
    return dict(schema_version='bll.evidence.v1',device_id=device,boot_id=boot,seq=seq,
        event_id=f'{device}/{boot}/{seq}',stream_id=f'live/{device}',origin='LIVE',source='XIAO',
        timestamp_utc=None,time_quality='UNSYNCED',monotonic_ms=123,event_type=kind,
        quality=dict(valid=True,reason_codes=[]),result=dict(scope='DEVICE_DIAGNOSTIC_ONLY'),
        case_id=None,session_id=None,observation_id=None,request_id=None,image_ref=None,model_version=None,
        producer_version='0.7.0-evidence-littlefs')


class FakePort:
    def __init__(self,events,bad_cursor=False):self.events=events;self.pending=[];self.bad_cursor=bad_cursor
    def write(self,command):
        if command==b'evidence-status\n':
            state=reduce_events(self.events);state.update(mounted=True,device_id=self.events[0]['device_id'])
            self.pending=[('EVIDENCE_STATUS '+json.dumps(state)+'\n').encode()]
        else:
            index=int(command.decode().split()[1]);page=self.events[index:index+16];next_index=index+len(page)
            self.pending=[f'EVIDENCE_BEGIN after_index={index}\n'.encode()]
            self.pending.extend(('EVIDENCE_EVENT '+json.dumps(e)+'\n').encode() for e in page)
            self.pending.append(f'EVIDENCE_END next_index={next_index+int(self.bad_cursor)} has_more={int(next_index<len(self.events))}\n'.encode())
    def flush(self):pass
    def readline(self,limit):return self.pending.pop(0) if self.pending else b''


class EvidenceTests(unittest.TestCase):
    def test_nano_esp32_device_ids_are_accepted_and_others_rejected(self):
        # 0.16.0: the Arduino Nano ESP32 build writes nano-<mac> (2026-11-23)
        nano=event();device='nano-0123456789ab'
        nano.update(device_id=device,event_id=f'{device}/{"a"*32}/1',stream_id=f'live/{device}')
        validate_event(nano)
        for bad in ('uno-0123456789ab','nano-0123456789AB','nano-0123456789a'):
            other=copy.deepcopy(nano);other.update(device_id=bad,event_id=f'{bad}/{"a"*32}/1',stream_id=f'live/{bad}')
            with self.subTest(bad=bad),self.assertRaisesRegex(ValueError,'Invalid device ID'):validate_event(other)
        mixed=copy.deepcopy(nano);mixed['stream_id']='live/xiao-0123456789ab'
        with self.assertRaisesRegex(ValueError,'Invalid stream'):validate_event(mixed)

    def test_restore_counts_across_boots_and_diagnostics_does_not_confirm_movement(self):
        state=reduce_events([event(),event(2,kind='DEVICE_DIAGNOSTIC'),event(1,'b'*32)])
        self.assertEqual(state['boot_events'],2)
        self.assertEqual(state['diagnostic_events'],1)
        self.assertEqual(state['current_activity'],'UNKNOWN')
        self.assertIsNone(state['last_confirmed_movement_utc'])

    def test_duplicate_is_idempotent_and_conflicting_duplicate_is_rejected(self):
        e=event();self.assertEqual(reduce_events([e,e])['events'],1)
        other=copy.deepcopy(e);other['monotonic_ms']=456
        with self.assertRaises(ValueError):reduce_events([e,other])

    def test_replay_and_activity_promotion_and_fabricated_time_rejected(self):
        for key,value in [('origin','RECORDED'),('origin','SYNTHETIC'),('event_type','VISION_MOVEMENT'),
                          ('timestamp_utc','2026-01-01T00:00:00Z'),('seq',True),('stream_id','live/another')]:
            e=event();e[key]=value
            with self.assertRaises(ValueError):validate_event(e)

    def test_pagination_exports_same_reduced_state(self):
        events=[event(i,kind='DEVICE_DIAGNOSTIC') for i in range(1,40)]
        state,download,mirror=export(FakePort(events))
        self.assertEqual(download,events)
        self.assertEqual(state['events'],39)
        self.assertEqual(mirror['diagnostic_events'],39)

    def test_bad_page_cursor_is_not_accepted(self):
        with self.assertRaises(ValueError):export(FakePort([event()],True))

    def test_fragmented_serial_lines_preserve_json_and_page_boundaries(self):
        class Fragmented(FakePort):
            def readline(self,limit):
                if not self.pending:return b''
                value=self.pending[0][:7];self.pending[0]=self.pending[0][7:]
                if not self.pending[0]:self.pending.pop(0)
                return value
        events=[event(i) for i in range(1,19)]
        self.assertEqual(export(Fragmented(events))[1],events)

    def test_raw_lines_are_kept_verbatim_and_hashed_with_newlines(self):
        events=[event(i,kind='DEVICE_DIAGNOSTIC') for i in range(1,20)]
        lines=[];export(FakePort(events),lines)
        self.assertEqual(lines,[json.dumps(e) for e in events])
        count,size,sha=log_digest(lines)
        data=''.join(line+'\n' for line in lines).encode()
        self.assertEqual((count,size,sha),(19,len(data),hashlib.sha256(data).hexdigest()))

    def test_saved_export_must_match_lines_and_device_count(self):
        lines=[json.dumps(event(i)) for i in range(1,4)]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'e.jsonl';path.write_bytes(''.join(l+'\n' for l in lines).encode())
            self.assertTrue(saved_matches(path,lines,dict(events=3)))
            self.assertFalse(saved_matches(path,lines,dict(events=4)))      # device has more events
            path.write_bytes(''.join(l+'\r\n' for l in lines).encode())     # text-mode rewrite
            self.assertFalse(saved_matches(path,lines,dict(events=3)))

    def test_rotate_sends_count_and_hash_and_needs_verification(self):
        class RotatePort:
            def __init__(self,replies):self.replies=replies;self.pending=[];self.sent=[]
            def write(self,command):self.sent.append(command);self.pending=[(r+'\n').encode() for r in self.replies]
            def flush(self):pass
            def readline(self,limit):return self.pending.pop(0) if self.pending else b''
        lines=[json.dumps(event(i)) for i in range(1,3)]
        count,_,sha=log_digest(lines)
        port=RotatePort([f'EVIDENCE_ROTATE stage=verified events=2 bytes=1 sha256={sha}',
                         'EVIDENCE_ROTATE stage=done removed_events=2 diagnostic=stored restart=1'])
        self.assertEqual(rotate(port,lines),dict(removed_events='2',diagnostic='stored',restart='1'))
        self.assertEqual(port.sent,[f'evidence-rotate {count} {sha}\n'.encode()])
        with self.assertRaises(RuntimeError):  # the XIAO refuses: nothing removed
            rotate(RotatePort(['EVIDENCE_ERROR reason=rotate_mismatch device_events=3 device_sha256='+'0'*64]),lines)
        with self.assertRaises(ValueError):
            rotate(RotatePort(['EVIDENCE_ROTATE stage=done removed_events=2 diagnostic=stored restart=1']),lines)


if __name__=='__main__':unittest.main()
