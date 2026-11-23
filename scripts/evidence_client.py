"""Export XIAO-owned diagnostic evidence. Does not invent biological observations."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

SCHEMA = 'bll.evidence.v1'
ACTIONS = {'WAIT','CAPTURE_FIXED','CAPTURE_FRONT','CAPTURE_TOP','CAPTURE_BACK'}
POSE_OF = {'CAPTURE_FIXED':'FIXED','CAPTURE_FRONT':'FRONT','CAPTURE_TOP':'TOP','CAPTURE_BACK':'BACK'}
OBSERVATION_TYPES = {'POLICY_DECISION','OBSERVATION_ACTION_RESULT'}


def _text(value,maximum):
    return isinstance(value,str) and 0<len(value)<=maximum


def validate_decision(event,result):
    # Rule-based decision persisted before any physical action. Not a larva result.
    if (result.get('scope')!='OBSERVATION_POLICY' or result.get('kind')!='RULE_BASED_NOT_TRAINED'
            or not _text(result.get('policy_version'),40) or not _text(result.get('reducer_version'),40)
            or not _text(result.get('decision_id'),47) or result.get('action') not in ACTIONS):
        raise ValueError('Invalid policy decision')
    causes,reasons=result.get('caused_by_event_ids'),result.get('reason_codes')
    if (not isinstance(causes,list) or len(causes)!=1 or not _text(causes[0],99)
            or not isinstance(reasons,list) or not 1<=len(reasons)<=16 or not all(_text(r,40) for r in reasons)):
        raise ValueError('Invalid decision provenance')
    if result['action']=='WAIT':
        if result.get('action_id','missing') is not None or result.get('target_pose','missing') is not None \
                or event.get('observation_id') is not None:
            raise ValueError('WAIT must not create an action')
    elif (not _text(result.get('action_id'),47) or event.get('observation_id')!=result['action_id']
            or result.get('target_pose')!=POSE_OF[result['action']]):
        raise ValueError('Invalid action intent')


def validate_action_result(event,result):
    capture=result.get('capture')
    if (result.get('scope')!='OBSERVATION_ACTION' or not _text(result.get('action_id'),47)
            or event.get('observation_id')!=result['action_id'] or not _text(result.get('decision_event_id'),99)
            or result.get('action') not in POSE_OF or result.get('pose')!=POSE_OF[result['action']]
            or not isinstance(capture,dict) or capture.get('presence')!='UNKNOWN'
            or capture.get('movement')!='UNKNOWN' or not _text(capture.get('reason'),40)):
        raise ValueError('Invalid observation result')
    allowed={'COMPLETED':{'SUCCESS'},'FAILED':{'FAILED','NOT_ATTEMPTED'},'INTERRUPTED':{'UNKNOWN'}}
    if capture.get('status') not in allowed.get(result.get('status'),set()):
        raise ValueError('Inconsistent observation status')
    vision=result.get('vision')  # 0.17.0: model output of the captured image
    if vision is not None:
        if (not isinstance(vision,dict) or capture.get('status')!='SUCCESS'
                or type(vision.get('top_class_id')) is not int or not 0<=vision['top_class_id']<=255
                or type(vision.get('score')) is not int or not 0<=vision['score']<=100
                or type(vision.get('larva_visible')) is not bool
                or vision['larva_visible']!=(vision['top_class_id']==1 and vision['score']>=80)):
            raise ValueError('Invalid vision output')
    image=event.get('image_ref')
    if capture['status']!='SUCCESS':
        if image is not None:raise ValueError('Image reference without a capture')
    elif (not isinstance(image,dict) or image.get('store')!='HOST_USB_STREAM'
            or image.get('status')!='SENT_TO_HOST_UNCONFIRMED'
            or not isinstance(image.get('payload_sha256'),str) or not re.fullmatch('[0-9a-f]{64}',image['payload_sha256'])
            or type(image.get('payload_bytes')) is not int or image['payload_bytes']<=0):
        raise ValueError('Invalid image reference')


def validate_event(event):
    if not isinstance(event, dict) or event.get('schema_version') != SCHEMA:
        raise ValueError('Unsupported evidence schema')
    device,boot,seq=event.get('device_id'),event.get('boot_id'),event.get('seq')
    if not isinstance(device,str) or not re.fullmatch(r'(xiao|nano)-[0-9a-f]{12}',device):  # nano-: Arduino Nano ESP32 (0.16.0)
        raise ValueError('Invalid device ID')
    if not isinstance(boot,str) or not re.fullmatch(r'[0-9a-f]{32}',boot):raise ValueError('Invalid boot ID')
    if type(seq) is not int or not 0<seq<=0xffffffff:raise ValueError('Invalid sequence')
    if event.get('event_id')!=f'{device}/{boot}/{seq}':raise ValueError('Event ID mismatch')
    passive=event.get('event_type')=='PASSIVE_VIBRATION_CANDIDATE'
    sync=event.get('event_type')=='TIME_SYNC'
    observation=event.get('event_type') in OBSERVATION_TYPES
    if event.get('origin')!='LIVE' or event.get('source')!=('PC_AD3' if passive else 'PC_CLOCK' if sync else 'XIAO'):
        raise ValueError('Unsupported origin/source')
    if event.get('stream_id')!=f'live/{device}':raise ValueError('Invalid stream')
    if event.get('event_type') not in {'BOOT','DEVICE_DIAGNOSTIC','PASSIVE_VIBRATION_CANDIDATE','TIME_SYNC'}|OBSERVATION_TYPES:
        raise ValueError('Unsupported event type')
    if event.get('time_quality')=='UNSYNCED':
        if sync or event.get('timestamp_utc','missing') is not None or 'clock_sync_event_id' in event:
            raise ValueError('Unexpected unsynced clock contract')
    elif event.get('time_quality')=='ESTIMATED':
        value=event.get('timestamp_utc')
        if not isinstance(value,str) or not re.fullmatch(r'20[2-9][0-9]-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z',value):
            raise ValueError('Invalid UTC timestamp')
        datetime.fromisoformat(value.replace('Z','+00:00'))
        if not isinstance(event.get('clock_sync_event_id'),str) or not re.fullmatch(
                re.escape(device+'/'+boot+'/')+r'[1-9][0-9]*',event['clock_sync_event_id']):
            raise ValueError('Invalid clock reference')
    else:raise ValueError('Unexpected clock quality')
    if type(event.get('monotonic_ms')) is not int or event['monotonic_ms']<0:raise ValueError('Invalid monotonic clock')
    if event.get('quality')!={'valid':True,'reason_codes':[]}:raise ValueError('Invalid diagnostic quality')
    result=event.get('result',{})
    if not isinstance(result,dict):raise ValueError('Invalid result')
    if sync:
        if result.get('scope')!='CLOCK_REFERENCE_ONLY' or result.get('pc_clock_accuracy')!='UNVERIFIED':
            raise ValueError('Invalid clock source')
        if not isinstance(result.get('sync_id'),str) or not re.fullmatch('[0-9a-f]{32}',result['sync_id']):
            raise ValueError('Invalid clock sync ID')
        for key,low,high in (('utc_unix_ms',1577836800000,4102444799999),
                             ('device_monotonic_ms',0,event['monotonic_ms']),('transport_uncertainty_ms',1,501)):
            if type(result.get(key)) is not int or not low<=result[key]<=high:
                raise ValueError('Invalid clock reference value')
        if event['monotonic_ms']-result['device_monotonic_ms']>5000 or event['clock_sync_event_id']!=event['event_id']:
            raise ValueError('Invalid clock reference association')
    elif passive:
        # 0.17.0: LOWFREQ_FOREST_RATIO (PC forest or 1-40 Hz ratio >= 3) sends its ratio as the score
        if result.get('scope')!='PASSIVE_CANDIDATE_ONLY' or result.get('detector') not in ('B2_guarded_mad','LOWFREQ_FOREST_RATIO'):
            raise ValueError('Invalid passive scope')
        for key,length in (('source_run_id',32),('config_sha256',64)):
            if not isinstance(result.get(key),str) or not re.fullmatch('[0-9a-f]{'+str(length)+'}',result[key]):
                raise ValueError('Invalid passive provenance')
        for key,low,high in (('candidate_seq',1,1000000),('source_start_ms',0,0xffffffff),
                             ('source_end_ms',1,0xffffffff),('peak_rms_uV',1,50000000),
                             ('peak_score_milli',4000 if result['detector']=='B2_guarded_mad' else 1,1000000000)):
            if type(result.get(key)) is not int or not low<=result[key]<=high:
                raise ValueError('Invalid passive feature')
        if not 0<result['source_end_ms']-result['source_start_ms']<=15000:
            raise ValueError('Invalid passive duration')
        if result.get('source_time_quality')!='SAMPLE_CLOCK_ONLY' or result.get('clipping_verified') is not False:
            raise ValueError('Invalid passive quality')
    elif event['event_type']=='POLICY_DECISION':validate_decision(event,result)
    elif event['event_type']=='OBSERVATION_ACTION_RESULT':validate_action_result(event,result)
    elif result.get('scope')!='DEVICE_DIAGNOSTIC_ONLY':raise ValueError('Invalid diagnostic scope')
    if any(k not in event for k in ('observation_id','image_ref')):raise ValueError('Missing observation context')
    if any(event.get(k,'missing') is not None for k in ('case_id','session_id','request_id','model_version')) \
            or (not observation and event['observation_id'] is not None) \
            or (event['event_type']!='OBSERVATION_ACTION_RESULT' and event['image_ref'] is not None):
        raise ValueError('Unexpected observation context')
    if not isinstance(event.get('producer_version'),str):raise ValueError('Missing producer version')
    return event


def reduce_events(events):
    """Host mirror for diagnostics. No external event can confirm animal movement."""
    unique={}
    for event in events:
        validate_event(event);key=event['event_id']
        if key in unique and unique[key]!=event:raise ValueError('Conflicting duplicate event ID')
        unique[key]=event
    return dict(events=len(unique),boot_events=sum(e['event_type']=='BOOT' for e in unique.values()),
        diagnostic_events=sum(e['event_type']=='DEVICE_DIAGNOSTIC' for e in unique.values()),
        passive_candidates=sum(e['event_type']=='PASSIVE_VIBRATION_CANDIDATE' for e in unique.values()),
        time_sync_events=sum(e['event_type']=='TIME_SYNC' for e in unique.values()),
        decision_events=sum(e['event_type']=='POLICY_DECISION' for e in unique.values()),
        action_result_events=sum(e['event_type']=='OBSERVATION_ACTION_RESULT' for e in unique.values()),
        current_activity='UNKNOWN',visual_presence='UNKNOWN',last_confirmed_movement_utc=None,
        passive_status='EXPERIMENTAL',active_status='HISTORICAL_ONLY',time_quality='UNSYNCED')


def receive(port,timeout=15):
    end=time.monotonic()+timeout
    pending=bytearray()
    while time.monotonic()<end:
        line=port.readline(8192)
        if not line:continue
        pending.extend(line)
        if len(pending)>8192:raise ValueError('Evidence serial line too large')
        while b'\n' in pending:
            raw,_,rest=pending.partition(b'\n');pending=bytearray(rest)
            text=raw.decode('utf-8',errors='strict').strip()
            if text.startswith('EVIDENCE_ERROR '):raise RuntimeError(text)
            if text.startswith('EVIDENCE_'):yield text


def status(port):
    port.write(b'evidence-status\n');port.flush()
    for line in receive(port):
        if line.startswith('EVIDENCE_STATUS '):return json.loads(line.split(' ',1)[1])
    raise TimeoutError('Evidence status not received: the board did not answer (needs nano_evidence or '
                       'xiao_evidence firmware; if it was running, it may be hung: replug USB)')


def log_digest(lines):
    """(event count, byte count, SHA-256) of the exact lines the XIAO sent, each plus a newline.
    The XIAO computes the same over its stored lines before evidence-rotate."""
    digest=hashlib.sha256();size=0
    for line in lines:
        data=line.encode('utf-8')+b'\n';digest.update(data);size+=len(data)
    return len(lines),size,digest.hexdigest()


def export(port,raw=None):
    """Full export. When a list is passed as raw, it receives each event line exactly as sent."""
    state=status(port)
    if not state.get('mounted'):raise RuntimeError(state.get('storage_reason','Storage unavailable'))
    index=0;events=[]
    while True:
        port.write(f'evidence-read {index}\n'.encode());port.flush()
        began=False;page=[];lines=[];ending=None
        for line in receive(port):
            if line.startswith('EVIDENCE_BEGIN '):
                if line!=f'EVIDENCE_BEGIN after_index={index}':raise ValueError('Page cursor mismatch')
                began=True
            elif line.startswith('EVIDENCE_EVENT '):
                if not began:raise ValueError('Event before page header')
                text=line.split(' ',1)[1];page.append(validate_event(json.loads(text)));lines.append(text)
            elif line.startswith('EVIDENCE_END '):
                ending=dict(part.split('=',1) for part in line.split()[1:]);break
        if not began or ending is None:raise TimeoutError('Incomplete evidence page')
        next_index=int(ending['next_index'])
        if next_index!=index+len(page) or len(page)>16:raise ValueError('Page count mismatch')
        if any(e['device_id']!=state['device_id'] for e in page):raise ValueError('Wrong device')
        events.extend(page)
        if raw is not None:raw.extend(lines)
        if len(events)>2048:raise ValueError('Export exceeds bounded diagnostic capacity')
        if ending['has_more']=='0':break
        if ending['has_more']!='1' or next_index<=index:raise ValueError('Non-progressing cursor')
        index=next_index
    mirror=reduce_events(events)
    if len(events)!=state['events'] or mirror['events']!=state['events']:
        raise ValueError('State/export count mismatch; preserve evidence and retry')
    counters={'passive_candidates','time_sync_events','decision_events','action_result_events'}
    for key in ('boot_events','diagnostic_events','passive_candidates','time_sync_events','decision_events',
                'action_result_events','current_activity','visual_presence','last_confirmed_movement_utc'):
        if mirror[key]!=state.get(key,0 if key in counters else None):raise ValueError(f'Restored state mismatch: {key}')
    by_id={e['event_id']:e for e in events}
    for event in events:
        if event['time_quality']!='ESTIMATED':continue
        reference=by_id.get(event['clock_sync_event_id'])
        if reference is None or reference['event_type']!='TIME_SYNC' or reference['boot_id']!=event['boot_id']:
            raise ValueError('Missing clock reference in export')
        result=reference['result'];delta=event['monotonic_ms']-result['device_monotonic_ms']
        if not 0<=delta<=3600000:raise ValueError('Expired stored clock reference')
        actual=round(datetime.fromisoformat(event['timestamp_utc'].replace('Z','+00:00')).timestamp()*1000)
        if actual!=result['utc_unix_ms']+delta:raise ValueError('UTC/reference mismatch')
    if state.get('time_quality')=='ESTIMATED':
        reference=by_id.get(state.get('clock_sync_event_id'))
        if reference is None or reference['event_type']!='TIME_SYNC' or reference['boot_id']!=state.get('boot_id'):
            raise ValueError('Invalid active clock state')
        if type(state.get('clock_age_ms')) is not int or not 0<=state['clock_age_ms']<=3600000:
            raise ValueError('Invalid active clock age')
        mirror['time_quality']='ESTIMATED'
    return state,events,mirror


BACKUP_BYTES=0x180000  # read_flash 0x670000 0x180000 (the whole LittleFS partition)


def rotate(port,lines):
    """evidence-rotate: the XIAO empties its log only if its own lines give the same count and
    SHA-256, then restarts. Raises on any EVIDENCE_ERROR; returns the stage=done fields."""
    count,_,sha=log_digest(lines)
    port.write(f'evidence-rotate {count} {sha}\n'.encode());port.flush()
    verified=False
    for line in receive(port,90):
        if line.startswith('EVIDENCE_ROTATE stage=verified'):verified=True
        elif line.startswith('EVIDENCE_ROTATE stage=done'):
            if not verified:raise ValueError('Rotation finished without verification')
            return dict(part.split('=',1) for part in line.split()[2:])
    raise TimeoutError('Rotation response not received; read the device state before retrying')


def saved_matches(path,lines,state):
    """The export file on disk holds exactly these lines and the device's event count."""
    count,size,sha=log_digest(lines)
    saved=path.read_bytes()
    return (hashlib.sha256(saved).hexdigest()==sha and len(saved)==size
            and saved.count(b'\n')==count and count==state['events'])


