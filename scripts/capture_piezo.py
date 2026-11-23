"""Capture a 10-second XIAO piezo ADC trace to CSV plus JSON metadata."""
import argparse
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
import uuid

import serial
from serial.tools import list_ports

from firmware_compat import require as require_firmware

ROOT = Path(__file__).resolve().parents[1]
DURATION_SECONDS = 10.0
SAMPLE_RATE_HZ_NOMINAL = 8000


def read_line(port):
    raw = port.readline()
    return raw.decode("utf-8", errors="replace").strip() if raw else ""


def read_complete_lines(port, pending):
    """Read a USB serial chunk and retain any line split across reads."""
    chunk = port.read(4096)
    if chunk:
        pending.extend(chunk)
    lines = []
    while True:
        newline = pending.find(b"\n")
        if newline < 0:
            break
        raw = bytes(pending[:newline]).rstrip(b"\r")
        del pending[:newline + 1]
        lines.append(raw.decode("ascii", errors="replace"))
    return lines


def wait_for_status(port, timeout_seconds=20):
    deadline = time.monotonic() + timeout_seconds
    status = None
    while time.monotonic() < deadline:
        line = read_line(port)
        if line.startswith("STATUS "):
            status = line
            break
    if status is None:
        # A connection may already have been considered open by USB CDC. Ask
        # for status once, then wait again rather than losing the first command.
        port.write(b"info\n")
        port.flush()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            line = read_line(port)
            if line.startswith("STATUS "):
                status = line
                break
    if status is None:
        raise TimeoutError("XIAO STATUS not received; close other serial monitors and retry")
    return status


def parse_status(line):
    return dict(part.split("=", 1) for part in line.split()[1:] if "=" in part)


def capture(port_name, condition, output_dir):
    capture_id = "piezo-" + uuid.uuid4().hex
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    stem = f"{stamp}_{condition}_{capture_id[-8:]}"
    csv_path = output_dir / f"{stem}.csv"
    metadata_path = output_dir / f"{stem}.json"
    rows = []
    active = False
    status_line = None
    started_utc = None
    elapsed_host = 0.0
    port = serial.Serial(port=None, baudrate=115200, timeout=0.2, write_timeout=2)
    port.dtr = True
    port.rts = False
    port.port = port_name
    try:
        port.open()
        status_line = wait_for_status(port)
        status = parse_status(status_line)
        require_firmware("piezo_capture", status.get("fw"))  # see firmware_compat.json

        port.write(f"adc-start {int(DURATION_SECONDS)}\n".encode("ascii"))
        port.flush()
        deadline = time.monotonic() + 15
        startup_lines = []
        while time.monotonic() < deadline:
            line = read_line(port)
            if line:
                startup_lines.append(line)
                startup_lines = startup_lines[-12:]
            if line.startswith("ADC_CSV_BEGIN"):
                active = True
                started_utc = datetime.now(timezone.utc).isoformat()
                began = time.monotonic()
                print(f"CAPTURE_STARTED condition={condition} duration_s={DURATION_SECONDS:g}", flush=True)
                break
            if line.startswith("ADC_ERROR") or line.startswith("ADC_BLOCKED") or line.startswith("ADC_ALREADY_RUNNING"):
                raise RuntimeError(f"XIAO refused capture: {line}")
        else:
            details = " | ".join(startup_lines) if startup_lines else "no device response after adc-start"
            raise TimeoutError(f"ADC_CSV_BEGIN not received within 15 s; received: {details}")

        transfer_deadline = began + DURATION_SECONDS + 90
        csv_started = True
        sample_count_reported = None
        pending = bytearray()
        finished = False
        while not finished:
            for line in read_complete_lines(port, pending):
                if line.startswith("ADC_CSV_END"):
                    end_fields = parse_status("STATUS " + line.removeprefix("ADC_CSV_END"))
                    sample_count_reported = int(end_fields.get("samples", "0"))
                    elapsed_host = time.monotonic() - began
                    active = False
                    finished = True
                    break
                if line:
                    match = re.fullmatch(r"(\d+),(\d+)", line)
                    if csv_started and match:
                        elapsed_us, raw12 = int(match.group(1)), int(match.group(2))
                        if elapsed_us <= int(DURATION_SECONDS * 1_000_000):
                            rows.append((elapsed_us, raw12))
            if time.monotonic() > transfer_deadline:
                raise TimeoutError("Timed out waiting for 10-second DMA capture and CSV transfer")
    finally:
        if active and port.is_open:
            try:
                port.write(b"adc-stop\n")
                port.flush()
            except serial.SerialException:
                pass
        if port.is_open:
            port.close()

    output_dir.mkdir(parents=True, exist_ok=True)
    with csv_path.open("x", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("elapsed_us", "raw12"))
        writer.writerows(rows)
    metadata = {
        "schema": "bll.piezo_capture.v1",
        "capture_id": capture_id,
        "condition": condition,
        "condition_note": "operator-assigned test condition; not a larva ground-truth label",
        "started_utc_pc": started_utc,
        "duration_s_requested": DURATION_SECONDS,
        "duration_s_host": round(elapsed_host, 3),
        "sample_rate_hz_nominal": SAMPLE_RATE_HZ_NOMINAL,
        "sample_count_expected_nominal": int(DURATION_SECONDS * SAMPLE_RATE_HZ_NOMINAL),
        "sample_count_reported_by_firmware": sample_count_reported,
        "sample_count": len(rows),
        "capture_status": "complete" if len(rows) == int(DURATION_SECONDS * SAMPLE_RATE_HZ_NOMINAL) else "partial",
        "firmware_version": parse_status(status_line).get("fw"),
        "device_status": status_line,
        "csv_file": csv_path.name,
        "csv_columns": ["elapsed_us", "raw12"],
        "measurement_note": "raw 12-bit ADC codes; elapsed_us is nominal 125-us sample spacing, not hardware timestamps; ADC voltage calibration and sample-clock accuracy are unverified",
    }
    with metadata_path.open("x", encoding="utf-8") as stream:
        json.dump(metadata, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"SAVED {csv_path.resolve()}", flush=True)
    print(f"SAVED {metadata_path.resolve()}", flush=True)
    print(f"SAMPLES {len(rows)} duration_s={elapsed_host:.3f}", flush=True)
    if len(rows) != int(DURATION_SECONDS * SAMPLE_RATE_HZ_NOMINAL):
        print("WARNING received sample count differs from the 80000-sample target", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition", required=True, choices=("quiet", "dummy-digging"),
                        help="Operator-assigned condition for comparing captures")
    parser.add_argument("--port", help="XIAO COM port; auto-detects one Seeed/Espressif device")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "captures" / "piezo")
    args = parser.parse_args()
    port_name = args.port
    if not port_name:
        ports = [p.device for p in list_ports.comports() if p.vid in (0x303A, 0x2886, 0x2341)]
        if len(ports) != 1:
            parser.error("Specify --port when there is not exactly one XIAO candidate")
        port_name = ports[0]
    try:
        capture(port_name, args.condition, args.output_dir)
    except (OSError, serial.SerialException, TimeoutError, RuntimeError) as exc:
        parser.exit(2, f"CAPTURE_FAILED {exc}\n")


if __name__ == "__main__":
    main()
