"""XIAO bridge protocol. Mock and replay never call the persistent LIVE command."""
from datetime import datetime, timezone
import json
import re
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from evidence_client import status, export as export_evidence
from time_sync import sync_on_port
from vision_model import model_from_inspect_lines


def read_evidence_snapshot(port_name, serial_factory=None):
    """Read-only export also works when storage is mounted but no longer writable."""
    if serial_factory is None:
        from serial import Serial
        serial_factory = Serial
    port = serial_factory(port=None, baudrate=115200, timeout=.1, write_timeout=3)
    try:
        port.dtr = True
        port.rts = False
        port.port = port_name
        port.open()
        port.reset_input_buffer()
        return export_evidence(port)
    finally:
        port.close()


OBSERVATION_TIMEOUT_S = 45  # XIAO capture <= 25 s plus BREAK and two Flash commits
# 0.13.0+: a DONE with followup=armed is followed, after the 5 s camera cooldown, by one
# more step for the same trigger (another arm view: move <= 4 s, settle, capture).
FOLLOW_UP_TIMEOUT_S = OBSERVATION_TIMEOUT_S + 15
MAX_FOLLOW_UPS = 3  # per-trigger capture budget of the policy
# Before 0.14.3 the XIAO re-read its whole log for every candidate before the ACK; at
# ~470 KB of log that passed 10 s (2026-11-11). 0.14.3 uses an in-RAM index; the margin
# stays for older firmware.
ACK_TIMEOUT_S = 30
# Longest one send() may legitimately block: ACK, first step, and every follow-up.
MAX_DELIVERY_S = ACK_TIMEOUT_S + OBSERVATION_TIMEOUT_S + MAX_FOLLOW_UPS * FOLLOW_UP_TIMEOUT_S
MAX_OBSERVATION_LINE = 512 * 1024  # one base64 JPEG event line
VISION_INSPECT_TIMEOUT_S = 20  # six SSCMA queries, 5 s each at worst on a dead link
LOWFREQ_REPLY_TIMEOUT_S = 2.0  # one lowfreq-* line; the XIAO answers within its next loop turn
MODE_REPLY_TIMEOUT_S = 2.0     # one observe-mode line (firmware 0.18.1+)
# follow / explore: 0.18.1+; hold (follow, and the arm waits where the larva was seen): 0.18.2+
OBSERVATION_MODES = ('follow', 'explore', 'hold')


def _json_line(line, prefix):
    value = json.loads(line[len(prefix):])
    if not isinstance(value, dict):
        raise RuntimeError('Invalid observation line')
    return value


def _pc_now():
    return datetime.now(timezone.utc).isoformat()


# Arm lines the XIAO already prints (observation_runtime.cpp). The bridge keeps their PC
# receipt times so the review can show when the arm was moving (its own sound can raise CH2).
# Display only: nothing here reaches the detector, the candidates or the XIAO.
ACTUATION_LINES = {'OBSERVATION_MOVE_BEGIN': 'MOVE_BEGIN', 'OBSERVATION_CAPTURE_BEGIN': 'CAPTURE_BEGIN',
                   'SERVO_PARK': 'PARK'}
ACTUATION_FIELDS = ('action_id', 'pose', 'target_us', 'us', 'elapsed_ms', 'reached_us', 'reason')
PARK_WATCH_S = 12.0  # firmware park timeout is 10 s


def actuation_line(line, late_read_possible=False):
    head, _, rest = line.partition(' ')
    kind = ACTUATION_LINES.get(head)
    if kind is None:
        return None
    fields = dict(part.split('=', 1) for part in rest.split() if '=' in part)
    if kind == 'PARK':
        kind = 'PARK_' + re.sub('[^A-Z]', '', fields.get('status', '').upper())[:12]
    record = dict(kind=kind, received_at_pc=_pc_now(), time_basis='PC_RECEIPT',
                  **{k: fields[k][:80] for k in ACTUATION_FIELDS if k in fields})
    if late_read_possible:
        record['late_read_possible'] = True  # may have waited in the USB buffer before this read
    return record


