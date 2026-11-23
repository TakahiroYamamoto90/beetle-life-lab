"""Capture one Grove Vision V2 JPEG through XIAO USB CDC (requires pyserial)."""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import uuid

import serial
from serial.tools import list_ports

from firmware_compat import require as require_firmware
from vision_model import (KNOWN_MODEL_ARTIFACTS, MODEL_CLASSES, MODEL_NAME,  # noqa: F401
                          decode_info_reply, interpret_classes, verify_model)

MAX_LINE = 256 * 1024 + 128


def receive_lines(port, deadline):
    pending = bytearray()
    while time.monotonic() < deadline:
        pending.extend(port.read(min(port.in_waiting or 1, 4096)))
        while b"\n" in pending:
            line, _, rest = pending.partition(b"\n")
            pending = bytearray(rest)
            if len(line) > MAX_LINE:
                raise RuntimeError("Serial line exceeds image transfer limit")
            yield line.decode("utf-8", errors="strict").strip()
        if len(pending) > MAX_LINE:
            raise RuntimeError("Incomplete serial line exceeds transfer limit")
    raise TimeoutError("Timed out waiting for camera response")


def jpeg_dimensions(data):
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise ValueError(f"Missing JPEG SOI/EOI markers: bytes={len(data)}, "
                         f"head={data[:16].hex()}, tail={data[-16:].hex()}")
    position = 2
    while position < len(data):
        if data[position] != 0xFF:
            raise ValueError("Invalid JPEG marker")
        while position < len(data) and data[position] == 0xFF:
            position += 1
        marker = data[position]
        position += 1
        if marker == 0xDA:
            break
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            continue
        size = int.from_bytes(data[position:position + 2], "big")
        if size < 2 or position + size > len(data):
            raise ValueError("Truncated JPEG segment")
        if marker in (0xC0, 0xC1, 0xC2):
            if size < 8:
                raise ValueError("Truncated JPEG dimensions")
            height = int.from_bytes(data[position + 3:position + 5], "big")
            width = int.from_bytes(data[position + 5:position + 7], "big")
            if not width or not height:
                raise ValueError("Invalid JPEG dimensions")
            return width, height
        position += size
    raise ValueError("No supported JPEG SOF marker")


