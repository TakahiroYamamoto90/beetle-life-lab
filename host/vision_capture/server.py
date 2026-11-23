"""Loopback-only image collection UI; stdlib server plus existing pyserial."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sys
import threading
import time
from urllib.parse import urlsplit
import webbrowser

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from capture_vision import capture, capture_classified
from piezo_capture import PiezoCaptureManager
from serial.tools import list_ports
from session_store import SessionStore

TRAINING_SETS = ROOT / "ml" / "vision" / "model_training_sets.json"


def load_training_sets(path=TRAINING_SETS, base=ROOT):
    """image_id -> models trained on it, image_id -> models whose frozen dataset manifest
    lists it (any split), plus each model's status.

    A missing manifest leaves its images unmarked; the UI reports that as unknown."""
    trained, frozen, models = {}, {}, []
    try:
        registry = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return trained, frozen, models
    for entry in registry.get("models", []):
        manifest = Path(base) / entry["training_manifest"]
        try:
            images = json.loads(manifest.read_text(encoding="utf-8"))["images"]
        except (OSError, ValueError, KeyError):
            models.append({"model": entry["model"], "status": "MANIFEST_MISSING", "images": 0})
            continue
        count = 0
        for image in images:
            frozen.setdefault(image["image_id"], []).append(entry["model"])
            if image.get("split") == "train":
                trained.setdefault(image["image_id"], []).append(entry["model"])
                count += 1
        models.append({"model": entry["model"], "status": "OK", "images": count})
    return trained, frozen, models


class App:
    def __init__(self, data_root, port_name=None, training_sets=TRAINING_SETS, training_base=ROOT):
        self.store = SessionStore(data_root)
        self.trained, self.frozen, self.training_models = load_training_sets(training_sets, training_base)
        self.port_name = port_name
        self.token = secrets.token_urlsafe(32)
        self.serial_lock = threading.Lock()
        self.piezo = PiezoCaptureManager(self.store.root, self.serial_lock)
        self.shutdown_event = threading.Event()
        self.previews = {}
        self.preview_lock = threading.Lock()

    def ports(self):
        return [{"port": p.device, "name": p.description}
                for p in list_ports.comports() if p.vid in (0x303A, 0x2886, 0x2341)]

    def capture_one(self, data):
        if not self.serial_lock.acquire(blocking=False):
            raise BlockingIOError("MCUのシリアルポートを別の処理が使用中です")
        try:
            mode = data.get("mode")
            if mode not in {"preview", "save", "classify_save"}:
                raise ValueError("mode must be preview, save, or classify_save")
            ports = self.ports()
            port_name = self.port_name or data.get("port")
            if not port_name:
                if len(ports) != 1:
                    raise ValueError("MCUのポートを選択してください")
                port_name = ports[0]["port"]
            if port_name not in [p["port"] for p in ports]:
                raise ValueError("選択したMCUポートが見つかりません")
            session_id = data.get("session_id")
            if mode in {"save", "classify_save"}:
                self.store.session(session_id)
                destination = self.store.root / "sessions" / session_id
            else:
                destination = self.store.root / "previews"
            # The server console keeps diagnostics; base64 is never logged here.
            path = (capture_classified(port_name, destination) if mode == "classify_save"
                    else capture(port_name, destination))
            if mode in {"save", "classify_save"}:
                return self.store.add_capture(session_id, path)
            with self.preview_lock:
                self.previews[path.stem] = path
            metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            return {"id": path.stem, "image_url": "/api/images/" + path.stem,
                    "metadata": metadata, "label": "unreviewed", "preview": True}
        finally:
            self.serial_lock.release()

    def start_piezo(self, data):
        ports = self.ports()
        port_name = self.port_name or data.get("port")
        if not port_name:
            if len(ports) != 1:
                raise ValueError("MCUのポートを選択してください")
            port_name = ports[0]["port"]
        if port_name not in [item["port"] for item in ports]:
            raise ValueError("選択したMCUポートが見つかりません")
        session = self.store.session(data.get("session_id")) if data.get("session_id") else None
        return self.piezo.start(port_name, data, session)

    def close(self):
        self.shutdown_event.set()
        state = self.piezo.get_state()
        if state.get("running"):
            try:
                self.piezo.stop()
            except ValueError:
                pass
        if self.piezo.thread and self.piezo.thread.is_alive():
            self.piezo.thread.join(timeout=10)

    def trained_in_case(self, case_id):
        # Images count under their current case (after case corrections).
        return sorted({model for sample in self.store.samples() if sample["case_id"] == case_id
                       for model in self.trained.get(sample["id"], [])})

    def assign_case(self, data):
        """Move images to another / a new case. A trained image may not enter an evaluation case."""
        target = data.get("case_id")
        if isinstance(target, str):
            role = next((c["role"] for c in self.store.cases() if c["case_id"] == target.strip()), "unassigned")
            ids = data.get("sample_ids") if isinstance(data.get("sample_ids"), list) else []
            trained = sorted({m for i in ids if isinstance(i, str) for m in self.trained.get(i, [])})
            if role == "evaluation" and trained:
                raise ValueError(f"選んだ画像に {', '.join(trained)} の学習に使った画像があるため、評価用ケースへは移せません")
        return self.store.assign_case(data)

    def delete_session(self, data):
        """Move a session to the trash. Refused while it is recording, or when a model's frozen
        dataset manifest lists its images (that model's train/val/test could no longer be rebuilt)."""
        session_id = data.get("session_id")
        self.store.session(session_id)
        piezo = self.piezo.get_state()
        if piezo.get("running") and piezo.get("session_id") == session_id:
            raise ValueError("このセッションで圧電計測中のため削除できません")
        models = sorted({m for s in self.store.samples(session_id) for m in self.frozen.get(s["id"], [])})
        if models:
            raise ValueError(f"このセッションの画像は {', '.join(models)} のデータセット（学習・評価）に使われているため削除できません")
        return self.store.set_session_deleted(session_id, True, data)

    def set_case_profile(self, data):
        case_id = data.get("case_id")
        if data.get("role") == "evaluation":
            models = self.trained_in_case(case_id)
            if models:
                raise ValueError(f"このケースには {', '.join(models)} の学習に使った画像があるため、評価用にできません")
        return self.store.set_case_profile(case_id, data)

    def set_dataset_role(self, data):
        sample_id = data.get("sample_id")
        if data.get("role") == "evaluation_only" and self.trained.get(sample_id):
            raise ValueError(f"この画像は {', '.join(self.trained[sample_id])} の学習に使ったため、評価専用にできません")
        return self.store.set_dataset_role(sample_id, data)

    def state(self):
        return {"ports": self.ports(), "sessions": self.store.sessions(), "cases": self.store.cases(),
                "deleted_sessions": self.store.deleted_sessions(),
                "samples": self.store.samples(), "busy": self.serial_lock.locked(),
                "piezo": self.piezo.get_state(), "trained": self.trained,
                "training_models": self.training_models}

    def image_path(self, capture_id):
        # vision-<uuid>: collection captures. obs-<boot>-<n>: observation-loop frames
        # imported by observation_import.py (their XIAO action ID).
        if not re.fullmatch(r"vision-[0-9a-f]{32}|obs-[0-9a-f]{32}-[1-9][0-9]{0,9}", capture_id):
            raise ValueError("不正な画像IDです")
        with self.preview_lock:
            preview = self.previews.get(capture_id)
        if preview:
            return preview
        sample = self.store.sample(capture_id)
        return self.store.root / sample["image_path"]


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def send(self, status, body, content_type="application/json; charset=utf-8",
                 download=False, filename=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if download:
                filename = filename or "vision_dataset.jsonl"
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def allowed_host(self):
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, {"error": "Local host required"})
            route = urlsplit(self.path).path
            try:
                if route == "/":
                    page = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
                    return self.send(200, page.replace("__BLL_TOKEN__", app.token).encode(), "text/html; charset=utf-8")
                static = re.fullmatch(r"/static/((?:common|notes)/[a-z_]+\.js|notes\.css)", route)
                if static:
                    # Page modules (ES modules, host/web) and styles; a fixed pattern, so no path escapes.
                    path = Path(__file__).resolve().parents[1] / "web" / static[1]
                    if not path.is_file():
                        return self.send(404, {"error": "File not found"})
                    kind = "text/css" if path.suffix == ".css" else "text/javascript"
                    return self.send(200, path.read_bytes(), kind + "; charset=utf-8")
                if route == "/api/state":
                    return self.send(200, app.state())
                if route == "/api/piezo/stream":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache, no-transform")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    version = -1
                    heartbeat = time.monotonic()
                    try:
                        while not app.shutdown_event.is_set():
                            previous_version = version
                            version, update = app.piezo.wait_for_update(version, timeout=1.0)
                            if version != previous_version:
                                event = "samples" if update.get("points") else "state"
                                payload = json.dumps(update, ensure_ascii=False, separators=(",", ":"))
                                self.wfile.write(f"id: {version}\nevent: {event}\ndata: {payload}\n\n".encode())
                                self.wfile.flush()
                                heartbeat = time.monotonic()
                            elif time.monotonic() - heartbeat >= 10:
                                self.wfile.write(b": keep-alive\n\n")
                                self.wfile.flush()
                                heartbeat = time.monotonic()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                    return
                file_match = re.fullmatch(r"/api/piezo/files/(piezo-[0-9a-f]{32})\.(csv|json)", route)
                if file_match:
                    path = app.piezo.file_path(*file_match.groups())
                    content_type = "text/csv; charset=utf-8" if path.suffix == ".csv" else "application/json; charset=utf-8"
                    return self.send(200, path.read_bytes(), content_type,
                                     download=True, filename=path.name)
                if route.startswith("/api/images/"):
                    return self.send(200, app.image_path(route.rsplit("/", 1)[-1]).read_bytes(), "image/jpeg")
                if route in {"/api/export", "/api/classification-snapshot"}:
                    records = (app.store.classification_snapshot()
                               if route == "/api/classification-snapshot" else app.store.export())
                    body = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records)
                    filename = ("vision_classification_snapshot.jsonl"
                                if route == "/api/classification-snapshot" else "vision_dataset.jsonl")
                    return self.send(200, body.encode(), "application/x-ndjson; charset=utf-8", True, filename)
                return self.send(404, {"error": "Not found"})
            except (ValueError, FileNotFoundError) as exc:
                return self.send(404, {"error": str(exc)})

        def do_POST(self):
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if (not self.allowed_host() or self.headers.get("X-BLL-Token") != app.token or
                self.headers.get("Origin", origin) != origin):
                return self.send(403, {"error": "Local request token required"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                route = urlsplit(self.path).path
                # A case move lists up to 5000 image IDs; everything else stays small.
                if not 0 < length <= (512 * 1024 if route == "/api/case-assign" else 16384):
                    raise ValueError("Invalid request size")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("Expected JSON object")
                if route == "/api/sessions":
                    return self.send(201, app.store.create_session(data))
                if route == "/api/capture":
                    return self.send(201, app.capture_one(data))
                if route == "/api/piezo/start":
                    return self.send(202, app.start_piezo(data))
                if route == "/api/piezo/stop":
                    return self.send(202, app.piezo.stop())
                if route == "/api/annotate":
                    return self.send(200, app.store.annotate(data.get("sample_id"), data))
                if route == "/api/dataset-role":
                    return self.send(200, app.set_dataset_role(data))
                if route == "/api/case-profile":
                    return self.send(200, app.set_case_profile(data))
                if route == "/api/case-assign":
                    return self.send(200, app.assign_case(data))
                if route == "/api/session-delete":
                    return self.send(200, app.delete_session(data))
                if route == "/api/session-restore":
                    return self.send(200, app.store.set_session_deleted(data.get("session_id"), False, data))
                if route == "/api/session-title":
                    return self.send(200, app.store.set_session_title(data.get("session_id"), data))
                return self.send(404, {"error": "Not found"})
            except BlockingIOError as exc:
                return self.send(409, {"error": str(exc)})
            except (ValueError, TypeError) as exc:
                return self.send(400, {"error": str(exc)})
            except Exception as exc:
                print(f"Operation failed: {type(exc).__name__}: {exc}", flush=True)
                return self.send(503, {"error": f"操作に失敗しました: {exc}"})

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http-port", type=int, default=8765)
    parser.add_argument("--port", help="Use a fixed XIAO serial port")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "captures")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    app = App(args.data_dir, args.port)
    server = ThreadingHTTPServer(("127.0.0.1", args.http_port), make_handler(app))
    server.daemon_threads = False
    address = f"http://127.0.0.1:{server.server_port}"
    print(f"Beetle Life Lab collection: {address}", flush=True)
    print(f"Data: {app.store.root}; Ctrl+C to stop", flush=True)
    if args.open:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        server.server_close()


if __name__ == "__main__":
    main()
