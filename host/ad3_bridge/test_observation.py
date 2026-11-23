"""Observation decision/result contracts and bridge handling. Synthetic fixtures only."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from evidence_client import validate_event, reduce_events
from manager import BridgeManager, save_observation_image
from protocol import XiaoLink
from test_bridge import FakePort, capture_fixture, fixture_candidate

DEVICE, BOOT = 'xiao-0123456789ab', 'a' * 32
ACTION = 'obs-' + BOOT + '-1'
JPEG = b'\xff\xd8\xff\xe0' + b'\x00\x10JFIF' + b'\x11' * 40 + b'\xff\xd9'
PAYLOAD = JPEG + b'\x00' * 7  # Vision V2 pads the transport buffer with zeros


def envelope(seq, event_type, result, observation_id=None, image_ref=None):
    return dict(schema_version='bll.evidence.v1', device_id=DEVICE, boot_id=BOOT, seq=seq,
                event_id=f'{DEVICE}/{BOOT}/{seq}', stream_id=f'live/{DEVICE}', origin='LIVE', source='XIAO',
                timestamp_utc=None, time_quality='UNSYNCED', monotonic_ms=1000 + seq, event_type=event_type,
                quality=dict(valid=True, reason_codes=[]), result=result, case_id=None, session_id=None,
                observation_id=observation_id, request_id=None, image_ref=image_ref, model_version=None,
                producer_version='0.10.0-observation-policy')


def decision(action='CAPTURE_FIXED'):
    capture = action != 'WAIT'
    return envelope(3, 'POLICY_DECISION', dict(
        scope='OBSERVATION_POLICY', kind='RULE_BASED_NOT_TRAINED', policy_version='observation-policy-v1',
        reducer_version='temporal-reducer-v1', decision_id='dec-' + BOOT + '-1', action=action,
        action_id=ACTION if capture else None, target_pose='FIXED' if capture else None,
        caused_by_event_ids=[f'{DEVICE}/{BOOT}/2'],
        reason_codes=['PASSIVE_CANDIDATE_NEW', 'FIXED_NOT_RECENTLY_OBSERVED' if capture else 'ALL_VIEWS_RECENTLY_CHECKED'],
        constraints=dict(live_mode=True, storage_writable=True, safety_ok=True, device_busy=False,
                         light='UNKNOWN', light_source='NO_SENSOR'),
        capabilities=['FIXED'], config=dict(view_recency_ms=30000)), ACTION if capture else None)


def action_result(capture='SUCCESS'):
    status = {'SUCCESS': 'COMPLETED', 'FAILED': 'FAILED', 'NOT_ATTEMPTED': 'FAILED', 'UNKNOWN': 'INTERRUPTED'}[capture]
    image = dict(store='HOST_USB_STREAM', status='SENT_TO_HOST_UNCONFIRMED',
                 payload_sha256=hashlib.sha256(PAYLOAD).hexdigest(), payload_bytes=len(PAYLOAD))
    return envelope(4, 'OBSERVATION_ACTION_RESULT', dict(
        scope='OBSERVATION_ACTION', action_id=ACTION, decision_event_id=f'{DEVICE}/{BOOT}/3', status=status,
        action='CAPTURE_FIXED', pose='FIXED',
        capture=dict(status=capture, reason='none' if capture == 'SUCCESS' else 'timeout', duration_ms=900,
                     presence='UNKNOWN', movement='UNKNOWN', presence_reason='LARVA_MODEL_UNVERIFIED')),
        ACTION, image if capture == 'SUCCESS' else None)


class ObservationContractTests(unittest.TestCase):
    def test_valid_events_pass_validator_and_schema(self):
        import jsonschema
        schema = json.loads((ROOT / 'schemas/evidence_v1.schema.json').read_text(encoding='utf-8'))
        for event in (decision(), decision('WAIT'), action_result(), action_result('FAILED'),
                      action_result('NOT_ATTEMPTED'), action_result('UNKNOWN')):
            validate_event(event)
            jsonschema.validate(event, schema)
        state = reduce_events([decision(), action_result()])
        self.assertEqual((state['decision_events'], state['action_result_events']), (1, 1))
        self.assertEqual(state['current_activity'], 'UNKNOWN')

    def test_overclaims_and_inconsistent_intents_are_rejected(self):
        import jsonschema
        schema = json.loads((ROOT / 'schemas/evidence_v1.schema.json').read_text(encoding='utf-8'))
        # (event, mutation, schema can express it). The pose/action mapping is a
        # cross-field rule enforced by the firmware and host validators only.
        mutations = [
            (decision('WAIT'), lambda e: e['result'].update(action_id=ACTION), True),     # WAIT creates no action
            (decision(), lambda e: e['result'].update(target_pose='BACK'), False),        # pose must match action
            (decision(), lambda e: e['result'].update(kind='TRAINED_MODEL'), True),       # never called trained AI
            (action_result(), lambda e: e['result']['capture'].update(presence='DETECTED'), True),
            (action_result(), lambda e: e.update(image_ref=None), True),                  # capture needs its image ref
            (action_result('FAILED'), lambda e: e['result'].update(status='COMPLETED'), True),
            (action_result('FAILED'), lambda e: e.update(image_ref=action_result()['image_ref']), True),
        ]
        for event, mutate, in_schema in mutations:
            changed = copy.deepcopy(event); mutate(changed)
            with self.assertRaises(ValueError): validate_event(changed)
            if in_schema:
                with self.assertRaises(jsonschema.ValidationError): jsonschema.validate(changed, schema)
        boot = envelope(1, 'BOOT', dict(scope='DEVICE_DIAGNOSTIC_ONLY'), observation_id=ACTION)
        with self.assertRaises(ValueError): validate_event(boot)


class ObservationPort(FakePort):
    """Stored ACK followed by the XIAO observation step lines (and an image)."""
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.observation_status = True
        self.trigger_override = None

    def write(self, wire):
        self.commands.append(wire)
        if wire == b'evidence-status\n':
            self.pending.extend(('EVIDENCE_STATUS ' + json.dumps(dict(
                writable=True, device_id=DEVICE, boot_id=BOOT, passive_candidates=0,
                **({'observation_policy': 'observation-policy-v1'} if self.observation_status else {}))) + '\n').encode())
            return len(wire)
        parts = wire.decode().split()
        trigger = f'{DEVICE}/{BOOT}/2'
        self.pending.extend(f'PASSIVE_ACK run={parts[1]} candidate={parts[2]} status={self.ack_status} event_id={trigger}\n'.encode())
        if self.ack_status == 'stored':
            sha = hashlib.sha256(PAYLOAD).hexdigest()
            lines = [
                'OBSERVATION_DECISION ' + json.dumps(dict(decision_event_id=f'{DEVICE}/{BOOT}/3', action='CAPTURE_FIXED',
                                                          action_id=ACTION, reason_codes=['PASSIVE_CANDIDATE_NEW'])),
                f'OBSERVATION_CAPTURE_BEGIN action_id={ACTION} pose=FIXED',
                'VISION_SAMPLE_REPLY ' + json.dumps(dict(type=1, name='SAMPLE', code=0, data=dict(
                    count=1, image=base64.b64encode(PAYLOAD).decode()))),
                'OBSERVATION_RESULT ' + json.dumps(dict(result_event_id=f'{DEVICE}/{BOOT}/4', action_id=ACTION,
                                                        status='COMPLETED', capture='SUCCESS', reason='none',
                                                        payload_sha256=sha, payload_bytes=len(PAYLOAD), stored=True)),
                f'OBSERVATION_DONE trigger={self.trigger_override or trigger} status=decided action=CAPTURE_FIXED '
                'reasons=["PASSIVE_CANDIDATE_NEW","FIXED_NOT_RECENTLY_OBSERVED"]',
            ]
            self.pending.extend(('\n'.join(lines) + '\n').encode())
        return len(wire)


class ObservationBridgeTests(unittest.TestCase):
    def test_stored_candidate_returns_one_observation_with_image(self):
        link = XiaoLink('MOCK_PORT', 'LIVE', serial_factory=ObservationPort)
        ack = link.send('b' * 32, 1, fixture_candidate(), 'c' * 64)
        observation = ack['observation']
        self.assertEqual(observation['done']['action'], 'CAPTURE_FIXED')
        self.assertEqual(observation['done']['reasons'][1], 'FIXED_NOT_RECENTLY_OBSERVED')
        self.assertEqual(base64.b64decode(observation['image_b64']), PAYLOAD)
        link.port.ack_status = 'duplicate'
        self.assertNotIn('observation', link.send('b' * 32, 1, fixture_candidate(), 'c' * 64))
        link.close()

    def test_answer_for_another_trigger_or_old_firmware(self):
        link = XiaoLink('MOCK_PORT', 'LIVE', serial_factory=ObservationPort)
        link.port.trigger_override = f'{DEVICE}/{BOOT}/99'
        with self.assertRaisesRegex(RuntimeError, 'different trigger'):
            link.send('b' * 32, 1, fixture_candidate(), 'c' * 64)
        class OldFirmware(ObservationPort):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.observation_status = False
        old = XiaoLink('MOCK_PORT', 'LIVE', serial_factory=OldFirmware)
        self.assertFalse(old.observation_supported)
        self.assertNotIn('observation', old.send('b' * 32, 1, fixture_candidate(), 'c' * 64))

    def test_drain_waits_for_a_whole_follow_up_episode(self):
        import manager, protocol
        worst = (protocol.ACK_TIMEOUT_S + protocol.OBSERVATION_TIMEOUT_S
                 + protocol.MAX_FOLLOW_UPS * protocol.FOLLOW_UP_TIMEOUT_S)
        self.assertGreater(manager.DELIVERY_DRAIN_TIMEOUT_S, worst)

    def test_armed_follow_up_is_read_for_the_same_trigger(self):
        trigger = f'{DEVICE}/{BOOT}/2'

        def step(action, pose, followup, seq, trig=trigger):
            act = f'obs-{BOOT}-{seq}'
            return [
                'OBSERVATION_DECISION ' + json.dumps(dict(decision_event_id=f'{DEVICE}/{BOOT}/{seq * 2 + 1}',
                                                          action=action, action_id=act, reason_codes=['EPISODE_FOLLOW_UP'])),
                f'OBSERVATION_MOVE_BEGIN action_id={act} pose={pose} target_us=1500',
                f'OBSERVATION_CAPTURE_BEGIN action_id={act} pose={pose}',
                'OBSERVATION_RESULT ' + json.dumps(dict(result_event_id=f'{DEVICE}/{BOOT}/{seq * 2 + 2}', action_id=act,
                                                        status='FAILED', capture='FAILED', reason='timeout',
                                                        payload_sha256=None, payload_bytes=0, stored=True)),
                f'OBSERVATION_DONE trigger={trig} status=decided action={action} followup={followup} '
                'reasons=["EPISODE_FOLLOW_UP"]',
            ]

        class ArmPort(ObservationPort):
            follow_trigger = trigger
            extra_armed = False

            def write(self, wire):
                n = super().write(wire)
                if wire.startswith(b'passive-candidate') and self.ack_status == 'stored':
                    text = bytes(self.pending).decode().replace(
                        'action=CAPTURE_FIXED reasons=', 'action=CAPTURE_FIXED followup=armed reasons=')
                    lines = step('CAPTURE_TOP', 'TOP', 'armed' if self.extra_armed else 'none', 5, self.follow_trigger)
                    self.pending[:] = (text + '\n'.join(lines) + '\n').encode()
                return n

        link = XiaoLink('MOCK_PORT', 'LIVE', serial_factory=ArmPort)
        ack = link.send('b' * 32, 1, fixture_candidate(), 'c' * 64)
        self.assertEqual(ack['observation']['done']['followup'], 'armed')
        self.assertEqual([f['done']['action'] for f in ack['follow_ups']], ['CAPTURE_TOP'])
        self.assertNotIn('tail', ack['observation'])
        link.close()
        # A follow-up for another trigger is a protocol error, not silently accepted.
        ArmPort.follow_trigger = f'{DEVICE}/{BOOT}/99'
        link = XiaoLink('MOCK_PORT', 'LIVE', serial_factory=ArmPort)
        with self.assertRaisesRegex(RuntimeError, 'different trigger'):
            link.send('b' * 32, 1, fixture_candidate(), 'c' * 64)
        link.close()
        # The manager counts Baseline B once per candidate, follow-ups add only real captures.
        with capture_fixture() as directory:
            manager = BridgeManager(directory)
            manager.state.update(baseline_b_captures=0, proposed_captures=0, proposed_waits=0)
            ack = dict(status='stored', event_id=trigger)
            follow = dict(decision=dict(action_id='obs-x-5'), image_b64=None,
                          result=dict(capture='FAILED'), done=dict(trigger=trigger, status='decided',
                                                                    action='CAPTURE_TOP', reasons=[]))
            summary = manager._record_observation(directory, 'd' * 32, 1, ack, follow, follow_up=True)
            self.assertTrue(summary['follow_up'])
            state = manager.snapshot()
            self.assertEqual((state['baseline_b_captures'], state['proposed_captures']), (0, 1))

    def test_arm_lines_are_noted_with_receipt_times_and_park_is_watched(self):
        act = f'obs-{BOOT}-5'

        class ParkPort(ObservationPort):
            def write(self, wire):
                n = super().write(wire)
                if wire.startswith(b'passive-candidate'):
                    text = bytes(self.pending).decode().replace(
                        f'OBSERVATION_CAPTURE_BEGIN action_id={ACTION} pose=FIXED',
                        f'OBSERVATION_MOVE_BEGIN action_id={act} pose=TOP target_us=1535\n'
                        f'OBSERVATION_CAPTURE_BEGIN action_id={act} pose=TOP')
                    self.pending[:] = (text + 'SERVO_PARK status=begin pose=FRONT target_us=890\n'
                                       'SERVO_PARK status=done pose=FRONT us=890 elapsed_ms=2100 (assumed)\n').encode()
                return n

        link = XiaoLink('MOCK_PORT', 'LIVE', serial_factory=ParkPort)
        ack = link.send('b' * 32, 1, fixture_candidate(), 'c' * 64)
        self.assertEqual(ack['observation']['done']['action'], 'CAPTURE_FIXED')  # step parsing unchanged
        self.assertEqual([a['kind'] for a in link.actuation], ['MOVE_BEGIN', 'CAPTURE_BEGIN'])
        self.assertEqual(link.actuation[0]['pose'], 'TOP')
        self.assertTrue(all(a['time_basis'] == 'PC_RECEIPT' and a['received_at_pc'] for a in link.actuation))
        self.assertTrue(link.watch_park(lambda: False, max_s=2))
        self.assertEqual([a['kind'] for a in link.actuation][2:], ['PARK_BEGIN', 'PARK_DONE'])
        self.assertEqual(link.actuation[3]['elapsed_ms'], '2100')
        # A waiting candidate is never delayed by the park watch.
        link.actuation.clear()
        link.send('b' * 32, 2, fixture_candidate(), 'c' * 64)
        import time
        started = time.monotonic()
        link._after_episode = b''
        link.port.pending.clear()
        self.assertFalse(link.watch_park(lambda: True, max_s=5))
        self.assertLess(time.monotonic() - started, .5)
        link.close()

    def test_image_saved_trimmed_and_verified_against_xiao_hash(self):
        with capture_fixture() as directory:
            encoded = base64.b64encode(PAYLOAD).decode()
            self.assertTrue(save_observation_image(directory, ACTION, encoded, hashlib.sha256(PAYLOAD).hexdigest()))
            saved = Path(directory) / 'observations' / f'{ACTION}.jpg'
            self.assertEqual(saved.read_bytes(), JPEG)
            meta = json.loads(saved.with_suffix('.json').read_text(encoding='utf-8'))
            self.assertEqual(meta['larva_presence'], 'UNKNOWN')
            self.assertFalse(save_observation_image(directory, ACTION, encoded, 'f' * 64))
            with self.assertRaises(ValueError):
                save_observation_image(directory, '../escape', encoded, 'f' * 64)

    def test_manager_counts_baseline_for_stored_candidates_only(self):
        with capture_fixture() as directory:
            manager = BridgeManager(directory)
            manager.state.update(baseline_b_captures=0, proposed_captures=0, proposed_waits=0)
            port = ObservationPort()
            port.write(b'evidence-status\n'); port.pending.clear()
            port.write(b'passive-candidate ' + b'b' * 32 + b' 1\n')
            from protocol import read_observation
            import time
            raw = bytes(port.pending)
            ack_line, _, rest = raw.partition(b'\n')
            observation = read_observation(port, time.monotonic() + 1, rest)
            ack = dict(status='stored', event_id=f'{DEVICE}/{BOOT}/2')
            summary = manager._record_observation(directory, 'd' * 32, 1, ack, observation)
            self.assertTrue(summary['image_verified'])
            self.assertEqual(summary['baseline_b_action'], 'CAPTURE_FIXED')
            # Per-step PC receipt times place each frame on the review time axis.
            self.assertTrue(summary['frame_received_at_pc'] and summary['result_received_at_pc'])
            manager._record_observation(directory, 'd' * 32, 1, dict(status='duplicate'), None)
            # A capture plan whose decision could not be persisted is not a capture.
            failed = dict(decision=None, result=None, image_b64=None,
                          done=dict(trigger=f'{DEVICE}/{BOOT}/5', status='persist_failed',
                                    action='CAPTURE_FIXED', reasons=['PASSIVE_CANDIDATE_NEW']))
            manager._record_observation(directory, 'd' * 32, 2, ack, failed)
            state = manager.snapshot()
            self.assertEqual((state['baseline_b_captures'], state['proposed_captures']), (2, 1))
            self.assertEqual(state['proposed_waits'], 0)
            self.assertEqual(state['observations'][0]['larva_presence'], 'UNKNOWN')
            # Arm lines for the live demo view: finished ones plus the list the link is filling.
            manager.arm.append(dict(kind='PARK_DONE', candidate_seq=1))
            manager.live_arm = [dict(kind='MOVE_BEGIN', pose='TOP')]
            state = manager.snapshot()
            self.assertEqual([a['kind'] for a in state['arm']], ['PARK_DONE'])
            self.assertEqual(state['arm_live'], [dict(kind='MOVE_BEGIN', pose='TOP')])
            state['arm_live'].append('x')  # a copy, never the link's own list
            self.assertEqual(len(manager.live_arm), 1)


MODEL_INFO = dict(name='larva_visibility_int8_vela', version='1.4.0',
                  classes=['NO_VISIBLE_LARVA', 'VISIBLE_LARVA'])


def inspect_lines(info=MODEL_INFO):
    encoded = base64.b64encode(json.dumps(info).encode()).decode()
    return ['VISION_QUERY INFO?',
            'VISION_REPLY ' + json.dumps(dict(type=0, name='INFO?', code=0, data=dict(info=encoded))),
            'VISION_QUERY_DONE name=INFO? status=ok elapsed_ms=12', 'VISION_INSPECT_DONE']


class InvokePort(ObservationPort):
    """0.14.0: the capture is an INVOKE event with the same frame and raw classes."""
    classes = [[87, 1]]
    inspect = None

    def write(self, wire):
        if wire == b'vision\n':
            self.commands.append(wire)
            self.pending.extend(('\n'.join(self.inspect or inspect_lines()) + '\n').encode())
            return len(wire)
        n = super().write(wire)
        if wire.startswith(b'passive-candidate') and self.ack_status == 'stored':
            text = bytes(self.pending).decode()
            sample = next(line for line in text.split('\n') if line.startswith('VISION_SAMPLE_REPLY '))
            event = json.loads(sample[len('VISION_SAMPLE_REPLY '):])
            event.update(name='INVOKE')
            event['data'].update(classes=self.classes, perf=[9, 24, 0])
            invoke = '\n'.join([
                'VISION_CLASSIFY_REPLY ' + json.dumps(dict(type=0, name='INVOKE', code=0, data=dict())),
                'VISION_CLASSIFY_REPLY ' + json.dumps(event)])
            self.pending[:] = text.replace(sample, invoke).encode()
        return n


class VisionModelReferenceTests(unittest.TestCase):
    def test_model_check_reads_info_and_never_raises_on_unknown_models(self):
        link = XiaoLink('MOCK_PORT', 'LIVE', serial_factory=InvokePort)
        check = link.read_vision_model()
        self.assertEqual(check['status'], 'VERIFIED')
        self.assertEqual((check['model']['version'], check['model']['recipe']), ('1.4.0', 'v12-192'))
        self.assertTrue(check['model']['artifact_sha256'].startswith('12bebc7a'))
        link.port.inspect = inspect_lines(dict(MODEL_INFO, version='1.5.0'))
        check = link.read_vision_model()
        self.assertEqual((check['model']['recipe'], check['model']['artifact_sha256'][:8]), ('v13b-96', 'ae9a10ed'))
        from vision_model import reference_output
        # v13b trained on the arm views; older models only on the fixed camera.
        self.assertEqual(reference_output(check, dict(classes=[[87, 1]]), 'TOP')['view_vs_training'], 'IN_TRAINING_VIEW')
        link.port.inspect = inspect_lines(dict(MODEL_INFO, version='9.9.9'))
        self.assertEqual(link.read_vision_model()['status'], 'UNVERIFIED')
        link.port.inspect = ['VISION_UNAVAILABLE reason=no_ack address=0x62 label=unknown', 'VISION_INSPECT_DONE']
        self.assertEqual(link.read_vision_model()['reason'], 'vision_unavailable')
        link.close()

    def test_invoke_frame_and_classes_reach_the_summary_and_sidecar(self):
        link = XiaoLink('MOCK_PORT', 'LIVE', serial_factory=InvokePort)
        check = link.read_vision_model()
        ack = link.send('b' * 32, 1, fixture_candidate(), 'c' * 64)
        observation = ack['observation']
        self.assertEqual(base64.b64decode(observation['image_b64']), PAYLOAD)
        self.assertEqual(observation['model_raw'], dict(classes=[[87, 1]], perf=[9, 24, 0]))
        link.close()
        with capture_fixture() as directory:
            manager = BridgeManager(directory)
            manager.state.update(baseline_b_captures=0, proposed_captures=0, proposed_waits=0)
            manager.vision_model = check
            observation['decision']['target_pose'] = 'TOP'
            summary = manager._record_observation(directory, 'd' * 32, 1, dict(status='stored'), observation)
            ai = summary['vision_ai']
            self.assertTrue(summary['image_verified'])
            self.assertEqual(ai['prediction']['top']['class_name'], 'VISIBLE_LARVA')
            self.assertEqual((ai['pose'], ai['view_vs_training']), ('TOP', 'OUTSIDE_TRAINING_VIEWS'))
            self.assertEqual((ai['larva_presence'], summary['larva_presence']), ('UNKNOWN', 'UNKNOWN'))
            self.assertTrue(ai['reference_only'])
            meta = json.loads((Path(directory) / 'observations' / f'{ACTION}.json').read_text(encoding='utf-8'))
            self.assertEqual(meta['vision_model_output']['raw_classes_score_target'], [[87, 1]])
            self.assertEqual(meta['larva_presence'], 'UNKNOWN')

    def test_unverified_model_output_is_kept_raw_but_not_named(self):
        from vision_model import reference_output
        for check in (None, dict(status='UNVERIFIED', reason='inspect_timeout', model=None)):
            ai = reference_output(check, dict(classes=[[87, 1]], perf=None), 'FRONT')
            self.assertEqual(ai['prediction']['status'], 'MODEL_NOT_VERIFIED')
            self.assertIsNone(ai['model'])
            self.assertEqual(ai['raw_classes_score_target'], [[87, 1]])
            self.assertEqual(ai['view_vs_training'], 'NEAR_TRAINING_VIEW')
        # SAMPLE captures (older firmware or observe-vision sample) carry no model output.
        with capture_fixture() as directory:
            manager = BridgeManager(directory)
            manager.state.update(baseline_b_captures=0, proposed_captures=0, proposed_waits=0)
            port = ObservationPort()
            port.write(b'evidence-status\n'); port.pending.clear()
            port.write(b'passive-candidate ' + b'b' * 32 + b' 1\n')
            from protocol import read_observation
            import time
            _, _, rest = bytes(port.pending).partition(b'\n')
            observation = read_observation(port, time.monotonic() + 1, rest)
            self.assertIsNone(observation['model_raw'])
            summary = manager._record_observation(directory, 'd' * 32, 1, dict(status='stored'), observation)
            self.assertIsNone(summary['vision_ai'])


if __name__ == '__main__':
    unittest.main()