def capture(port_name, output_dir):
    capture_id = "vision-" + uuid.uuid4().hex
    started = datetime.now(timezone.utc).isoformat()
    began = time.monotonic()
    response_ok = False
    event = None
    status = None
    device_status = None
    with serial.Serial(port=None, baudrate=115200, timeout=0.25, write_timeout=3) as port:
        port.dtr = True
        port.rts = False
        port.port = port_name
        port.open()
        port.reset_input_buffer()
        port.write(b"info\n")
        for line in receive_lines(port, time.monotonic() + 5):
            if line.startswith("STATUS "):
                device_status = line
                print(line, flush=True)
                break
        fw = dict(part.split("=", 1) for part in device_status.split()[1:] if "=" in part).get("fw")
        # Accepted builds live in firmware_compat.json (read on every capture).
        require_firmware("vision_capture", fw)
        port.write(b"vision-sample\n")
        for line in receive_lines(port, time.monotonic() + 45):
            if line.startswith("VISION_SAMPLE_REPLY "):
                reply = json.loads(line.removeprefix("VISION_SAMPLE_REPLY "))
                if reply.get("name") != "SAMPLE" or reply.get("code") != 0:
                    raise RuntimeError("Camera returned an unsuccessful sample response")
                if reply.get("type") == 0:
                    response_ok = True
                elif reply.get("type") == 1:
                    if event is not None:
                        raise RuntimeError("Unexpected additional sample event")
                    event = reply
                    print("Received image event", flush=True)
            elif line.startswith("VISION_SAMPLE_DONE "):
                status = dict(part.split("=", 1) for part in line.split()[1:])
                print(line, flush=True)
                break
            elif line:
                print(line[:300], flush=True)
        if not response_ok or event is None or status is None or status.get("status") != "ok":
            raise RuntimeError("Capture failed; no image saved")
        if event.get("data", {}).get("count") != 1:
            raise RuntimeError("Expected one captured frame")
        # Confirm the serial command loop recovered after the large transfer.
        port.write(b"ping\n")
        for line in receive_lines(port, time.monotonic() + 5):
            if line == "pong":
                print("Serial recovery: pong", flush=True)
                break
    payload = base64.b64decode(event["data"]["image"], validate=True)
    image = payload
    padding = 0
    # This device firmware returns a zero-padded JPEG buffer. Retain the
    # exact decoded payload and trim only verified zero bytes after JPEG EOI.
    if not image.endswith(b"\xff\xd9"):
        end = image.rfind(b"\xff\xd9")
        if end >= 0 and not any(image[end + 2:]):
            padding = len(image) - end - 2
            image = image[:end + 2]
    width, height = jpeg_dimensions(image)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / (capture_id + ".jpg")
    metadata = {
        "schema": "bll.capture_diagnostic.v1", "capture_id": capture_id,
        "source": "grove_vision_ai_v2_via_xiao_i2c_usb", "simulated": False,
        "request_sent_utc_pc": started,
        "received_utc_pc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds_pc": round(time.monotonic() - began, 3),
        "device_status": device_status, "sample_count": event["data"].get("count"),
        "image_file": path.name, "bytes": len(image), "width": width, "height": height,
        "sha256": hashlib.sha256(image).hexdigest(), "label": "unknown",
        "transport_bytes": len(payload), "trailing_zero_padding_bytes": padding,
        "transport_sha256": hashlib.sha256(payload).hexdigest(),
        "validation": "base64_strict_and_jpeg_markers_dimensions",
        "time_note": "PC timestamps bracket acquisition/transfer; camera exposure time is not synchronized",
    }
    with path.open("xb") as stream:
        stream.write(image)
    if padding:
        with path.with_suffix(".payload.bin").open("xb") as stream:
            stream.write(payload)
        metadata["transport_payload_file"] = path.with_suffix(".payload.bin").name
    with path.with_suffix(".json").open("x", encoding="utf-8") as stream:
        json.dump(metadata, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"SAVED {path.resolve()} ({width}x{height}, {len(image)} bytes)")
    return path


def _model_info(port, timeout=20):
    port.write(b"vision\n")
    replies = {}
    for line in receive_lines(port, time.monotonic() + timeout):
        if line.startswith("VISION_REPLY "):
            reply = json.loads(line.removeprefix("VISION_REPLY "))
            replies[reply.get("name")] = reply
        elif line == "VISION_INSPECT_DONE":
            break
    try:
        return verify_model(decode_info_reply(replies.get("INFO?")))
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


# Kept for existing callers; the shared implementation lives in vision_model.py.
_interpret_classes = interpret_classes


