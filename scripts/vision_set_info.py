"""Write the model metadata (AT+INFO) to a Vision V2 on USB, then check one INVOKE.

usage: python scripts/vision_set_info.py COMx [--version 1.5.0] [--dry-run]

Run after flashing the Vela model to 0x400000. The PC tools identify the loaded model
by this metadata (scripts/vision_model.py KNOWN_MODEL_ARTIFACTS); without it the model
output is recorded as raw classes only. Writes nothing but the INFO string.
"""
import argparse
import base64
import json
import sys
import time
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vision_model import KNOWN_MODEL_ARTIFACTS, MODEL_CLASSES, MODEL_NAME, MODEL_RECIPES  # noqa: E402

SIZES = {"1.5.0": "621104"}  # Vela file size of the released model


def info_for(version):
    sha = KNOWN_MODEL_ARTIFACTS[version]
    return {"name": MODEL_NAME, "version": version, "category": "Image Classification",
            "model_type": "TFLite", "algorithm": f"MobileNetV2 (recipe {MODEL_RECIPES[version]})",
            "description": f"Recipe {MODEL_RECIPES[version]}; sha256 {sha[:12]}; XMODEM onto v2023.12.19",
            "classes": list(MODEL_CLASSES), "size": SIZES.get(version, ""), "isCustom": True}


def command(port, cmd, name, want_event=False, timeout=15.0):
    port.write((cmd + "\r\n").encode())
    buf, resp, event = b"", None, None
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        buf += port.read(65536)
        *lines, buf = buf.split(b"\n")
        for line in lines:
            line = line.strip()
            if not line.startswith(b"{"):
                continue
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if reply.get("name") != name:
                continue
            if reply.get("type") == 0:
                resp = reply
            elif reply.get("type") == 1:
                event = reply
        if resp is not None and (resp.get("code") != 0 or not want_event or event is not None):
            break
    return resp, event


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("port", help="Vision V2 USB serial port (CH343), e.g. COM4")
    parser.add_argument("--version", default="1.5.0", choices=sorted(KNOWN_MODEL_ARTIFACTS))
    parser.add_argument("--dry-run", action="store_true", help="print the INFO JSON only")
    args = parser.parse_args()
    info = info_for(args.version)
    print(json.dumps(info, ensure_ascii=False))
    if args.dry_run:
        return 0
    payload = base64.b64encode(json.dumps(info, separators=(",", ":")).encode()).decode()
    with serial.Serial(args.port, 921600, timeout=0.2) as port:
        time.sleep(3)  # opening the CH343 port resets the WE2; wait for boot
        port.reset_input_buffer()
        ver, _ = command(port, "AT+VER?", "VER?")
        print("VER?", json.dumps((ver or {}).get("data")))
        resp, _ = command(port, f'AT+INFO="{payload}"', "INFO")
        if not resp or resp.get("code") != 0:
            print("INFO write failed:", resp)
            return 1
        back, _ = command(port, "AT+INFO?", "INFO?")
        stored = json.loads(base64.b64decode(back["data"]["info"])) if back and back.get("code") == 0 else None
        if stored != info:
            print("INFO read-back differs:", stored)
            return 1
        print("INFO written and read back:", info["name"], info["version"])
        _, event = command(port, "AT+INVOKE=1,0,0", "INVOKE", want_event=True, timeout=20)
        classes = (event or {}).get("data", {}).get("classes")
        print("INVOKE classes [score, class_id]:", classes)
        if not classes:
            print("No classification output: check that the model was written to 0x400000")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
