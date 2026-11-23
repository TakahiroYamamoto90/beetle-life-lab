"""OBS recording control against a fake obs-websocket v5 server, and its capture integration."""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import socket
import struct
import sys
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from obs_record import GUID, ObsClient, ObsError, ObsRecorder, auth_string, collect_recording
from manager import BridgeManager

PASSWORD = 'test-pass'


class FakeObs:
    """Accepts one websocket session per connection; records requests; optional auth."""

    def __init__(self, recording=False, fail=None, camera_width=1280):
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen()
        self.port = self.server.getsockname()[1]
        self.requests, self.recording, self.fail = [], recording, fail or {}
        self.camera_width = camera_width
        self.salt, self.challenge = 'salt123', 'chal456'
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self.session, args=(conn,), daemon=True).start()

    @staticmethod
    def read_frame(conn):
        head = conn.recv(2)
        if len(head) < 2:
            return None, None
        n = head[1] & 0x7F
        if n == 126:
            n = struct.unpack('!H', conn.recv(2))[0]
        mask = conn.recv(4)
        data = b''
        while len(data) < n:
            data += conn.recv(n - len(data))
        return head[0] & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(data))

    @staticmethod
    def send_json(conn, value):
        data = json.dumps(value).encode()
        header = bytes([0x81]) + (bytes([len(data)]) if len(data) < 126 else bytes([126]) + struct.pack('!H', len(data)))
        conn.sendall(header + data)

    def session(self, conn):
        with conn:
            request = b''
            while b'\r\n\r\n' not in request:
                request += conn.recv(4096)
            key = [l.split(':', 1)[1].strip() for l in request.decode().split('\r\n') if l.lower().startswith('sec-websocket-key')][0]
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            conn.sendall(f'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                         f'Sec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: obswebsocket.json\r\n\r\n'.encode())
            self.send_json(conn, dict(op=0, d=dict(rpcVersion=1, authentication=dict(salt=self.salt, challenge=self.challenge))))
            op, data = self.read_frame(conn)
            if data is None:
                return  # client gave up before identifying (e.g. no password)
            identify = json.loads(data)['d']
            if identify.get('authentication') != auth_string(PASSWORD, self.salt, self.challenge):
                conn.sendall(bytes([0x88, 2]) + struct.pack('!H', 4009))
                return
            self.send_json(conn, dict(op=2, d=dict(negotiatedRpcVersion=1)))
            while True:
                op, data = self.read_frame(conn)
                if op is None or op == 8:
                    return
                d = json.loads(data)['d']
                kind = d['requestType']
                self.requests.append(kind)
                ok, out = True, {}
                if kind in self.fail:
                    ok = False
                elif kind == 'GetVersion':
                    out = dict(obsVersion='32.0.4', obsWebSocketVersion='5.6.3')
                elif kind == 'GetCurrentProgramScene':
                    out = dict(sceneName='Scene', currentProgramSceneName='Scene')
                elif kind == 'GetSceneItemList':
                    out = dict(sceneItems=[dict(sourceName='Camera', sceneItemId=1, sceneItemEnabled=True),
                                           dict(sourceName='Hidden', sceneItemId=2, sceneItemEnabled=False)])
                elif kind == 'GetSceneItemTransform':
                    out = dict(sceneItemTransform=dict(sourceWidth=self.camera_width))
                elif kind == 'GetRecordStatus':
                    out = dict(outputActive=self.recording)
                elif kind == 'StartRecord':
                    self.recording = True
                elif kind == 'StopRecord':
                    self.recording = False
                    out = dict(outputPath='C:/Videos/2026-11-13 10-00-00.mkv')
                # An unrelated event first: the client must skip it.
                self.send_json(conn, dict(op=5, d=dict(eventType='Noise')))
                self.send_json(conn, dict(op=7, d=dict(requestType=kind, requestId=d['requestId'],
                                                       requestStatus=dict(result=ok, code=100 if ok else 500,
                                                                          comment=None if ok else 'refused'),
                                                       responseData=out)))

    def close(self):
        self.server.close()


def recorder_for(fake, password=PASSWORD):
    return ObsRecorder(lambda: ObsClient(port=fake.port, password=password, timeout=2))


class ObsTest(unittest.TestCase):
    def test_start_stop_records_times_and_path_without_password(self):
        fake = FakeObs()
        try:
            rec = recorder_for(fake)
            started = rec.start()
            self.assertEqual(started['status'], 'RECORDING')
            self.assertEqual(started['obs_version'], '32.0.4')
            self.assertNotIn(PASSWORD, json.dumps(started))
            stopped = rec.stop()
            self.assertTrue(stopped['output_path'].endswith('.mkv'))
            self.assertEqual(fake.requests, ['GetVersion', 'GetCurrentProgramScene', 'GetSceneItemList',
                                             'GetSceneItemTransform', 'GetRecordStatus', 'StartRecord', 'StopRecord'])
            self.assertEqual(rec.stop(), {})  # second stop does nothing
        finally:
            fake.close()

    def test_wrong_password_existing_recording_and_refusal_are_errors(self):
        fake = FakeObs()
        try:
            with self.assertRaises(ObsError):
                recorder_for(fake, password='wrong').start()
            with self.assertRaises(ObsError):
                recorder_for(fake, password=None).start()
        finally:
            fake.close()
        dark = FakeObs(camera_width=0)  # camera selected but delivering no frames
        try:
            with self.assertRaises(ObsError) as caught:
                recorder_for(dark).start()
            self.assertIn('映像が届いていません', str(caught.exception))
            self.assertNotIn('StartRecord', dark.requests)
        finally:
            dark.close()
        busy = FakeObs(recording=True)
        try:
            with self.assertRaises(ObsError):
                recorder_for(busy).start()
            self.assertNotIn('StartRecord', busy.requests)  # a manual recording is never taken over
            self.assertNotIn('StopRecord', busy.requests)
        finally:
            busy.close()
        with self.assertRaises(ValueError):
            ObsClient(host='192.168.0.2')
        closed = socket.socket()
        closed.bind(('127.0.0.1', 0))
        port = closed.getsockname()[1]
        closed.close()
        with self.assertRaises(ObsError):
            ObsRecorder(lambda: ObsClient(port=port, password=PASSWORD, timeout=1)).start()


def fake_collector(source, directory):
    return dict(video_file='video.mkv', original_path=str(source), bytes=1, sha256='0' * 64, moved_at_pc='now')


def run_mock(video_factory, video=True, collector=fake_collector):
    root = ROOT / 'captures/test_tmp'
    root.mkdir(parents=True, exist_ok=True)
    output = root / ('ad3-obs-test-' + uuid.uuid4().hex)
    manager = BridgeManager(output, video_factory=video_factory, video_collector=collector)
    state = manager.start(dict(mode='mock', duration_s=1, video=video))
    manager.thread.join(timeout=60)
    manifest = json.loads((output / ('ad3-' + state['run_id']) / 'manifest.json').read_text(encoding='utf-8'))
    return output, manager, manifest


class CollectTest(unittest.TestCase):
    def test_moves_verified_copy_and_keeps_original_on_failure(self):
        root = ROOT / 'captures/test_tmp' / ('collect-' + uuid.uuid4().hex)
        (root / 'obs').mkdir(parents=True)
        (root / 'run').mkdir()
        try:
            src = root / 'obs' / '2026-11-13 10-00-00.mkv'
            data = bytes(range(256)) * 4096
            src.write_bytes(data)
            moved = collect_recording(src, root / 'run', wait_s=5, poll_s=.05)
            self.assertEqual(moved['video_file'], 'video.mkv')
            self.assertEqual((root / 'run' / 'video.mkv').read_bytes(), data)
            self.assertEqual(moved['sha256'], hashlib.sha256(data).hexdigest())
            self.assertFalse(src.exists())
            self.assertEqual(list((root / 'obs').iterdir()), [])
            # destination taken: the original must stay under its OBS name
            src.write_bytes(data)
            with self.assertRaises(ObsError):
                collect_recording(src, root / 'run', wait_s=5, poll_s=.05)
            self.assertEqual(src.read_bytes(), data)
            with self.assertRaises(ObsError):
                collect_recording(root / 'obs' / 'missing.mkv', root / 'run', wait_s=.3, poll_s=.05)
        finally:
            shutil.rmtree(root)


class SettingTest(unittest.TestCase):
    def test_process_environment_wins_and_missing_is_none(self):
        from obs_record import setting
        self.assertEqual(setting('BLL_OBS_TEST', {'BLL_OBS_TEST': 'x'}), 'x')
        self.assertIsNone(setting('BLL_OBS_DEFINITELY_UNSET_' + uuid.uuid4().hex, {}))


class CaptureVideoTest(unittest.TestCase):
    def cleanup(self, output):
        output.resolve().relative_to((ROOT / 'captures/test_tmp').resolve())
        shutil.rmtree(output)

    def test_capture_starts_and_stops_obs_and_keeps_running_without_it(self):
        fake = FakeObs()
        try:
            output, manager, manifest = run_mock(lambda: recorder_for(fake))
            try:
                video = manifest['video']
                self.assertEqual(video['status'], 'RECORDED')
                self.assertEqual(video['video_file'], 'video.mkv')
                self.assertTrue(video['output_path'].endswith('video.mkv'))
                self.assertTrue(video['original_path'].endswith('.mkv'))
                self.assertEqual(video['use'], 'HUMAN_REVIEW_ONLY')
                self.assertIn('recording_s_at_first_block', video)
                self.assertNotIn('start_ack_monotonic_s', video)
                self.assertTrue(manifest['complete'])
                self.assertFalse(fake.recording)
                self.assertEqual(manager.snapshot()['video']['status'], 'RECORDED')
            finally:
                self.cleanup(output)
        finally:
            fake.close()

        def unavailable():
            raise ObsError('OBSに接続できません')
        output, manager, manifest = run_mock(unavailable)
        try:
            self.assertEqual(manifest['video']['status'], 'UNAVAILABLE')
            self.assertTrue(manifest['complete'])  # capture is not failed by missing video
            self.assertEqual(manifest['errors'], [])
        finally:
            self.cleanup(output)

        def never():
            raise AssertionError('OBS must not be contacted when video is off')
        output, manager, manifest = run_mock(never, video=False)
        try:
            self.assertNotIn('video', manifest)
        finally:
            self.cleanup(output)
        with self.assertRaises(ValueError):
            BridgeManager(output).start(dict(mode='mock', duration_s=1, video='yes'))


if __name__ == '__main__':
    unittest.main()