def write_meta(path,meta):
    path.with_suffix('.json').write_text(json.dumps(meta,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--port',required=True)
    ap.add_argument('--output',type=Path,default=Path('captures/evidence'))
    ap.add_argument('--init-empty',action='store_true',help='Explicit initialization, refused unless expected FS partition is entirely erased')
    ap.add_argument('--rotate',action='store_true',
        help='After saving and re-reading a verified export, ask the XIAO to empty its log (it restarts). Needs --flash-backup')
    ap.add_argument('--flash-backup',type=Path,help='read_flash 0x670000 0x180000 image taken before --rotate')
    args=ap.parse_args()
    if args.rotate and (args.init_empty or not args.flash_backup or not args.flash_backup.is_file()
                        or args.flash_backup.stat().st_size!=BACKUP_BYTES):
        ap.error('--rotate needs --flash-backup with a 1,572,864-byte read_flash image of the partition')
    import serial
    with serial.Serial(port=None,baudrate=115200,timeout=.25,write_timeout=3) as port:
        port.dtr=True;port.rts=False;port.port=args.port;port.open();port.reset_input_buffer()
        if args.init_empty:
            port.write(b'evidence-init-empty\n');port.flush()
            for line in receive(port,30):
                if line.startswith('EVIDENCE_STATUS '):break
            else:raise TimeoutError('Initialization response not received')
        lines=[]
        state,events,mirror=export(port,lines)
        count,size,sha=log_digest(lines)
        args.output.mkdir(parents=True,exist_ok=True)
        stem='evidence-'+uuid.uuid4().hex
        path=args.output/f'{stem}.jsonl'
        # The lines exactly as the XIAO sent them (earlier exports were re-serialized by json.dumps).
        path.write_bytes(''.join(line+'\n' for line in lines).encode('utf-8'))
        meta=dict(schema='bll.evidence_export.v1',received_at_pc=datetime.now(timezone.utc).isoformat(),
            delivery_mode='SYNC',device_state=state,host_mirror=mirror,events_file=path.name,
            events_file_format='DEVICE_LINES_VERBATIM',log_events=count,log_bytes=size,log_sha256=sha)
        write_meta(path,meta)
        print(json.dumps(state,indent=2,ensure_ascii=False));print(f'SAVED {path.resolve()} events={count} sha256={sha}')
        if args.rotate:
            if not saved_matches(path,lines,state):
                raise ValueError('Saved export does not match the device log; nothing was rotated')
            done=rotate(port,lines)
            meta['rotation']=dict(requested_at_pc=datetime.now(timezone.utc).isoformat(),response=done,
                                  flash_backup=str(args.flash_backup.resolve()))
            write_meta(path,meta);print(f'ROTATED {done}')
    if args.rotate:
        time.sleep(5)  # the XIAO restarts; USB CDC re-enumerates
        with serial.Serial(port=None,baudrate=115200,timeout=.25,write_timeout=3) as port:
            port.dtr=True;port.rts=False;port.port=args.port;port.open();port.reset_input_buffer()
            after=status(port)
        meta['rotation']['device_state_after']=after;write_meta(path,meta)
        print(json.dumps(after,indent=2,ensure_ascii=False))


if __name__=='__main__':main()