def read_observation(port, deadline, already_read=b'', actuation=None):
    """Collect one XIAO observation step: decision, optional capture and result, DONE.
    Arm lines are appended to `actuation` (a list) when given."""
    observation = dict(decision=None, result=None, image_b64=None, model_raw=None, done=None, tail=b'')
    pending = bytearray(already_read)
    while time.monotonic() < deadline:
        pending.extend(port.readline(65536))
        if len(pending) > MAX_OBSERVATION_LINE:
            raise RuntimeError('Oversize observation line')
        while b'\n' in pending:
            raw, _, tail = pending.partition(b'\n')
            pending = bytearray(tail)
            line = raw.decode('utf-8', errors='strict').strip()
            noted = actuation_line(line) if actuation is not None else None
            if noted:
                actuation.append(noted)
            elif line.startswith('OBSERVATION_DECISION '):
                observation['decision'] = _json_line(line, 'OBSERVATION_DECISION ')
            elif line.startswith('OBSERVATION_RESULT '):
                observation['result'] = _json_line(line, 'OBSERVATION_RESULT ')
                observation['result_received_at_pc'] = _pc_now()
            elif line.startswith(('VISION_SAMPLE_REPLY ', 'VISION_CLASSIFY_REPLY ')):
                # 0.14.0+: INVOKE events carry the inference frame and its raw classes.
                prefix = line.split(' ', 1)[0] + ' '
                reply = _json_line(line, prefix)
                data = reply.get('data') if isinstance(reply.get('data'), dict) else {}
                image = data.get('image') if reply.get('type') == 1 else None
                if isinstance(image, str) and image:
                    if observation['image_b64'] is not None:
                        raise RuntimeError('More than one frame for one observation')
                    observation['image_b64'] = image
                    # PC receipt of the frame line, seconds after the exposure (USB transfer only);
                    # the review page places the image on the AD3 time axis with it.
                    observation['frame_received_at_pc'] = _pc_now()
                    if prefix == 'VISION_CLASSIFY_REPLY ':
                        observation['model_raw'] = dict(classes=data.get('classes'), perf=data.get('perf'))
            elif line.startswith('OBSERVATION_DONE '):
                fields = dict(part.split('=', 1) for part in line.split()[1:] if '=' in part)
                fields['reasons'] = json.loads(fields.get('reasons', '[]'))
                observation['done'] = fields
                observation['tail'] = bytes(pending)  # lines already read for the next step
                return observation
    raise TimeoutError('OBSERVATION_TIMEOUT; decision/capture outcome unknown, inspect XIAO evidence log')


def candidate_payload(run_id, number, event, fingerprint):
    if not re.fullmatch('[0-9a-f]{32}', run_id) or not re.fullmatch('[0-9a-f]{64}', fingerprint):
        raise ValueError('Invalid provenance')
    if event.get('quality') != 'DEVELOPMENT_CANDIDATE':
        raise ValueError('Incomplete candidates cannot be forwarded')
    values = [number, round(event['t_start_s'] * 1000), round(event['t_end_s'] * 1000),
              round(event['peak_rms_V'] * 1000000), round(event['peak_score'] * 1000)]
    seq, start, end, peak, score = values
    # 0.17.0: the low-frequency trigger names itself (8th token) and sends its 1-40 Hz ratio as the
    # score; B2 candidates keep the 7-token form older firmware accepts.
    detector = event.get('detector', 'B2_guarded_mad')
    if detector not in ('B2_guarded_mad', 'LOWFREQ_FOREST_RATIO'):
        raise ValueError('Unknown detector')
    low_score = 4000 if detector == 'B2_guarded_mad' else 1
    if (type(seq) is not int or not 1 <= seq <= 1000000 or not 0 <= start < end <= 0xffffffff
            or end - start > 15000 or not 1 <= peak <= 50000000 or not low_score <= score <= 1000000000):
        raise ValueError('Candidate outside wire limits')
    tail = '' if detector == 'B2_guarded_mad' else f' {detector}'
    return f'{run_id} ' + ' '.join(str(v) for v in values) + f' {fingerprint}' + tail


