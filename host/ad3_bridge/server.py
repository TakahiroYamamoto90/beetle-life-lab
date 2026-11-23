"""Loopback AD3 waveform and candidate bridge. No hardware starts on page load."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from serial.tools import list_ports
from acquisition import WaveForms
from manager import BridgeManager
from review import ReviewStore
from collection_link import CollectionLink


def handler_for(manager, token, review=None, collection=None):
    if review is None:
        def busy_run():
            state = manager.snapshot()
            return 'ad3-' + state['run_id'] if state.get('running') and state.get('run_id') else None
        review = ReviewStore(manager.output, busy_run)

    class Handler(BaseHTTPRequestHandler):
        def local(self):
            return self.headers.get('Host') in {f'127.0.0.1:{self.server.server_port}',
                                               f'localhost:{self.server.server_port}'}

        def send(self, code, data, content_type='application/json; charset=utf-8'):
            if not isinstance(data, bytes):
                data = json.dumps(data, allow_nan=False).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

        def send_range(self, path, content_type):
            """Stream a file with HTTP Range support (the video element seeks with it)."""
            size = path.stat().st_size
            start, end = 0, size - 1
            wanted = re.fullmatch(r'bytes=(\d*)-(\d*)', self.headers.get('Range', '').strip())
            if wanted and (wanted[1] or wanted[2]):
                if wanted[1]:
                    start = int(wanted[1])
                    end = min(int(wanted[2]), size - 1) if wanted[2] else size - 1
                else:  # suffix range: the last N bytes
                    start = max(0, size - int(wanted[2]))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            else:
                self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            try:
                with path.open('rb') as stream:
                    stream.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = stream.read(min(262144, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            if not self.local():
                return self.send(403, {'error': 'Local host required'})
            route = urlsplit(self.path).path
            if route in ('/', '/review', '/episodes', '/demo', '/demo-prev', '/diary'):
                page = Path(__file__).with_name({'/': 'index.html', '/review': 'review.html', '/episodes': 'episodes.html',
                                                 '/demo': 'demo.html', '/demo-prev': 'demo_prev.html',  # comparison snapshot
                                                 '/diary': 'diary.html'}[route]).read_text(encoding='utf-8')
                return self.send(200, page.replace('__TOKEN__', token).encode(), 'text/html; charset=utf-8')
            if route in ('/evidence_groups.js', '/observation_view.js'):
                return self.send(200, Path(__file__).with_name(route[1:]).read_bytes(),
                                 'application/javascript; charset=utf-8')
            static = re.fullmatch(r'/static/((?:common|review|live|episodes|demo|demo_prev|diary)/[a-z_]+\.js|(?:review|live|episodes|demo|demo_prev|diary)\.css)', route)
            if static:
                # Page modules (ES modules) and styles; a fixed pattern, so no path escapes the folder.
                path = Path(__file__).resolve().parents[1] / 'web' / static[1]  # host/web, shared with 観察ノート
                if not path.is_file():
                    return self.send(404, {'error': 'File not found'})
                kind = 'text/css' if path.suffix == '.css' else 'text/javascript'
                return self.send(200, path.read_bytes(), kind + '; charset=utf-8')
            if route == '/demo_scenarios.json':
                # SYNTHETIC rule demonstrations generated by the firmware core tests.
                return self.send(200, Path(__file__).with_name('demo_scenarios.json').read_bytes())
            image = re.fullmatch(r'/files/(ad3-[0-9a-f]{32})/observations/(obs-[0-9a-f]{32}-[1-9][0-9]{0,9})\.(jpg|json)', route)
            if image:
                path = manager.output / image[1] / 'observations' / f'{image[2]}.{image[3]}'
                if not path.is_file():
                    return self.send(404, {'error': 'File not found'})
                return self.send(200, path.read_bytes(),
                                 'image/jpeg' if image[3] == 'jpg' else 'application/json; charset=utf-8')
            if route.startswith('/api/review/'):
                query = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}
                try:
                    if route == '/api/review/runs':
                        return self.send(200, dict(runs=review.runs()))
                    if route == '/api/review/trash':
                        return self.send(200, dict(runs=review.trash()))
                    if route == '/api/review/overview':
                        return self.send(200, review.overview(query.get('run')))
                    if route == '/api/review/wave':
                        return self.send(200, review.wave(query.get('run'), query.get('t0', 'x'), query.get('t1', 'x')))
                    if route == '/api/review/frame':
                        return self.send(200, review.frame(query.get('run'), query.get('v', 'x'), query.get('w', 480)),
                                         'image/jpeg')
                    if route == '/api/review/video':
                        return self.send_range(review.playable_video(query.get('run')), 'video/mp4')
                    if route == '/api/review/episodes':
                        data = review.episodes(query.get('run'))
                        # human photo labels of 観察ノート for this run's steps (shown apart from the image AI)
                        data['human_labels'] = {}
                        if collection is not None:
                            try:
                                labels = collection.diary_context().get('labels') or {}
                                ids = {s['action_id'] for e in data['episodes'] for s in e['steps'] if s.get('action_id')}
                                data['human_labels'] = {i: labels[i]['label'] for i in ids if i in labels}
                            except Exception:  # the page still works without them
                                pass
                        return self.send(200, data)
                    if route == '/api/review/diary':
                        data = review.diary()
                        if collection is not None:
                            try:
                                data['collection'] = collection.diary_context()
                            except Exception as exc:  # the diary still works without the 観察ノート labels
                                data['collection'] = dict(cases={}, labels={}, error=f'{type(exc).__name__}: {exc}')
                        else:
                            data['collection'] = dict(cases={}, labels={}, error='観察ノートとの連携は無効です')
                        return self.send(200, data)
                    if route == '/api/review/candidates':
                        return self.send(200, review.candidate_context(query.get('run')))
                    if route == '/api/review/audio':
                        return self.send(200, review.spectrogram(query.get('run'), query.get('v0'), query.get('v1'),
                                                                 query.get('cols', 1200)))
                except BlockingIOError as exc:
                    return self.send(409, {'error': str(exc)})
                except LookupError as exc:
                    return self.send(404, {'error': str(exc)})
                except NotImplementedError as exc:
                    return self.send(501, {'error': str(exc)})
                except (TypeError, ValueError) as exc:
                    return self.send(400, {'error': str(exc)})
                return self.send(404, {'error': 'Not found'})
            if route == '/api/state':
                return self.send(200, manager.snapshot())
            if route in ('/api/xiao/exports', '/api/xiao/exports/read'):
                # Saved PC copies of the XIAO log: read-only, no serial port, allowed during acquisition.
                try:
                    if route == '/api/xiao/exports':
                        return self.send(200, dict(exports=manager.saved_evidence_list()))
                    name = {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}.get('name')
                    return self.send(200, manager.read_saved_evidence(name))
                except LookupError as exc:
                    return self.send(404, {'error': str(exc)})
                except (TypeError, ValueError, UnicodeDecodeError) as exc:
                    return self.send(422, {'error': f'書き出しを読めません: {exc}'})
            if route == '/api/collection/context':
                if collection is None:
                    return self.send(200, dict(cases=[], last_case_id='', illuminations=[], last_illumination='',
                                               error='観察ノートとの連携は無効です'))
                try:
                    return self.send(200, collection.context())
                except Exception as exc:
                    return self.send(503, {'error': f'{type(exc).__name__}: {exc}'})
            if route == '/api/devices':
                if manager.snapshot()['running']:
                    return self.send(409, {'error': 'Cannot enumerate during acquisition'})
                try:
                    sdk = WaveForms()
                    devices = sdk.devices()
                    return self.send(200, dict(sdk_version=sdk.version, devices=devices,
                                              ports=[p.device for p in list_ports.comports() if p.vid in (0x303A, 0x2886, 0x2341)]))
                except Exception as exc:
                    return self.send(503, {'error': str(exc)})
            match = re.fullmatch(r'/files/(ad3-[0-9a-f]{32})/(samples\.f64|features\.csv|candidates\.jsonl|delivery\.jsonl|annotations\.jsonl|candidate_context\.jsonl|review_annotations\.jsonl|actuation\.jsonl|manifest\.json)', route)
            if match:
                path = manager.output / match[1] / match[2]
                if not path.is_file():
                    return self.send(404, {'error': 'File not found'})
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Disposition', f'attachment; filename="{match[1]}-{match[2]}"')
                self.send_header('Content-Length', str(path.stat().st_size))
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                try:
                    with path.open('rb') as stream:
                        for chunk in iter(lambda: stream.read(65536), b''):
                            self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            return self.send(404, {'error': 'Not found'})

        def do_POST(self):
            origin = f'http://127.0.0.1:{self.server.server_port}'
            if (not self.local() or self.headers.get('X-BLL-Token') != token
                    or self.headers.get('Origin', origin) != origin):
                return self.send(403, {'error': 'Local request token required'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    raise ValueError('Invalid request size')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('Expected object')
                route = urlsplit(self.path).path
                if route == '/api/start':
                    return self.send(202, manager.start(data))
                if route == '/api/annotate':
                    return self.send(200, manager.annotate(data.get('kind')))
                if route == '/api/review/annotate':
                    return self.send(200, review.annotate(data.get('run'), data))
                if route == '/api/review/status':
                    return self.send(200, review.set_review_status(data.get('run'), data))
                if route in ('/api/review/delete', '/api/review/restore'):
                    return self.send(200, review.set_trashed(data.get('run'), route.endswith('delete'), data))
                if route == '/api/stop':
                    return self.send(202, manager.stop())
                if route == '/api/xiao/evidence':
                    return self.send(200, manager.read_evidence(data.get('port')))
                if route == '/api/xiao/time-sync':
                    return self.send(200, manager.read_evidence(data.get('port'), synchronize=True))
                return self.send(404, {'error': 'Not found'})
            except BlockingIOError as exc:
                return self.send(409, {'error': str(exc)})
            except (TypeError, ValueError) as exc:
                return self.send(400, {'error': str(exc)})
            except Exception as exc:
                return self.send(503, {'error': str(exc)})

        def log_message(self, fmt, *args):
            if '/api/state' not in (args[0] if args else ''):
                super().log_message(fmt, *args)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--http-port', type=int, default=8766)
    parser.add_argument('--output', type=Path, default=ROOT / 'captures/ad3')
    parser.add_argument('--open', action='store_true')
    parser.add_argument('--collection-dir', type=Path, default=ROOT / 'captures',
                        help='観察ノート data directory (collection.sqlite3) for the automatic image import')
    parser.add_argument('--no-collection', action='store_true', help='do not import observation images')
    args = parser.parse_args()
    collection = None if args.no_collection else CollectionLink(args.collection_dir, args.output)
    manager = BridgeManager(args.output, collection_importer=collection.import_run if collection else None)
    server = ThreadingHTTPServer(('127.0.0.1', args.http_port),
                                 handler_for(manager, secrets.token_urlsafe(32), collection=collection))
    server.daemon_threads = True
    address = f'http://127.0.0.1:{server.server_port}/'
    print(f'AD3 bridge: {address}', flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        manager.stop()
        if manager.thread:
            manager.thread.join(timeout=25)
        server.server_close()


if __name__ == '__main__':
    main()
