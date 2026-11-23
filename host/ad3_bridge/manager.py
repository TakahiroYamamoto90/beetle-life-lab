"""Bounded live acquisition, independent serial delivery, and PC-owned raw data."""
import base64
from collections import deque
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import queue
import re
import shutil
import threading
import time
import uuid

import numpy as np

from acquisition import WaveForms, SAMPLE_RATE
from obs_record import ObsRecorder, collect_recording
from processing import Processor
from protocol import XiaoLink, read_evidence_snapshot, MAX_DELIVERY_S
import lowfreq_link

# After the AD3 stops, an observation episode already running on the XIAO (up to four
# arm views, about 25 s on 2026-11-10) must finish before the port closes; closing it
# under the sender lost the images and raised inside pyserial (2026-11-10, 15 s limit).
DELIVERY_DRAIN_TIMEOUT_S = MAX_DELIVERY_S + 5
from time_sync import evidence_times, ad3_candidate_time
from evidence_client import validate_event, reduce_events  # on sys.path via protocol
import candidate_context
import activity_trigger
import disturbance
from vision_model import reference_output


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# Operator labels typed during a capture (button press). PC-only: never sent to the XIAO and
# never part of detection; the time is the sample clock when the press reached the bridge.
ANNOTATION_KINDS = ('DIG_START', 'DIG_END', 'NOISE')


def save_observation_image(directory, action_id, encoded, expected_sha256, model_output=None):
    """Store the frame the XIAO streamed for one action; verify it against the XIAO hash."""
    if not re.fullmatch(r'obs-[0-9a-f]{32}-[1-9][0-9]{0,9}', action_id or ''):
        raise ValueError('Invalid observation action ID')
    payload = base64.b64decode(encoded, validate=True)
    sha256 = hashlib.sha256(payload).hexdigest()
    image = payload
    # Vision V2 returns a zero-padded buffer; trim only verified zeros after JPEG EOI.
    if not image.endswith(b'\xff\xd9'):
        end = image.rfind(b'\xff\xd9')
        if end >= 0 and not any(image[end + 2:]):
            image = image[:end + 2]
    folder = Path(directory) / 'observations'
    folder.mkdir(exist_ok=True)
    jpeg_ok = image.startswith(b'\xff\xd8') and image.endswith(b'\xff\xd9')
    verified = sha256 == expected_sha256 and jpeg_ok
    (folder / f'{action_id}.jpg').write_bytes(image)
    metadata = dict(schema='bll.observation_image.v1', action_id=action_id, received_at_pc=utc_now(),
                    transport_sha256=sha256, xiao_payload_sha256=expected_sha256,
                    hash_matches_xiao=sha256 == expected_sha256, jpeg_markers_ok=jpeg_ok,
                    image_sha256=hashlib.sha256(image).hexdigest(), bytes=len(image),
                    larva_presence='UNKNOWN', presence_reason='LARVA_MODEL_UNVERIFIED')
    if model_output is not None:
        metadata['vision_model_output'] = model_output  # reference only, separate from any label
    (folder / f'{action_id}.json').write_text(json.dumps(metadata, indent=2) + '\n', encoding='utf-8')
    return verified


