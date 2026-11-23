"""Host mock coverage for piezo serial handling; no fixture is real sensor data."""
import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import tempfile
import threading
import unittest

from piezo_capture import FIRMWARE_VERSION, PiezoCaptureManager


class FakeSerial:
    def __init__(self, port=None, **_kwargs):
        self.port = port
        self.dtr = False
        self.rts = False
        self.is_open = False
        self.buffer = bytearray()
        self.commands = []

    @property
    def in_waiting(self):
        return len(self.buffer)

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def flush(self):
        pass

    def write(self, data):
        self.commands.append(data)
        if data == b"info\n":
            self.buffer.extend(f"STATUS fw={FIRMWARE_VERSION} chip=ESP32-S3\n".encode())
        elif data == b"adc-start 1\n":
            self.buffer.extend(b"ADC_CSV_BEGIN fields=elapsed_us,raw12 sample_rate_hz=8000 duration_s=1 timing=nominal\n")
            for index in range(8000):
                self.buffer.extend(f"{index * 125},{index % 4096}\n".encode())
            self.buffer.extend(b"ADC_CSV_END samples=8000 duration_us=1000000 reason=duration_reached\n")
        return len(data)

    def read(self, size):
        count = min(size, len(self.buffer))
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result


class PiezoCaptureTests(unittest.TestCase):
    def test_evidence_firmware_retains_adc_capture_contract(self):
        class EvidenceFirmware(FakeSerial):
            def write(self,data):
                if data==b"info\n":
                    self.commands.append(data)
                    self.buffer.extend(b"STATUS fw=0.7.2-evidence-littlefs chip=ESP32-S3 loop_stack_min_free_bytes=5120\n")
                    return len(data)
                return super().write(data)
        with tempfile.TemporaryDirectory() as temporary:
            manager=PiezoCaptureManager(temporary,threading.Lock(),serial_factory=EvidenceFirmware)
            manager.start("COM3",{"condition":"quiet","duration_s":1})
            manager.thread.join(timeout=5)
            self.assertFalse(manager.thread.is_alive())
            self.assertEqual(manager.get_state()["state"],"complete")
            self.assertEqual(manager.get_state()["samples_received"],8000)

    def test_duration_is_optional_but_when_set_must_be_positive_integer(self):
        condition, duration, note, session_id = PiezoCaptureManager.validate_request(
            {"condition": "quiet", "duration_s": None})
        self.assertEqual((condition, duration, note, session_id), ("quiet", None, "", None))
        for duration in (0, -1, 1.5, True, 4294967296):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                PiezoCaptureManager.validate_request({"condition": "quiet", "duration_s": duration})

    def test_mock_serial_capture_saves_complete_csv_and_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            serial_lock = threading.Lock()
            fake_ports = []

            def factory(**kwargs):
                port = FakeSerial(**kwargs)
                fake_ports.append(port)
                return port

            manager = PiezoCaptureManager(temporary, serial_lock, serial_factory=factory)
            session = {"id": "session-" + "1" * 32, "box_id": "fixture-box"}
            started = manager.start(
                "COM3", {"condition": "quiet", "duration_s": 1,
                         "session_id": session["id"]}, session)
            manager.thread.join(timeout=5)
            self.assertFalse(manager.thread.is_alive())
            self.assertFalse(serial_lock.locked())
            result = manager.get_state()
            self.assertEqual(result["state"], "complete")
            self.assertEqual(result["samples_received"], 8000)
            capture_id = started["capture_id"]
            csv_path = manager.file_path(capture_id, "csv")
            metadata_path = manager.file_path(capture_id, "json")
            self.assertEqual(csv_path.name, f"{capture_id}_quiet.csv")
            self.assertEqual(metadata_path.name, f"{capture_id}_quiet.json")
            with csv_path.open(encoding="ascii") as stream:
                self.assertEqual(sum(1 for _ in stream) - 1, 8000)
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            self.assertEqual(metadata["csv_file"], csv_path.name)
            self.assertTrue(metadata["simulated"])
            self.assertEqual(metadata["source"], "host_mock")
            self.assertEqual(metadata["session_id"], session["id"])
            self.assertEqual(metadata["box_id"], "fixture-box")
            self.assertEqual(fake_ports[0].commands[:2], [b"info\n", b"adc-start 1\n"])

    def test_active_serial_lock_rejects_piezo_start(self):
        lock = threading.Lock()
        manager = PiezoCaptureManager(".", lock, serial_factory=FakeSerial)
        lock.acquire()
        self.addCleanup(lock.release)
        with self.assertRaises(BlockingIOError):
            manager.start("COM3", {"condition": "setup"})

    def test_condition_filenames_and_legacy_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = PiezoCaptureManager(temporary, threading.Lock(), serial_factory=FakeSerial)
            manager.root.mkdir(parents=True)
            capture_id = "piezo-" + "a" * 32
            for condition in ("quiet", "dummy-digging", "setup"):
                for suffix in ("csv", "json"):
                    path = manager.file_path(capture_id, suffix, condition)
                    self.assertEqual(path.name, f"{capture_id}_{condition}.{suffix}")
                    path.write_text("fixture", encoding="ascii")
                    self.assertEqual(manager.file_path(capture_id, suffix), path)
                    path.unlink()
            legacy = manager.root / f"{capture_id}.csv"
            legacy.write_text("fixture", encoding="ascii")
            self.assertEqual(manager.file_path(capture_id, "csv"), legacy)
            with self.assertRaises(ValueError):
                manager.file_path(capture_id, "csv", "../quiet")

    def test_local_http_start_stream_state_and_download_routes(self):
        from server import App, make_handler

        with tempfile.TemporaryDirectory() as temporary:
            app = App(temporary)
            fake_ports = []

            def factory(**kwargs):
                port = FakeSerial(**kwargs)
                fake_ports.append(port)
                return port

            app.ports = lambda: [{"port": "COM3", "name": "host mock"}]
            app.piezo.serial_factory = factory
            app.piezo.simulated = True
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
            httpd.daemon_threads = True
            server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            server_thread.start()
            connection = HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
            try:
                connection.request("GET", "/api/state")
                state_response = connection.getresponse()
                state = json.loads(state_response.read())
                self.assertIn("piezo", state)

                body = json.dumps({"port": "COM3", "condition": "quiet", "duration_s": 1})
                connection.request("POST", "/api/piezo/start", body=body, headers={
                    "X-BLL-Token": app.token, "Content-Type": "application/json"})
                start_response = connection.getresponse()
                self.assertEqual(start_response.status, 202)
                started = json.loads(start_response.read())
                app.piezo.thread.join(timeout=5)
                self.assertFalse(app.piezo.thread.is_alive())

                capture_id = started["capture_id"]
                connection.request("GET", f"/api/piezo/files/{capture_id}.csv")
                file_response = connection.getresponse()
                self.assertEqual(file_response.status, 200)
                self.assertIn("attachment", file_response.getheader("Content-Disposition"))
                self.assertIn(f"{capture_id}_quiet.csv", file_response.getheader("Content-Disposition"))
                self.assertEqual(file_response.read().count(b"\n"), 8001)

                connection.request("GET", f"/api/piezo/files/{capture_id}.json")
                metadata_response = connection.getresponse()
                self.assertEqual(metadata_response.status, 200)
                self.assertIn(f"{capture_id}_quiet.json", metadata_response.getheader("Content-Disposition"))
                self.assertEqual(json.loads(metadata_response.read())["csv_file"], f"{capture_id}_quiet.csv")

                connection.request("GET", "/api/piezo/stream")
                stream_response = connection.getresponse()
                self.assertEqual(stream_response.getheader("Content-Type"), "text/event-stream; charset=utf-8")
                self.assertTrue(stream_response.readline().startswith(b"id:"))
            finally:
                connection.close()
                app.close()
                httpd.shutdown()
                httpd.server_close()
                server_thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
