"""PC UTC references and derived display times; original evidence stays immutable."""
from datetime import datetime, timezone, timedelta
import math
import re
import time
import uuid

MIN_UTC_MS = 1577836800000
MAX_UTC_MS = 4102444800000
MAX_REFERENCE_AGE_MS = 3600000
JST = timezone(timedelta(hours=9))


def iso_utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def iso_jst(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).astimezone(JST).isoformat(timespec='milliseconds')


def sync_on_port(port, state, wall_ns=time.time_ns, monotonic_ns=time.monotonic_ns):
    from evidence_client import receive
    if not state.get('clock_sync_supported'):
        raise RuntimeError('UTC同期にはxiao_evidence 0.9.0以降が必要です')
    if not state.get('writable') or state.get('corrupt_segments', 0):
        raise RuntimeError('MCUの保存領域が書込み可能ではありません')
    nonce = uuid.uuid4().hex
    wire = f'evidence-clock-probe {nonce}\n'.encode()
    start_wall, start_mono = wall_ns(), monotonic_ns()
    if port.write(wire) != len(wire):
        raise RuntimeError('Clock probe short write')
    port.flush()
    for line in receive(port, timeout=3):
        if line.startswith('EVIDENCE_CLOCK_PROBE '):
            end_mono, end_wall = monotonic_ns(), wall_ns()
            fields = dict(item.split('=', 1) for item in line.split()[1:])
            break
    else:
        raise TimeoutError('Clock probe timed out')
    if fields.get('nonce') != nonce or fields.get('boot_id') != state['boot_id']:
        raise RuntimeError('Clock probe nonce/boot mismatch')
    if not re.fullmatch(r'[0-9]+', fields.get('monotonic_ms', '')):
        raise RuntimeError('Invalid clock probe time')
    rtt_ns = end_mono - start_mono
    if not 0 <= rtt_ns <= 1000000000 or abs((end_wall - start_wall) - rtt_ns) > 50000000:
        raise RuntimeError('Clock probe delayed or PC clock changed; retry synchronization')
    utc_ms = (start_wall + end_wall) // 2000000
    if not MIN_UTC_MS <= utc_ms < MAX_UTC_MS:
        raise ValueError('PC UTC outside supported 2020..2099 range')
    uncertainty = math.ceil(rtt_ns / 2000000) + 1
    wire = f'evidence-time-sync {state["boot_id"]} {nonce} {utc_ms} {uncertainty}\n'.encode()
    if port.write(wire) != len(wire):
        raise RuntimeError('Clock commit short write; delivery unknown')
    port.flush()
    for line in receive(port, timeout=10):
        if line.startswith('EVIDENCE_TIME_SYNC '):
            result = dict(item.split('=', 1) for item in line.split()[1:])
            if result.get('status') != 'stored' or result.get('time_quality') != 'ESTIMATED' or not re.fullmatch(
                    re.escape(state['device_id']+'/'+state['boot_id']+'/')+r'[1-9][0-9]*', result.get('event_id', '')):
                raise RuntimeError('Invalid clock commit acknowledgement')
            return dict(result, reference_utc=iso_utc(utc_ms), reference_jst=iso_jst(utc_ms),
                        device_monotonic_ms=int(fields['monotonic_ms']), round_trip_ms=rtt_ns / 1000000,
                        transport_uncertainty_ms=uncertainty, pc_clock_accuracy='UNVERIFIED')
    raise TimeoutError('Clock commit ACK missing; delivery unknown, inspect TIME_SYNC log')


def evidence_times(events):
    """Only map within the same device/boot and one hour of a durable reference."""
    anchors = [event for event in events if event['event_type'] == 'TIME_SYNC']
    output = {}
    for event in events:
        reference = None
        if event['time_quality'] == 'ESTIMATED':
            reference = next((a for a in anchors if a['event_id'] == event['clock_sync_event_id']), None)
            if reference is None:
                continue
            epoch = round(datetime.fromisoformat(event['timestamp_utc'].replace('Z', '+00:00')).timestamp() * 1000)
            basis = 'STORED_UTC'
        else:
            nearby = [a for a in anchors if a['boot_id'] == event['boot_id'] and a['device_id'] == event['device_id']
                      and abs(event['monotonic_ms'] - a['result']['device_monotonic_ms']) <= MAX_REFERENCE_AGE_MS]
            if not nearby:
                continue
            reference = min(nearby, key=lambda a: abs(event['monotonic_ms'] - a['result']['device_monotonic_ms']))
            epoch = reference['result']['utc_unix_ms'] + event['monotonic_ms'] - reference['result']['device_monotonic_ms']
            basis = 'SAME_BOOT_REFERENCE'
        if not MIN_UTC_MS <= epoch < MAX_UTC_MS:
            continue
        output[event['event_id']] = dict(timestamp_utc=iso_utc(epoch), timestamp_jst=iso_jst(epoch),
            time_quality='ESTIMATED', basis=basis, reference_event_id=reference['event_id'],
            transport_uncertainty_ms=reference['result']['transport_uncertainty_ms'], pc_clock_accuracy='UNVERIFIED')
    return output


def ad3_candidate_time(event, manifest):
    """Approximate source time from the PC acquisition-start bracket, never MCU receive time."""
    if (manifest.get('origin') != 'LIVE' or manifest.get('run_id') != event.get('result', {}).get('source_run_id')):
        return None
    reference = manifest.get('acquisition', {}).get('sample_clock_reference')
    if not reference or reference.get('basis') != 'PC_CONFIGURE_BRACKET':
        return None
    start, end = reference.get('pc_utc_before_ms'), reference.get('pc_utc_after_ms')
    if type(start) is not int or type(end) is not int or not MIN_UTC_MS <= start <= end < MAX_UTC_MS or end-start > 1000:
        return None
    middle = (start + end) // 2
    result = event['result']
    return dict(start_jst=iso_jst(middle + result['source_start_ms']),
                end_jst=iso_jst(middle + result['source_end_ms']), time_quality='ESTIMATED',
                basis='PC_CONFIGURE_BRACKET', hardware_start_accuracy='UNVERIFIED')