def capture_classified(port_name, output_dir):
    """Save the JPEG from the same SSCMA INVOKE event as its raw class result."""
    capture_id = "vision-" + uuid.uuid4().hex
    started = datetime.now(timezone.utc).isoformat()
    began = time.monotonic()
    with serial.Serial(port=None, baudrate=115200, timeout=0.25, write_timeout=3) as port:
        port.dtr = True
        port.rts = False
        port.port = port_name
        port.open()
        port.reset_input_buffer()
        port.write(b"info\n")
        device_status = None
        for line in receive_lines(port, time.monotonic() + 5):
            if line.startswith("STATUS "):
                device_status = line
                break
        if not device_status:
            raise RuntimeError("XIAO STATUS was not received")
        fw = dict(part.split("=", 1) for part in device_status.split()[1:] if "=" in part).get("fw")
        require_firmware("vision_capture", fw)
        model = _model_info(port)
        port.write(b"vision-classify-capture\n")
        event = None
        done = None
        for line in receive_lines(port, time.monotonic() + 45):
            if line.startswith("VISION_CLASSIFY_REPLY "):
                reply = json.loads(line.removeprefix("VISION_CLASSIFY_REPLY "))
                if reply.get("name") == "INVOKE" and reply.get("type") == 1:
                    event = reply
            elif line.startswith("VISION_CLASSIFY_DONE "):
                done = dict(part.split("=", 1) for part in line.split()[1:] if "=" in part)
                break
            elif line:
                print(line[:300], flush=True)
    if not done or done.get("status") != "ok" or not event or event.get("code") != 0:
        raise RuntimeError("Vision V2 did not return a classified image")
    data = event.get("data", {})
    if data.get("count") != 1:
        raise RuntimeError("Expected exactly one classified frame")
    encoded_image = data.get("image")
    if not isinstance(encoded_image, str):
        raise RuntimeError("The inference event did not include its source image")
    transport = base64.b64decode(encoded_image, validate=True)
    end = transport.rfind(b"\xff\xd9")
    if end < 0 or any(transport[end + 2:]):
        raise ValueError("Inference frame is not a complete JPEG with valid padding")
    image = transport[:end + 2]
    width, height = jpeg_dimensions(image)
    image_sha256 = hashlib.sha256(image).hexdigest()
    if done.get("image_sha256") != hashlib.sha256(transport).hexdigest():
        raise ValueError("XIAO and PC inference-frame payload hashes differ")
    if int(done.get("image_bytes", "-1")) != len(transport):
        raise ValueError("XIAO and PC inference-frame lengths differ")
    raw_classes = data.get("classes", [])
    interpretation = _interpret_classes(raw_classes, model["classes"])
    model_result = {
        "schema": "bll.vision_model_inference.v1",
        "model_name": model["name"], "model_version": model["version"],
        "model_artifact_sha256": model["artifact_sha256"],
        "same_frame_as_image": True, "image_sha256": image_sha256,
        "raw_classes_score_target": raw_classes,
        "prediction": interpretation,
        "interpretation_note": "Uncalibrated model output; does not set or replace the human image label.",
        "score_note": "Raw SSCMA integer score (0-100), not a calibrated probability.",
        "biological_absence_note": "NO_VISIBLE_LARVA means not visible in this frame; it does not mean the organism is absent.",
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / (capture_id + ".jpg")
    metadata = {
        "schema": "bll.capture_diagnostic.v1", "capture_id": capture_id,
        "source": "grove_vision_ai_v2_via_xiao_i2c_usb", "simulated": False,
        "request_sent_utc_pc": started,
        "received_utc_pc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds_pc": round(time.monotonic() - began, 3),
        "device_status": device_status, "sample_count": 1,
        "image_file": path.name, "bytes": len(image), "width": width, "height": height,
        "sha256": image_sha256, "label": "unknown",
        "transport_bytes": len(transport), "trailing_zero_padding_bytes": len(transport) - len(image),
        "transport_sha256": hashlib.sha256(transport).hexdigest(),
        "validation": "same_invoke_event_image_and_class_output_hash_and_jpeg_dimensions",
        "time_note": "PC timestamps bracket acquisition/transfer; camera exposure time is not synchronized",
        "model_inference": model_result,
    }
    with path.open("xb") as stream:
        stream.write(image)
    with path.with_suffix(".json").open("x", encoding="utf-8") as stream:
        json.dump(metadata, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(f"SAVED_CLASSIFIED {path.resolve()} ({width}x{height}, {len(image)} bytes)", flush=True)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="XIAO COM port; auto-detects one Seeed/Espressif device if omitted")
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "captures")
    parser.add_argument("--open", action="store_true", help="Open the saved JPEG in the Windows image viewer")
    args = parser.parse_args()
    port_name = args.port
    if not port_name:
        ports = [p.device for p in list_ports.comports() if p.vid in (0x303A, 0x2886, 0x2341)]
        if len(ports) != 1:
            parser.error("Specify --port when there is not exactly one XIAO candidate")
        port_name = ports[0]
    path = capture(port_name, args.output_dir)
    if args.open:
        import os
        os.startfile(path.resolve())


if __name__ == "__main__":
    main()