class BridgeManager:
    def __init__(self, output, source_factory=WaveForms, link_factory=XiaoLink,
                 evidence_reader=read_evidence_snapshot, video_factory=ObsRecorder,
                 video_collector=collect_recording, collection_importer=None):
        self.output = Path(output)
        # callable(run_dir, request) -> report; None (tests, default) never touches the collection.
        self.collection_importer = collection_importer
        self.video_factory = video_factory
        self.video_collector = video_collector
        self.source_factory = source_factory
        self.link_factory = link_factory
        self.evidence_reader = evidence_reader
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.reading_evidence = False
        self.points = deque(maxlen=2000)
        self.features = deque(maxlen=1500)
        # observation trigger per 250 ms window: [t, forest_p, ratio, active, masked] (2026-11-23)
        self.trigger_windows = deque(maxlen=2400)
        self.candidates = deque(maxlen=100)
        self.observations = deque(maxlen=100)
        # Arm lines for the live demo view (/demo LIVE): finished episodes, and the list the XIAO
        # link is filling right now (PC receipt order). Display only; actuation.jsonl stays the record.
        self.arm = deque(maxlen=60)
        self.live_arm = None
        self.annotations = deque(maxlen=500)
        self.annotation_path = None
        self.vision_model = None  # model check of the current run (read at link open)
        self.state = dict(state='idle', running=False, origin=None, error=None)

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def snapshot(self):
        with self.lock:
            return dict(self.state, points=list(self.points), features=list(self.features),
                        trigger_windows=list(self.trigger_windows),
                        candidates=list(self.candidates), observations=list(self.observations),
                        arm=list(self.arm), arm_live=list(self.live_arm or []),
                        annotations=list(self.annotations), reading_evidence=self.reading_evidence)

    def read_evidence(self, port, synchronize=False):
        if not isinstance(port, str) or not port or len(port) > 256:
            raise ValueError('MCUのポートを選択してください')
        with self.lock:
            if self.state['running'] or self.reading_evidence:
                raise BlockingIOError('取得を停止してからMCUの保存ログを読み出してください')
            self.reading_evidence = True
        try:
            sync_result = None
            if synchronize:
                link = self.link_factory(port, 'LIVE')
                try:
                    sync_result = link.synchronize_clock()
                finally:
                    link.close()
            state, events, mirror = self.evidence_reader(port)
            return dict(device_state=state, events=events, host_mirror=mirror,
                        received_at_pc=utc_now(), delivery_mode='SYNC', event_times=evidence_times(events),
                        ad3_times=self._ad3_times(events), clock_sync=sync_result)
        finally:
            with self.lock:
                self.reading_evidence = False

    def _ad3_times(self, events):
        source_times = {}
        for event in events:
            if event['event_type'] != 'PASSIVE_VIBRATION_CANDIDATE': continue
            run = event['result']['source_run_id']
            manifest_path = self.output / ('ad3-' + run) / 'manifest.json'
            if not manifest_path.is_file(): continue
            try:
                value = ad3_candidate_time(event, json.loads(manifest_path.read_text(encoding='utf-8')))
                if value: source_times[event['event_id']] = value
            except (OSError, ValueError, KeyError, TypeError):
                continue  # Missing source timing is unknown, not MCU receipt time.
        return source_times

    # ---- saved Evidence exports (PC copies; read-only, never sent back to the XIAO) ----
    def _export_folders(self):
        root = self.output.parent
        return root, [root / 'evidence', root / 'flash_backup']

    def saved_evidence_list(self):
        """Exports written by evidence_client (captures/evidence, captures/flash_backup), newest first."""
        root, folders = self._export_folders()
        rows = []
        for folder in folders:
            if not folder.is_dir():
                continue
            for path in folder.rglob('evidence-*.jsonl'):
                meta = {}
                try:
                    if path.with_suffix('.json').is_file():
                        meta = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    meta = {}
                meta = meta if isinstance(meta, dict) else {}
                state = meta.get('device_state') if isinstance(meta.get('device_state'), dict) else {}
                rows.append(dict(name=path.relative_to(root).as_posix(), received_at_pc=meta.get('received_at_pc'),
                                 device_id=state.get('device_id'), events=meta.get('log_events', state.get('events')),
                                 storage_reason=state.get('storage_reason'),
                                 verbatim=meta.get('events_file_format') == 'DEVICE_LINES_VERBATIM',
                                 rotated=bool(meta.get('rotation')), modified_s=path.stat().st_mtime))
        return sorted(rows, key=lambda r: (r['received_at_pc'] or '', r['modified_s']), reverse=True)

    def read_saved_evidence(self, name):
        """One saved export in the same shape as read_evidence, re-validated event by event."""
        if not isinstance(name, str) or name not in {r['name'] for r in self.saved_evidence_list()}:
            raise LookupError('保存済みの書き出しが見つかりません')
        root, _ = self._export_folders()
        path = root / name
        data = path.read_bytes()
        events = [validate_event(json.loads(line)) for line in data.decode('utf-8').splitlines() if line.strip()]
        mirror = reduce_events(events)  # also rejects conflicting duplicate event IDs
        meta = {}
        if path.with_suffix('.json').is_file():
            meta = json.loads(path.with_suffix('.json').read_text(encoding='utf-8'))
        expected = meta.get('log_sha256')
        rotation = meta.get('rotation') or {}
        return dict(device_state=meta.get('device_state'), events=events, host_mirror=mirror,
                    received_at_pc=meta.get('received_at_pc'), delivery_mode='SAVED_EXPORT',
                    event_times=evidence_times(events), ad3_times=self._ad3_times(events), clock_sync=None,
                    saved_export=dict(name=name, verbatim=meta.get('events_file_format') == 'DEVICE_LINES_VERBATIM',
                                      # None: older exports were re-serialized and carry no hash to compare
                                      hash_verified=(hashlib.sha256(data).hexdigest() == expected) if expected else None,
                                      rotated_after=bool(rotation), rotation_response=rotation.get('response')))

    def start(self, request):
        if request.get('mode') not in {'live', 'mock'}:
            raise ValueError('mode must be live or mock')
        if type(request.get('duration_s')) is not int or not 1 <= request['duration_s'] <= 1800:
            raise ValueError('duration_s must be 1..1800')
        live = request['mode'] == 'live'
        forward = request.get('forward', False)
        if type(forward) is not bool or (forward and not live):
            raise ValueError('Mock/replay forwarding is prohibited')
        port = request.get('port', '')
        if type(request.get('video', False)) is not bool:
            raise ValueError('video must be true or false')
        if forward and (not isinstance(port, str) or not port):
            raise ValueError('XIAO port is required for forwarding')
        index, range_v = request.get('device_index', 0), request.get('range_v', 5)
        if type(index) is not int or index < 0 or type(range_v) not in (int, float) or range_v not in (.5, 1, 2, 5, 10):
            raise ValueError('Invalid AD3 device or voltage range')
        if live and request.get('wiring_confirmed') is not True:
            raise ValueError('Confirm CH2 piezo input, ground, range and Wavegen disconnected')
        for key in ('case_id', 'session_id', 'note', 'illumination'):
            value = request.get(key, '')
            if not isinstance(value, str) or len(value) > (500 if key == 'note' else 128):
                raise ValueError('Invalid capture context: ' + key)
        with self.lock:
            if self.reading_evidence:
                raise BlockingIOError('MCUの保存ログを読み出し中です')
            if self.thread and self.thread.is_alive():
                raise BlockingIOError('AD3 acquisition already active')
            self.output.mkdir(parents=True, exist_ok=True)
            needed = request['duration_s'] * SAMPLE_RATE * 16 + 100 * 1024 * 1024
            if shutil.disk_usage(self.output).free < needed:
                raise ValueError('Insufficient disk space for raw capture plus reserve')
            self.stop_event.clear()
            self.points.clear(); self.features.clear(); self.trigger_windows.clear(); self.candidates.clear(); self.observations.clear()
            self.arm.clear(); self.live_arm = None
            self.annotations.clear(); self.annotation_path = None
            self.vision_model = None
            self.state = dict(state='starting', running=True, origin='LIVE' if live else 'SYNTHETIC',
                              run_id=uuid.uuid4().hex, error=None, samples=0, forwarded=0,
                              candidate_count=0, detector_state='WARMUP', detector_resets=0, forwarding=forward,
                              current_activity='UNKNOWN', time_quality='UNSYNCED',
                              observation_supported=None, baseline_b_captures=0,
                              proposed_captures=0, proposed_waits=0, vision_model=None,
                              video=dict(status='REQUESTED' if request.get('video') else 'OFF'))
            self.thread = threading.Thread(target=self._guarded_run, args=(dict(request), dict(self.state)), daemon=True)
            self.thread.start()
            return dict(self.state)

    def annotate(self, kind):
        if kind not in ANNOTATION_KINDS:
            raise ValueError('kind must be one of ' + ', '.join(ANNOTATION_KINDS))
        with self.lock:
            if not self.state.get('running') or self.state.get('state') != 'capturing' or not self.annotation_path:
                raise BlockingIOError('取得中のみ記録できます')
            samples = self.state.get('samples', 0)
            record = dict(schema='bll.ad3_annotation.v1', seq=len(self.annotations) + 1, kind=kind,
                          t_s=samples / SAMPLE_RATE, samples=samples, received_at_pc=utc_now(),
                          source='HUMAN_OPERATOR', origin=self.state.get('origin'),
                          time_basis='SAMPLES_RECEIVED_AT_PRESS_APPROXIMATE')
            with self.annotation_path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(record) + '\n')
            self.annotations.append(record)
            return record

    def stop(self):
        self.stop_event.set()
        return self.snapshot()

    def _guarded_run(self, request, initial):
        try:
            self._run(request, initial)
        except BaseException as exc:
            self.update(state='error', running=False, error=f'{type(exc).__name__}: {exc}')

    def _record_observation(self, directory, run_id, seq, ack, observation, follow_up=False):
        """Display/log summary of one XIAO decision. The XIAO evidence log stays the source of truth."""
        if ack.get('status') != 'stored':
            return None  # a duplicate never triggers a new decision on the XIAO
        if not follow_up:
            with self.lock:
                # Baseline B (same hardware, no memory): one capture for every stored candidate.
                self.state['baseline_b_captures'] += 1
        if observation is None:
            return None
        decision, result, done = observation['decision'], observation['result'], observation['done']
        summary = dict(candidate_seq=seq, trigger_event_id=done.get('trigger'), status=done.get('status'),
                       action=done.get('action'), reason_codes=done.get('reasons', []),
                       decision_event_id=(decision or {}).get('decision_event_id'),
                       action_id=(decision or {}).get('action_id'), result=result,
                       baseline_b_action='CAPTURE_FIXED', image_url=None, image_verified=None,
                       received_at_pc=utc_now(), larva_presence='UNKNOWN', follow_up=follow_up,
                       target_pose=(decision or {}).get('target_pose'), vision_ai=None,
                       frame_received_at_pc=observation.get('frame_received_at_pc'),
                       result_received_at_pc=observation.get('result_received_at_pc'))
        if observation['image_b64'] and result and result.get('capture') == 'SUCCESS':
            raw = observation.get('model_raw')
            if raw is not None:
                summary['vision_ai'] = reference_output(self.vision_model, raw, summary['target_pose'])
            summary['image_verified'] = save_observation_image(
                directory, summary['action_id'], observation['image_b64'], result.get('payload_sha256'),
                summary['vision_ai'])
            summary['image_url'] = f"/files/ad3-{run_id}/observations/{summary['action_id']}.jpg"
        with self.lock:
            # Count what the XIAO actually did, not what it planned (a plan may fail to persist).
            if summary['action'] == 'WAIT' and summary['status'] == 'decided':
                self.state['proposed_waits'] += 1
            elif result and result.get('capture') in {'SUCCESS', 'FAILED'}:
                self.state['proposed_captures'] += 1
            self.observations.append(summary)
        return summary

    def _mock_blocks(self, stop, duration):
        # Deterministic artificial signal, intentionally distinct in every artifact.
        rng = np.random.default_rng(20261031)
        for start in range(0, duration * SAMPLE_RATE, 5000):
            if stop.is_set():
                break
            count = min(5000, duration * SAMPLE_RATE - start)
            t = (start + np.arange(count)) / SAMPLE_RATE
            envelope = np.where((t >= 7) & (t < 9), .004, .001)
            piezo = envelope * np.sin(2 * np.pi * 700 * t) + rng.normal(0, .0001, count)
            # 12-14 s: also a 10 Hz burst, so the 1-40 Hz observation trigger fires once its
            # 10 s floor exists (activity_trigger.py, 2026-11-23)
            low = (t >= 12) & (t < 14)
            piezo = piezo + np.where(low, .004 * np.sin(2 * np.pi * 10 * t) + .003 * np.sin(2 * np.pi * 700 * t), 0)
            yield np.column_stack((np.zeros(count), piezo))
            stop.wait(count / SAMPLE_RATE)

    def _run(self, request, initial):
        run_id, origin = initial['run_id'], initial['origin']
        directory = self.output / ('ad3-' + run_id)
        directory.mkdir()
        source = link = sender = recorder = None
        failure = []
        abort_delivery = threading.Event()
        pending = queue.Queue(maxsize=16)
        count = number = b2_number = 0
        trigger = None  # observation trigger (activity_trigger.py), set with the processor
        manifest = dict(schema='bll.ad3_capture.v1', origin=origin, run_id=run_id,
                        started_at_pc=utc_now(), request=request, complete=False,
                        raw_format=dict(file='samples.f64', dtype='<f8', shape=['samples', 2],
                                        channels=['CH1_V', 'CH2_V'], sample_rate_hz=SAMPLE_RATE),
                        time_note='PC times describe receipt. Raw elapsed time is the nominal AD3 sample clock, not UTC.',
                        biological_activity='UNKNOWN', clipping_verified=False)
        manifest['bridge_code_sha256'] = {
            name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
            for name in ('acquisition.py', 'processing.py', 'protocol.py', 'manager.py', 'obs_record.py', 'candidate_context.py',
                         'disturbance.py', 'lowfreq_activity.py', 'lowfreq_link.py', 'lowfreq_forest.py',
                         'activity_trigger.py')}
        vision_model_py = Path(__file__).resolve().parents[2] / 'scripts' / 'vision_model.py'
        manifest['bridge_code_sha256']['vision_model.py'] = hashlib.sha256(vision_model_py.read_bytes()).hexdigest()
        manifest_path = directory / 'manifest.json'

        def save_manifest():
            temp = directory / 'manifest.tmp'
            temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            temp.replace(manifest_path)

        low = None  # XIAO low-frequency forest feed (reference only; set when forwarding)
        last_seq = [None]  # the last acknowledged candidate (arm lines read while idle belong to its episode)

        def write_arm(seq):
            noted = getattr(link, 'actuation', None)
            if noted:
                with (directory / 'actuation.jsonl').open('a', encoding='utf-8') as arm:
                    for a in noted:
                        arm.write(json.dumps(dict(a, candidate_seq=seq), ensure_ascii=False) + '\n')
                with self.lock:  # one step, so a snapshot never lists a line twice
                    self.arm.extend(dict(a, candidate_seq=seq) for a in noted)
                    noted.clear()

        def deliver():
            try:
                with (directory / 'delivery.jsonl').open('x', encoding='utf-8') as log:
                    while True:
                        try:
                            item = pending.get(timeout=.25)
                        except queue.Empty:
                            # idle: low-frequency windows for the XIAO's forest (never delays a candidate)
                            if low:
                                low.flush(link)
                                # a park end read meanwhile (no later candidate would write it, 2026-11-21)
                                write_arm(last_seq[0])
                            continue
                        try:
                            if item is None:
                                if low and not abort_delivery.is_set():
                                    low.flush(link, final=True)
                                write_arm(last_seq[0])
                                return
                            seq, event, fingerprint = item
                            if abort_delivery.is_set():
                                log.write(json.dumps(dict(candidate_seq=seq, status='not_sent_capture_failed')) + '\n')
                                log.flush()
                                continue
                            log.write(json.dumps(dict(candidate_seq=seq, status='sending', received_at_pc=utc_now())) + '\n')
                            log.flush()
                            self.update(delivering=seq)
                            trigger.busy_begin(self.snapshot().get('elapsed_s') or 0.0)
                            ack = link.send(run_id, seq, event, fingerprint)
                            observation = ack.pop('observation', None)
                            follow_ups = ack.pop('follow_ups', [])
                            summary = self._record_observation(directory, run_id, seq, ack, observation)
                            later = [self._record_observation(directory, run_id, seq, ack, step, follow_up=True)
                                     for step in follow_ups]
                            log.write(json.dumps(dict(candidate_seq=seq, status='acknowledged', ack=ack,
                                                      observation=summary, follow_ups=later,
                                                      received_at_pc=utc_now())) + '\n')
                            log.flush()
                            noted = getattr(link, 'actuation', None)
                            if noted is not None:
                                # Arm moves of this episode; then its park, unless a candidate is waiting.
                                if any(a['kind'] == 'MOVE_BEGIN' for a in noted) and hasattr(link, 'watch_park'):
                                    link.watch_park(lambda: not pending.empty() or abort_delivery.is_set())
                                write_arm(seq)
                            last_seq[0] = seq
                            trigger.busy_end(self.snapshot().get('elapsed_s') or 0.0)
                            with self.lock:
                                self.state['forwarded'] += 1
                                self.state['delivering'] = None
                        finally:
                            pending.task_done()
            except BaseException as exc:
                failure.append(f'{type(exc).__name__}: {exc}')
                abort_delivery.set()
                self.stop_event.set()

        save_manifest()
        try:
            processor = Processor()
            # 2026-11-23 (user decision): the observation trigger is the start of a 1-40 Hz activity
            # span (forest or ratio >= 3); B2 still runs and is saved to b2_candidates.jsonl only.
            trigger = activity_trigger.ActivityTrigger()
            self.update(trigger_threshold=trigger.model['threshold'], trigger_ratio_min=activity_trigger.RATIO_MIN)
            manifest['observation_trigger'] = dict(detector=activity_trigger.DETECTOR, config_sha256=trigger.fingerprint,
                                                   config=trigger.config, note=activity_trigger.NOTE,
                                                   b2='b2_candidates.jsonl (comparison only, not sent)')
            bands = disturbance.BandTracker()  # PC-only reference numbers; never detector input
            manifest['processing'] = processor.metadata
            manifest['config_sha256'] = processor.fingerprint
            manifest['detector_resets'] = processor.resets  # same list; saved with the manifest
            if request.get('forward'):
                link = self.link_factory(request['port'], origin)
                self.update(observation_supported=getattr(link, 'observation_supported', False))
                with self.lock:
                    self.live_arm = getattr(link, 'actuation', None)
                if link.state.get('clock_sync_supported'):
                    manifest['xiao_clock_sync'] = link.synchronize_clock()
                    self.update(time_quality='ESTIMATED')
                manifest['xiao_initial_state'] = link.state
                if hasattr(link, 'lowfreq'):
                    low = lowfreq_link.LowFreqForwarder(run_id, directory, SAMPLE_RATE)
                if getattr(link, 'observation_supported', False) and hasattr(link, 'read_vision_model'):
                    # Identify the deployed Vision model once, before any candidate; its
                    # outputs are labelled with this check (reference only).
                    self.vision_model = link.read_vision_model()
                    manifest['vision_model'] = self.vision_model
                    self.update(vision_model=self.vision_model)
            if request.get('video'):
                # Video is a PC-only review aid: if OBS is unavailable the capture still runs.
                try:
                    recorder = self.video_factory()
                    manifest['video'] = recorder.start()
                except Exception as exc:
                    recorder = None
                    manifest['video'] = dict(status='UNAVAILABLE', reason=f'{type(exc).__name__}: {exc}')
                manifest['video'].update(source='OBS_STUDIO_WEBSOCKET', use='HUMAN_REVIEW_ONLY',
                                         note='Not detector input and not sent to the XIAO. PC receipt times; '
                                              'use a sync tap to align video with the sample clock.')
                self.update(video=dict(manifest['video']))
            if origin == 'LIVE':
                source = self.source_factory()
                manifest['acquisition'] = source.start(request.get('device_index', 0), request.get('range_v', 5), self.stop_event)
                blocks = source.blocks(self.stop_event)
            else:
                manifest['acquisition'] = dict(source='DETERMINISTIC_MOCK', sample_rate_hz=SAMPLE_RATE)
                blocks = self._mock_blocks(self.stop_event, request['duration_s'])
            save_manifest()
            if link:
                sender = threading.Thread(target=deliver, daemon=True)
                sender.start()
            with self.lock:
                self.annotation_path = directory / 'annotations.jsonl'
                self.state['state'] = 'capturing'
            maximum = request['duration_s'] * SAMPLE_RATE
            last_arrival = time.monotonic()
            with (directory / 'samples.f64').open('xb') as raw, \
                    (directory / 'features.csv').open('x', newline='', encoding='utf-8') as feature_file, \
                    (directory / 'candidates.jsonl').open('x', encoding='utf-8') as candidates, \
                    (directory / 'b2_candidates.jsonl').open('x', encoding='utf-8') as b2_file, \
                    (directory / 'trigger_windows.csv').open('x', newline='', encoding='utf-8') as trigger_file:
                trigger_writer = csv.writer(trigger_file)
                trigger_writer.writerow(['t_center_s', 'forest_p', 'ratio', 'active', 'masked'])
                writer = csv.DictWriter(feature_file, fieldnames=['t_center_s', 'rms_V', 'smoothed_V',
                                        'baseline_V', 'score', 'state', 'reason', 'ch1_rms_V'])
                writer.writeheader()
                for values in blocks:
                    if self.stop_event.is_set():
                        break
                    if count >= maximum:
                        break
                    if time.monotonic() - last_arrival > 5:
                        raise RuntimeError('HOST_PROCESSING_STALL')
                    last_arrival = time.monotonic()
                    values = values[:maximum - count]
                    if count == 0 and recorder:
                        first = time.monotonic()
                        manifest['video'].update(
                            first_block_received_at_pc=utc_now(),
                            first_block_samples=len(values),
                            # Rough: video was already running for this long when the first AD3
                            # block (ending at len/SAMPLE_RATE on the sample clock) reached the PC.
                            recording_s_at_first_block=round(first - manifest['video']['start_ack_monotonic_s'], 4))
                    raw.write(np.asarray(values, dtype='<f8').tobytes(order='C'))
                    first_index = count
                    count += len(values)  # raw samples remain accounted for if detection faults
                    rows, b2_found = processor.push(values, first_index)
                    for event in b2_found:  # comparison only (2026-11-23): never sent to the MCU
                        b2_number += 1
                        b2_file.write(json.dumps(dict(event, b2_seq=b2_number, origin=origin, source_run_id=run_id,
                                                      config_sha256=processor.fingerprint, forwarded=False,
                                                      biological_activity='UNKNOWN'), allow_nan=False) + '\n')
                        b2_file.flush()
                    judged = len(trigger.windows)
                    found = trigger.push(values)
                    fresh = [[round(t, 3), None if p is None else round(p, 4), None if r is None else round(r, 4), int(a), int(m)]
                             for t, p, r, a, m in trigger.windows[judged:]]
                    if fresh:
                        trigger_writer.writerows([['' if v is None else v for v in row] for row in fresh])
                        with self.lock:
                            self.trigger_windows.extend(fresh)
                    if bands is not None:
                        try:
                            bands.push(values)
                        except Exception as exc:  # reference numbers only: never stop the capture
                            manifest['disturbance_error'] = f'{type(exc).__name__}: {exc}'
                            bands = None
                    if low is not None:
                        try:
                            low.push(values, count)
                        except Exception as exc:  # reference only: never stop the capture
                            manifest['xiao_lowfreq_error'] = f'{type(exc).__name__}: {exc}'
                            low.state = 'FAILED'
                    # Envelope bins preserve taps that plain point subsampling would miss.
                    bins = []
                    for offset in range(0, len(values), 500):
                        part = values[offset:offset + 500, 1]
                        bins.append([(first_index + offset) / SAMPLE_RATE, float(part.min()), float(part.max())])
                    writer.writerows(rows)
                    with self.lock:
                        self.points.extend(bins)
                        self.features.extend(rows)
                        self.state.update(samples=count, elapsed_s=count / SAMPLE_RATE,
                                          detector_state=trigger.state, b2_state=processor.detector.state,
                                          detector_resets=len(processor.resets))
                    for event in found:
                        number += 1
                        record = dict(event, candidate_seq=number, origin=origin, source_run_id=run_id,
                                      config_sha256=trigger.fingerprint, biological_activity='UNKNOWN')
                        candidates.write(json.dumps(record, allow_nan=False) + '\n')
                        candidates.flush()
                        with self.lock:
                            recent = list(self.features)
                        # PC-only reference numbers in their own file; candidates.jsonl and the
                        # XIAO payload stay exactly as the detector produced them.
                        context = None
                        try:
                            if recent:
                                arrays = candidate_context.rows_to_arrays(recent)
                                others = [(c['t_start_s'], c['t_end_s']) for c in list(self.candidates)]
                                context = candidate_context.band_ratios(*arrays, event['t_start_s'], event['t_end_s'], others)
                                if bands is not None:
                                    context['disturbance'] = disturbance.judge(
                                        bands.arrays(), arrays[:2], event['t_start_s'], event['t_end_s'], others)
                                with (directory / 'candidate_context.jsonl').open('a', encoding='utf-8') as stream:
                                    stream.write(json.dumps(dict(context, candidate_seq=number,
                                                                 schema=candidate_context.SCHEMA,
                                                                 note=candidate_context.NOTE), allow_nan=False) + '\n')
                        except (OSError, ValueError, KeyError) as exc:
                            context = dict(error=f'{type(exc).__name__}: {exc}')
                        with self.lock:
                            self.candidates.append(dict(record, context=context))
                            self.state['candidate_count'] = number
                        if event['quality'] == 'DEVELOPMENT_CANDIDATE' and link:
                            try:
                                pending.put_nowait((number, event, trigger.fingerprint))
                            except queue.Full:
                                raise RuntimeError('XIAO_DELIVERY_QUEUE_FULL')
                    if count == maximum:
                        break
                for event in processor.finish():
                    b2_file.write(json.dumps(dict(event, origin=origin, forwarded=False,
                                                  source_run_id=run_id, quality='INCOMPLETE')) + '\n')
            if failure:
                raise RuntimeError(failure[0])
            manifest['complete'] = count == maximum
            manifest['stop_reason'] = 'duration_reached' if count == maximum else 'operator_stop'
        except BaseException as exc:
            abort_delivery.set()
            failure.append(f'{type(exc).__name__}: {exc}')
        finally:
            if source:
                try:
                    source.close()
                except Exception as exc:
                    failure.append('AD3_CLOSE: ' + str(exc))
            if recorder:
                try:
                    manifest['video'].update(recorder.stop())
                    manifest['video']['status'] = 'RECORDED'
                except Exception as exc:
                    manifest['video'].update(status='STOP_FAILED', stop_error=f'{type(exc).__name__}: {exc}')
                if manifest['video'].get('output_path'):
                    self.update(state='saving_video', video=dict(manifest['video'], status='MOVING'))
                    try:
                        moved = self.video_collector(manifest['video']['output_path'], directory)
                        manifest['video'].update(moved)
                        manifest['video']['output_path'] = str(directory / moved['video_file'])
                    except Exception as exc:  # the recording stays where OBS saved it
                        manifest['video']['move_error'] = f'{type(exc).__name__}: {exc}'
                manifest['video'].pop('start_ack_monotonic_s', None)
                self.update(video=dict(manifest['video']))
            if sender:
                self.update(state='draining')
                # Never block acquisition on serial I/O; acquisition has stopped now.
                while sender.is_alive():
                    try:
                        pending.put(None, timeout=.1)
                        break
                    except queue.Full:
                        continue
                sender.join(timeout=DELIVERY_DRAIN_TIMEOUT_S)
                if sender.is_alive():
                    failure.append('DELIVERY_THREAD_TIMEOUT')
            if trigger is not None:
                manifest['observation_trigger'].update(trigger.summary(), b2_candidates=b2_number)
            if low is not None:
                manifest['xiao_lowfreq'] = low.summary()
            if link:
                link.close()
            with self.lock:
                self.annotation_path = None  # no labels after the capture ends
                labels = len(self.annotations)
            if labels:
                manifest['annotations'] = dict(file='annotations.jsonl', count=labels, source='HUMAN_OPERATOR',
                                               note='Button-press times on the sample clock; approximate, PC-only, '
                                                    'not detector input and not sent to the XIAO.')
            manifest.update(ended_at_pc=utc_now(), samples=count, candidates=number,
                            forwarded=self.snapshot().get('forwarded', 0), errors=failure)
            if failure:
                manifest['complete'] = False
            raw_path = directory / 'samples.f64'
            if raw_path.exists():
                digest = hashlib.sha256()
                with raw_path.open('rb') as stream:
                    for data in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(data)
                manifest['raw_sha256'] = digest.hexdigest()
            save_manifest()
            if self.collection_importer and request.get('forward') and manifest.get('origin') == 'LIVE':
                # Observation images -> 観察ノート as unreviewed samples. Never affects the run itself.
                if not request.get('case_id'):
                    result = dict(status='SKIPPED_NO_CASE', reason='ケースIDが空のため観察ノートへ取り込みませんでした')
                else:
                    self.update(state='importing')
                    try:
                        result = self.collection_importer(directory, request)
                    except Exception as exc:
                        result = dict(status='FAILED', error=f'{type(exc).__name__}: {exc}')
                manifest['collection_import'] = dict(result, at_pc=utc_now())
                save_manifest()
                self.update(collection_import=manifest['collection_import'])
            self.update(state='error' if failure else ('complete' if manifest['complete'] else 'stopped'),
                        running=False, error='; '.join(failure) if failure else None,
                        samples=count, elapsed_s=count / SAMPLE_RATE,
                        manifest=f'ad3-{run_id}/manifest.json')