class XiaoLink:
    def __init__(self, port, origin, serial_factory=None):
        if origin != 'LIVE':
            raise ValueError('Only LIVE AD3 acquisition may open XIAO forwarding')
        if serial_factory is None:
            from serial import Serial
            serial_factory = Serial
        self.port = serial_factory(port=None, baudrate=115200, timeout=.1, write_timeout=3)
        try:
            self.port.dtr = True
            self.port.rts = False
            self.port.port = port
            self.port.open()
            self.port.reset_input_buffer()
            self.state = status(self.port)
            if 'passive_candidates' not in self.state:
                raise RuntimeError('Upload xiao_evidence 0.8.0 for AD3 candidate reception')
            if not self.state.get('writable') or self.state.get('corrupt_segments', 0):
                reason = self.state.get('storage_reason') or 'unknown'
                hint = (' - log full: export and rotate it (scripts/evidence_client.py --rotate)'
                        if reason == 'SPACE_RESERVE_REACHED' else '')
                raise RuntimeError(f'XIAO Evidence storage unavailable ({reason}, corrupt_segments='
                                   f"{self.state.get('corrupt_segments', 0)}){hint}")
            self.device_id = self.state['device_id']
            self.boot_id = self.state['boot_id']
            # 0.10.0+: a stored candidate is followed by one observation step on the XIAO.
            self.observation_supported = bool(self.state.get('observation_policy'))
            self.actuation = []  # arm lines read so far; the manager writes and clears them
        except BaseException:
            self.port.close()
            raise

    def send(self, run_id, number, event, fingerprint, validate_only=False):
        payload = candidate_payload(run_id, number, event, fingerprint)
        prefix = 'passive-check' if validate_only else 'passive-candidate'
        wire = (prefix + ' ' + payload + '\n').encode('ascii')
        if len(wire) > 257:
            raise ValueError('Command exceeds firmware limit')
        if self.port.write(wire) != len(wire):
            raise RuntimeError('Partial serial write; delivery unknown')
        self.port.flush()
        deadline = time.monotonic() + ACK_TIMEOUT_S
        pending = bytearray()
        while time.monotonic() < deadline:
            pending.extend(self.port.readline(2048))
            if len(pending) > 8192:
                raise RuntimeError('Oversize serial response')
            while b'\n' in pending:
                raw, _, tail = pending.partition(b'\n')
                pending = bytearray(tail)
                line = raw.decode('utf-8', errors='strict').strip()
                if line.startswith(('PASSIVE_ERROR ', 'ERROR ', 'EVIDENCE_ERROR ')):
                    raise RuntimeError(line)
                if not line.startswith('PASSIVE_ACK '):
                    # e.g. the end of a park that was still running when this candidate was sent
                    noted = actuation_line(line, late_read_possible=True)
                    if noted:
                        self.actuation.append(noted)
                    continue
                fields = dict(part.split('=', 1) for part in line.split()[1:])
                if fields.get('run') != run_id or fields.get('candidate') != str(number):
                    raise RuntimeError('Unexpected candidate acknowledgement')
                if validate_only:
                    if fields.get('status') != 'validated_only' or 'event_id' in fields:
                        raise RuntimeError('Invalid test acknowledgement')
                elif fields.get('status') not in {'stored', 'duplicate'} or not re.fullmatch(
                        re.escape(self.device_id) + '/[0-9a-f]{32}/[1-9][0-9]*', fields.get('event_id', '')):
                    raise RuntimeError('Missing durable acknowledgement')
                if fields['status'] == 'stored' and self.observation_supported:
                    # The XIAO decides only after the candidate is durable; a duplicate never re-decides.
                    observation = read_observation(self.port, time.monotonic() + OBSERVATION_TIMEOUT_S,
                                                   bytes(pending), self.actuation)
                    if observation['done'].get('trigger') != fields['event_id']:
                        raise RuntimeError('Observation step answered a different trigger')
                    follow_ups, last = [], observation
                    while last['done'].get('followup') == 'armed':
                        if len(follow_ups) >= MAX_FOLLOW_UPS:
                            raise RuntimeError('More follow-up steps than the episode budget')
                        last = read_observation(self.port, time.monotonic() + FOLLOW_UP_TIMEOUT_S,
                                                last.pop('tail', b''), self.actuation)
                        if last['done'].get('trigger') != fields['event_id']:
                            raise RuntimeError('Follow-up step answered a different trigger')
                        follow_ups.append(last)
                    self._after_episode = last.get('tail', b'')
                    for step in [observation] + follow_ups:
                        step.pop('tail', None)
                    fields['observation'] = observation
                    fields['follow_ups'] = follow_ups
                return fields
        raise TimeoutError('XIAO_ACK_TIMEOUT; delivery unknown, no automatic retry')

    def lowfreq(self, command):
        """One lowfreq-* line (low-frequency forest, firmware 0.15.0+, reference only) and its LFF*
        reply line. None when the firmware does not know the command (it echoes it). Arm lines read
        meanwhile are noted as in send(); other lines (heartbeat) are dropped."""
        if not command.startswith('lowfreq-') or len(command) > 120:
            raise ValueError('Not a lowfreq command')
        wire = (command + '\n').encode('ascii')
        if self.port.write(wire) != len(wire):
            raise RuntimeError('Partial serial write')
        self.port.flush()
        deadline = time.monotonic() + LOWFREQ_REPLY_TIMEOUT_S
        pending = bytearray()
        while time.monotonic() < deadline:
            pending.extend(self.port.readline(2048))
            if len(pending) > 8192:
                raise RuntimeError('Oversize serial response')
            while b'\n' in pending:
                raw, _, tail = pending.partition(b'\n')
                pending = bytearray(tail)
                line = raw.decode('utf-8', errors='replace').strip()
                if line.startswith('LFF'):
                    if line.startswith('LFF_ERROR'):
                        raise RuntimeError(line)
                    return line
                if line.startswith('ECHO lowfreq-'):
                    return None
                noted = actuation_line(line, late_read_possible=True)
                if noted:
                    self.actuation.append(noted)
        raise TimeoutError('LOWFREQ_REPLY_TIMEOUT')

    def set_mode(self, mode):
        """'observe-mode follow|explore' (firmware 0.18.1+, RAM only on the MCU): follow looks again
        where the image AI last saw the larva (0.18.0 rule), explore takes the views not seen
        recently (until 0.17.0). Returns the mode the MCU confirmed, or None when the firmware
        does not know the command (0.18.0 always follows, 0.17.0 and older always explore)."""
        if mode not in OBSERVATION_MODES:
            raise ValueError('mode must be follow, explore or hold')
        wire = f'observe-mode {mode}\n'.encode('ascii')
        if self.port.write(wire) != len(wire):
            raise RuntimeError('Partial serial write')
        self.port.flush()
        deadline = time.monotonic() + MODE_REPLY_TIMEOUT_S
        pending = bytearray()
        while time.monotonic() < deadline:
            pending.extend(self.port.readline(2048))
            if len(pending) > 8192:
                raise RuntimeError('Oversize serial response')
            while b'\n' in pending:
                raw, _, tail = pending.partition(b'\n')
                pending = bytearray(tail)
                line = raw.decode('utf-8', errors='replace').strip()
                if line.startswith('OBSERVATION_MODE mode='):
                    return line.split('=', 1)[1].strip()
                if line == 'OBSERVATION_ERROR reason=unknown_command' or line.startswith('ECHO observe-mode'):
                    return None
                if line.startswith('OBSERVATION_ERROR'):
                    raise RuntimeError(line)
                noted = actuation_line(line, late_read_possible=True)
                if noted:
                    self.actuation.append(noted)
        raise TimeoutError('MODE_REPLY_TIMEOUT')

    def watch_park(self, stop, max_s=PARK_WATCH_S):
        """After an episode that moved the arm, keep reading only to note when the XIAO's park
        (return to the home pose) begins and ends. Stops at the park's end, as soon as stop()
        is true (a new candidate is waiting: it is never delayed for this), or after max_s.
        Other lines are dropped, as send() drops lines before an ACK."""
        pending = bytearray(getattr(self, '_after_episode', b''))
        self._after_episode = b''
        deadline = time.monotonic() + max_s
        while True:
            while b'\n' in pending:
                raw, _, tail = pending.partition(b'\n')
                pending = bytearray(tail)
                noted = actuation_line(raw.decode('utf-8', errors='replace').strip())
                if noted:
                    self.actuation.append(noted)
                    if noted['kind'] in ('PARK_DONE', 'PARK_FAILED'):
                        return True
            if stop() or time.monotonic() >= deadline or len(pending) > 65536:
                return False
            pending.extend(self.port.readline(2048))

    def read_vision_model(self):
        """Read-only 'vision' inspect; identify the deployed model. Never raises on a
        missing or unknown model: its output is then shown as unverified."""
        self.port.write(b'vision\n')
        self.port.flush()
        deadline = time.monotonic() + VISION_INSPECT_TIMEOUT_S
        pending, lines = bytearray(), []
        while time.monotonic() < deadline:
            pending.extend(self.port.readline(65536))
            if len(pending) > MAX_OBSERVATION_LINE:
                raise RuntimeError('Oversize serial response')
            while b'\n' in pending:
                raw, _, tail = pending.partition(b'\n')
                pending = bytearray(tail)
                line = raw.decode('utf-8', errors='replace').strip()
                if line == 'VISION_INSPECT_DONE':
                    return model_from_inspect_lines(lines)
                lines.append(line)
        return dict(status='UNVERIFIED', reason='inspect_timeout', model=None)

    def close(self):
        self.port.close()

    def synchronize_clock(self):
        self.state = status(self.port)
        expected_boot = self.state['boot_id']
        result = sync_on_port(self.port, self.state)
        self.state = status(self.port)
        if (self.state['boot_id'] != expected_boot or self.state.get('time_quality') != 'ESTIMATED'
                or self.state.get('clock_sync_event_id') != result['event_id']):
            raise RuntimeError('XIAO restarted or UTC reference not active; retry synchronization')
        self.boot_id = expected_boot
        return result
