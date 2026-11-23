"""Start/stop OBS Studio recording around an AD3 capture (obs-websocket v5, loopback only).

Video is a PC-only reference for human review: it never feeds the detector or the XIAO.
The password is read from the BLL_OBS_PASSWORD environment variable and is never stored.
Times are PC receipt times of OBS replies; aligning video to the AD3 sample clock to better
than a few hundred ms needs a sync tap visible in both.
"""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import struct
import time

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
OP_HELLO, OP_IDENTIFY, OP_IDENTIFIED, OP_REQUEST, OP_RESPONSE = 0, 1, 2, 6, 7
MAX_FRAME = 1 << 20


class ObsError(RuntimeError):
    pass


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def setting(name, environ=os.environ):
    """Environment value, else the saved Windows user/system variable.

    A process started from a terminal that predates `setx`/System Properties does not
    inherit newly saved variables, so read the registry as a fallback (Windows only).
    """
    value = environ.get(name)
    if value:
        return value
    try:
        import winreg
    except ImportError:
        return None
    for root, path in ((winreg.HKEY_CURRENT_USER, 'Environment'),
                       (winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')):
        try:
            with winreg.OpenKey(root, path) as key:
                value, _ = winreg.QueryValueEx(key, name)
                if value:
                    return str(value)
        except OSError:
            continue
    return None


def auth_string(password, salt, challenge):
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest()).decode()
    return base64.b64encode(hashlib.sha256((secret + challenge).encode()).digest()).decode()


class ObsClient:
    """Minimal synchronous obs-websocket v5 client: text frames, no extensions."""

    def __init__(self, host='127.0.0.1', port=4455, password=None, timeout=3.0):
        if host not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('OBS control is loopback only')
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self.sock = None
        self.buffer = b''

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise ObsError(f'OBSに接続できません（{self.host}:{self.port}）。OBSの起動とWebSocketサーバーの有効化を確認してください') from exc
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        self.sock.sendall((f'GET / HTTP/1.1\r\nHost: {self.host}:{self.port}\r\nUpgrade: websocket\r\n'
                           f'Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n'
                           'Sec-WebSocket-Protocol: obswebsocket.json\r\n\r\n').encode())
        while b'\r\n\r\n' not in self.buffer:
            self.buffer += self._recv_some()
            if len(self.buffer) > 16384:
                raise ObsError('Invalid OBS handshake')
        head, self.buffer = self.buffer.split(b'\r\n\r\n', 1)
        lines = head.decode('latin-1').split('\r\n')
        headers = {k.strip().lower(): v.strip() for k, v in (line.split(':', 1) for line in lines[1:] if ':' in line)}
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if ' 101 ' not in lines[0] + ' ' or headers.get('sec-websocket-accept') != accept:
            raise ObsError('OBS WebSocket handshake failed')
        hello = self._recv_json()
        if hello.get('op') != OP_HELLO:
            raise ObsError('Unexpected OBS greeting')
        identify = dict(rpcVersion=1, eventSubscriptions=0)
        auth = (hello.get('d') or {}).get('authentication')
        if auth:
            if not self.password:
                raise ObsError('OBSのパスワードが見つかりません（環境変数 BLL_OBS_PASSWORD をユーザーまたはシステムに設定）')
            identify['authentication'] = auth_string(self.password, auth['salt'], auth['challenge'])
        self._send_json(dict(op=OP_IDENTIFY, d=identify))
        reply = self._recv_json()
        if reply.get('op') != OP_IDENTIFIED:
            raise ObsError('OBS rejected identification')

    def request(self, request_type, data=None):
        request_id = secrets.token_hex(8)
        body = dict(requestType=request_type, requestId=request_id)
        if data:
            body['requestData'] = data
        self._send_json(dict(op=OP_REQUEST, d=body))
        while True:
            message = self._recv_json()
            if message.get('op') != OP_RESPONSE or (message.get('d') or {}).get('requestId') != request_id:
                continue  # no event subscriptions, but ignore anything unrelated
            d = message['d']
            status = d.get('requestStatus') or {}
            if not status.get('result'):
                raise ObsError(f"OBS {request_type} failed: code {status.get('code')} {status.get('comment') or ''}".strip())
            return d.get('responseData') or {}

    def close(self):
        if self.sock:
            try:
                self._send_frame(0x8, struct.pack('!H', 1000))
            except OSError:
                pass
            self.sock.close()
            self.sock = None

    def _recv_some(self):
        try:
            data = self.sock.recv(65536)
        except socket.timeout as exc:
            raise ObsError('OBS did not reply in time') from exc
        if not data:
            raise ObsError('OBS closed the connection (wrong password?)')
        return data

    def _need(self, n):
        while len(self.buffer) < n:
            self.buffer += self._recv_some()
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def _send_frame(self, opcode, payload):
        mask = secrets.token_bytes(4)
        n = len(payload)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack('!H', n)
        else:
            header += bytes([0x80 | 127]) + struct.pack('!Q', n)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _send_json(self, value):
        self._send_frame(0x1, json.dumps(value).encode())

    def _recv_json(self):
        message = b''
        while True:
            b0, b1 = self._need(2)
            opcode, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack('!H', self._need(2))[0]
            elif n == 127:
                n = struct.unpack('!Q', self._need(8))[0]
            if n > MAX_FRAME:
                raise ObsError('OBS frame too large')
            mask = self._need(4) if b1 & 0x80 else None
            payload = self._need(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise ObsError('OBS closed the connection')
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode in (0x1, 0x0):
                message += payload
                if b0 & 0x80:
                    return json.loads(message)


def visible_sources(obs):
    """(name, source width in px) of each enabled item in the program scene; 0 means no frames."""
    scene = obs.request('GetCurrentProgramScene')
    name = scene.get('sceneName') or scene.get('currentProgramSceneName')
    out = []
    for item in obs.request('GetSceneItemList', {'sceneName': name}).get('sceneItems', []):
        if not item.get('sceneItemEnabled'):
            continue
        transform = obs.request('GetSceneItemTransform', {'sceneName': name, 'sceneItemId': item['sceneItemId']})
        out.append((item.get('sourceName'), float((transform.get('sceneItemTransform') or {}).get('sourceWidth') or 0)))
    return out


class ObsRecorder:
    """One recording per capture. Refuses to take over a recording the user already started."""

    def __init__(self, client_factory=None):
        port = int(setting('BLL_OBS_PORT') or '4455')
        self.client_factory = client_factory or (lambda: ObsClient(port=port, password=setting('BLL_OBS_PASSWORD')))
        self.started = False

    def start(self):
        with self.client_factory() as obs:
            version = obs.request('GetVersion')
            sources = visible_sources(obs)
            if not any(width > 0 for _, width in sources):
                names = '、'.join(name for name, _ in sources) or 'なし'
                raise ObsError(f'OBSのシーンに映像が届いていません（ソース: {names}）。カメラの再選択やUSBの差し直しを確認してください（真っ黒の録画を防ぐため録画しません）')
            if obs.request('GetRecordStatus').get('outputActive'):
                raise ObsError('OBSはすでに録画中です。手動の録画を止めてから開始してください（この録画は操作しません）')
            sent = time.monotonic()
            sent_at = utc_now()
            obs.request('StartRecord')
            acked = time.monotonic()
            self.started = True
            return dict(status='RECORDING', start_requested_at_pc=sent_at, start_acknowledged_at_pc=utc_now(),
                        start_ack_monotonic_s=acked, start_round_trip_s=round(acked - sent, 4),
                        obs_version=version.get('obsVersion'), websocket_version=version.get('obsWebSocketVersion'))

    def stop(self):
        if not self.started:
            return {}
        with self.client_factory() as obs:
            sent_at = utc_now()
            data = obs.request('StopRecord')
        self.started = False
        return dict(stop_requested_at_pc=sent_at, stop_acknowledged_at_pc=utc_now(), output_path=data.get('outputPath'))


def collect_recording(source, directory, wait_s=20.0, poll_s=0.25, reserve_bytes=200 * 1024 * 1024):
    """Move a finished OBS file into the capture folder as video.<ext>; never lose the original.

    OBS may still be closing the file after StopRecord, so wait until the size is stable and a
    same-folder rename succeeds (Windows refuses to rename a file another process holds open).
    The copy is verified by size and SHA-256 before the original is deleted.
    """
    src = Path(source)
    dst = Path(directory) / ('video' + src.suffix.lower())
    deadline = time.monotonic() + wait_s
    last_size, held = None, None
    while True:
        try:
            size = src.stat().st_size
            if size > 0 and size == last_size:
                held = src.with_name(src.name + '.bll-moving')
                src.rename(held)
                break
            last_size = size
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise ObsError(f'録画ファイルを移動できません（OBSが使用中か見つかりません）: {src}')
        time.sleep(poll_s)
    try:
        if dst.exists():
            raise ObsError(f'移動先に既に動画があります: {dst}')
        if shutil.disk_usage(dst.parent).free < size + reserve_bytes:
            raise ObsError('計測フォルダの空き容量が足りません')
        partial = dst.with_name(dst.name + '.partial')
        digest = hashlib.sha256()
        with held.open('rb') as reader, partial.open('xb') as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b''):
                digest.update(chunk)
                writer.write(chunk)
        if partial.stat().st_size != size:
            partial.unlink()
            raise ObsError('動画のコピーが不完全でした')
        partial.replace(dst)
    except BaseException:
        held.rename(src)  # put the original back under its OBS name
        raise
    held.unlink()
    return dict(video_file=dst.name, original_path=str(src), bytes=size, sha256=digest.hexdigest(),
                moved_at_pc=utc_now())
