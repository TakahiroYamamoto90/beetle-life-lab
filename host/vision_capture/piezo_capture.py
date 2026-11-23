"""Own the XIAO serial port while streaming piezo samples to disk and the UI."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import threading
import time
import uuid

import serial

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from firmware_compat import require as require_firmware

# Reference build for host mocks. Accepted builds live in scripts/firmware_compat.json.
FIRMWARE_VERSION = "0.6.1-piezo-streaming-8khz"
SAMPLE_RATE_HZ = 8000
MAX_DURATION_SECONDS = 0xFFFFFFFF
DISPLAY_BATCH_SAMPLES = 400


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_fields(line):
    return dict(part.split("=", 1) for part in line.split()[1:] if "=" in part)


class SerialLineReader:
    """Keep bytes across USB reads; CDC may split a line at any boundary."""
    def __init__(self, port):
        self.port = port
        self.pending = bytearray()

    def read_line(self, deadline):
        while time.monotonic() < deadline:
            newline = self.pending.find(b"\n")
            if newline >= 0:
                raw = bytes(self.pending[:newline + 1])
                del self.pending[:newline + 1]
                return raw.decode("ascii", errors="replace").strip()
            waiting = self.port.in_waiting
            chunk = self.port.read(min(waiting or 1, 4096))
            if chunk:
                self.pending.extend(chunk)
        return None


class PiezoCaptureManager:
    def __init__(self, data_root, serial_lock, serial_factory=None):
        self.root = Path(data_root).resolve() / "piezo"
        self.serial_lock = serial_lock
        self.serial_factory = serial_factory or serial.Serial
        self.simulated = serial_factory is not None
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.thread = None
        self.version = 0
        self.snapshot = {
            "state": "idle", "running": False, "sample_rate_hz_nominal": SAMPLE_RATE_HZ,
            "samples_received": 0, "points": [], "capture_id": None,
        }

    def _publish(self, **updates):
        with self.condition:
            self.version += 1
            self.snapshot = {**self.snapshot, **updates, "version": self.version}
            self.condition.notify_all()

    def get_state(self):
        with self.condition:
            return dict(self.snapshot, points=[])

    def wait_for_update(self, after_version, timeout=1.0):
        with self.condition:
            if self.version == after_version:
                self.condition.wait_for(lambda: self.version != after_version, timeout)
            return self.version, dict(self.snapshot)

    @staticmethod
    def validate_request(data):
        condition = data.get("condition")
        if condition not in {"quiet", "dummy-digging", "setup"}:
            raise ValueError("condition must be quiet, dummy-digging, or setup")
        duration = data.get("duration_s")
        if duration in (None, ""):
            duration = None
        elif isinstance(duration, bool) or not isinstance(duration, int):
            raise ValueError("duration_s must be a positive integer or null")
        elif not 1 <= duration <= MAX_DURATION_SECONDS:
            raise ValueError("duration_s must be between 1 and 4294967295")
        note = data.get("note", "")
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError("note must be a string of at most 1000 characters")
        session_id = data.get("session_id") or None
        if session_id is not None and (not isinstance(session_id, str) or
                                      not re.fullmatch(r"session-[0-9a-f]{32}", session_id)):
            raise ValueError("invalid session_id")
        return condition, duration, note.strip(), session_id

    def start(self, port_name, data, session=None):
        condition, duration, note, session_id = self.validate_request(data)
        if not isinstance(port_name, str) or not re.fullmatch(r"COM[0-9]+", port_name, re.IGNORECASE):
            raise ValueError("select a valid XIAO serial port")
        with self.condition:
            if self.snapshot.get("running"):
                raise BlockingIOError("ピエゾ取得中です")
            if not self.serial_lock.acquire(blocking=False):
                raise BlockingIOError("MCUのシリアルポートを別の処理が使用中です")
            capture_id = "piezo-" + uuid.uuid4().hex
            self.stop_event.clear()
            self.snapshot = {
                "state": "starting", "running": True,
                "sample_rate_hz_nominal": SAMPLE_RATE_HZ,
                "duration_s_requested": duration,
                "samples_received": 0, "points": [], "capture_id": capture_id,
                "condition": condition, "session_id": session_id,
                "csv_url": None, "metadata_url": None, "error": None,
            }
            self.version += 1
            self.snapshot["version"] = self.version
            self.condition.notify_all()
            self.thread = threading.Thread(
                target=self._run,
                args=(port_name, capture_id, condition, duration, note, session),
                name="piezo-serial-reader", daemon=True)
            try:
                self.thread.start()
            except Exception:
                self.snapshot["running"] = False
                self.snapshot["state"] = "error"
                self.serial_lock.release()
                raise
        return self.get_state()

    def stop(self):
        with self.condition:
            if not self.snapshot.get("running"):
                raise ValueError("ピエゾ取得は実行中ではありません")
            self.stop_event.set()
            self._publish(state="stopping")
            return dict(self.snapshot, points=[])

    def file_path(self, capture_id, suffix, condition=None):
        if not re.fullmatch(r"piezo-[0-9a-f]{32}", capture_id):
            raise ValueError("不正な計測IDです")
        if suffix not in {"csv", "json"}:
            raise ValueError("不正なファイル形式です")
        if condition is not None:
            if condition not in {"quiet", "dummy-digging", "setup"}:
                raise ValueError("不正な取得条件です")
            return self.root / f"{capture_id}_{condition}.{suffix}"
        legacy_path = self.root / f"{capture_id}.{suffix}"
        if legacy_path.exists():
            return legacy_path
        matches = [self.root / f"{capture_id}_{value}.{suffix}"
                   for value in ("quiet", "dummy-digging", "setup")
                   if (self.root / f"{capture_id}_{value}.{suffix}").exists()]
        if len(matches) > 1:
            raise ValueError("計測IDに複数の取得条件が対応しています")
        return matches[0] if matches else legacy_path

    def _run(self, port_name, capture_id, condition, duration, note, session):
        started = utc_now()
        began = time.monotonic()
        sample_count = 0
        total_sq = 0
        minimum = None
        maximum = None
        pending_points = []
        csv_path = self.file_path(capture_id, "csv", condition)
        json_path = self.file_path(capture_id, "json", condition)
        firmware_status = None
        end_fields = {}
        errors = []
        fw = None
        port = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            port = self.serial_factory(port=None, baudrate=115200, timeout=0.1, write_timeout=2)
            port.dtr = True
            port.rts = False
            port.port = port_name
            port.open()
            reader = SerialLineReader(port)
            port.write(b"info\n")
            port.flush()
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                line = reader.read_line(deadline)
                if line and line.startswith("STATUS "):
                    firmware_status = line
                    break
            if not firmware_status:
                raise TimeoutError("MCU STATUSを受信できませんでした")
            fw = parse_fields(firmware_status).get("fw")
            require_firmware("piezo_capture", fw)  # read on every capture; no server restart needed

            command = "adc-start" if duration is None else f"adc-start {duration}"
            port.write((command + "\n").encode("ascii"))
            port.flush()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                line = reader.read_line(deadline)
                if line and line.startswith("ADC_CSV_BEGIN"):
                    break
                if line and line.startswith(("ADC_ERROR", "ADC_BLOCKED", "ERROR ")):
                    raise RuntimeError(line)
            else:
                raise TimeoutError("ADC_CSV_BEGINを受信できませんでした")

            self._publish(state="capturing", started_utc_pc=started)
            with csv_path.open("x", encoding="ascii", newline="") as stream:
                stream.write("elapsed_us,raw12\n")
                stream.flush()
                end_deadline = time.monotonic() + (duration or 0) + 120
                stop_sent = False
                while time.monotonic() < end_deadline:
                    if self.stop_event.is_set() and not stop_sent:
                        port.write(b"adc-stop\n")
                        port.flush()
                        stop_sent = True
                    line = reader.read_line(min(end_deadline, time.monotonic() + 1))
                    if line is None:
                        continue
                    if line.startswith("ADC_CSV_END"):
                        end_fields = parse_fields(line)
                        break
                    if line.startswith("ADC_ERROR"):
                        errors.append(line)
                        self._publish(state="error", error=line)
                        continue
                    match = re.fullmatch(r"([0-9]+),([0-9]+)", line)
                    if not match:
                        continue
                    elapsed_us, raw = int(match.group(1)), int(match.group(2))
                    if raw > 4095:
                        errors.append("ADC value exceeded 12-bit range")
                        continue
                    stream.write(f"{elapsed_us},{raw}\n")
                    sample_count += 1
                    total_sq += raw * raw
                    minimum = raw if minimum is None else min(minimum, raw)
                    maximum = raw if maximum is None else max(maximum, raw)
                    pending_points.append([elapsed_us, raw])
                    if len(pending_points) >= DISPLAY_BATCH_SAMPLES:
                        stream.flush()
                        self._publish(
                            state="capturing", samples_received=sample_count,
                            elapsed_us=elapsed_us, minimum=minimum, maximum=maximum,
                            rms=round((total_sq / sample_count) ** 0.5, 2),
                            points=pending_points)
                        pending_points = []
                else:
                    raise TimeoutError("ADC_CSV_ENDを待機中に時間切れになりました")
                if pending_points:
                    self._publish(points=pending_points, samples_received=sample_count,
                                  elapsed_us=pending_points[-1][0], minimum=minimum,
                                  maximum=maximum,
                                  rms=round((total_sq / sample_count) ** 0.5, 2))
                    pending_points = []
                stream.flush()

            reason = end_fields.get("reason", "unknown")
            firmware_sample_count = int(end_fields.get("samples", "0"))
            if firmware_sample_count != sample_count:
                errors.append(f"firmware reported {firmware_sample_count} samples; host saved {sample_count}")
            complete = reason in {"stopped", "duration_reached"} and not errors
            ended = utc_now()
            metadata = {
                "schema": "bll.piezo_capture.v1", "capture_id": capture_id,
                "condition": condition,
                "condition_note": "operator-assigned measurement condition; not a larva ground-truth label",
                "note": note, "session_id": session.get("id") if session else None,
                "box_id": session.get("box_id") if session else None,
                "started_utc_pc": started, "ended_utc_pc": ended,
                "duration_s_requested": duration,
                "duration_s_host": round(time.monotonic() - began, 3),
                "sample_rate_hz_nominal": SAMPLE_RATE_HZ,
                "sample_count_expected_nominal": duration * SAMPLE_RATE_HZ if duration else None,
                "sample_count_reported_by_firmware": firmware_sample_count,
                "sample_count": sample_count,
                "capture_status": "complete" if complete else "partial",
                "stop_reason": reason, "errors": errors,
                "firmware_version": fw, "device_status": firmware_status,
                "simulated": self.simulated,
                "source": "host_mock" if self.simulated else "xiao_usb_serial_adc_dma",
                "csv_file": csv_path.name, "csv_columns": ["elapsed_us", "raw12"],
                "measurement_note": "12-bit raw ADC codes; elapsed_us is nominal 125-us spacing, not hardware timestamps; voltage and effective sample clock are uncalibrated",
            }
            json_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                                 encoding="utf-8")
            self._publish(
                state="complete" if complete else "partial", running=False,
                samples_received=sample_count, firmware_sample_count=metadata["sample_count_reported_by_firmware"],
                duration_s_host=metadata["duration_s_host"], stop_reason=reason,
                csv_url=f"/api/piezo/files/{capture_id}.csv",
                metadata_url=f"/api/piezo/files/{capture_id}.json",
                error="; ".join(errors) if errors else None, points=[])
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            if port is not None and port.is_open:
                try:
                    port.write(b"adc-stop\n")
                    port.flush()
                except (serial.SerialException, OSError):
                    pass
            self._publish(state="error", running=False, error=message,
                          samples_received=sample_count, points=pending_points)
            try:
                if not json_path.exists():
                    metadata = {
                        "schema": "bll.piezo_capture.v1", "capture_id": capture_id,
                        "condition": condition, "note": note,
                        "session_id": session.get("id") if session else None,
                        "box_id": session.get("box_id") if session else None,
                        "started_utc_pc": started, "ended_utc_pc": utc_now(),
                        "duration_s_requested": duration,
                        "sample_rate_hz_nominal": SAMPLE_RATE_HZ,
                        "sample_count": sample_count, "capture_status": "partial",
                        "errors": [message], "firmware_version": fw,
                        "device_status": firmware_status,
                        "simulated": self.simulated,
                        "source": "host_mock" if self.simulated else "xiao_usb_serial_adc_dma",
                        "csv_file": csv_path.name if csv_path.exists() else None,
                        "csv_columns": ["elapsed_us", "raw12"],
                    }
                    json_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")
                    self._publish(
                        csv_url=f"/api/piezo/files/{capture_id}.csv" if csv_path.exists() else None,
                        metadata_url=f"/api/piezo/files/{capture_id}.json")
            except OSError:
                pass
        finally:
            if port is not None and port.is_open:
                port.close()
            self.serial_lock.release()

