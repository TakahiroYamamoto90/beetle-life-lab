"""Bench test console for the servo test firmware (env:xiao_servo_test, servo-test-0.1.0).

Talks to the XIAO over USB serial. The firmware clamps every target to its soft limits
(default 1000..2000 us) and ramps moves; this tool only sends commands and logs replies.
"Reached" in the replies means the commanded pulse reached the target; the real servo
position is NOT sensed.

Examples (PowerShell, from the repo root):
  scripts/servo_bench.ps1 --port COM3                      # interactive console
  scripts/servo_bench.ps1 --port COM3 --sequence small     # centre, +/-150 us once, centre
  scripts/servo_bench.ps1 --port COM3 --sequence full      # centre, 1000<->2000 us x2, centre
  scripts/servo_bench.ps1 --port COM3 --sequence steps     # 1000..2000 us in 100 us steps
  scripts/servo_bench.ps1 --port COM3 --sequence small --off-after
  scripts/servo_bench.ps1 --list-sequences

Interactive console: type firmware commands (help, info, state, servo-us 1600, servo-deg 150,
servo-center, servo-off, servo-ramp 5, servo-limits 900 2100, servo-sweep 1200 1800 3 500),
or ":run <sequence>", ":wait <ms>", ":quit". Ctrl+C sends servo-off and exits.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import threading
import time
from pathlib import Path

EXPECTED_FW_PREFIX = "servo-test-"
# The main firmware (0.13.0+) has servo-status/servo-us/servo-pose/servo-cal/servo-off
# for calibration, but no servo-center/servo-sweep: only the interactive console applies.
MAIN_FW_WITH_SERVO = ("0.13.0-observation-servo", "0.13.1-observation-servo",
                      "0.13.2-observation-servo", "0.14.0-observation-classify",
                      "0.14.1-observation-classify", "0.14.2-observation-classify",
                      "0.14.3-observation-classify")

# Each step is a firmware command or ("wait", milliseconds).
SEQUENCES: dict[str, tuple[str, list]] = {
    "small": ("centre, 1350<->1650 us once (about +/-20 deg), centre",
              ["servo-center", ("wait", 2000), "servo-sweep 1350 1650 1 1000", ("wait", 5000),
               "servo-center", ("wait", 1500), "state"]),
    "full": ("centre, 1000<->2000 us twice (about +/-67 deg), centre",
             ["servo-center", ("wait", 2000), "servo-sweep 1000 2000 2 1000", ("wait", 13000),
              "servo-center", ("wait", 2500), "state"]),
    "steps": ("1000 -> 2000 us in 100 us steps, 1.5 s each, then centre",
              ["servo-center", ("wait", 2000)]
              + [x for us in range(1000, 2001, 100) for x in (f"servo-us {us}", ("wait", 1500))]
              + ["servo-center", ("wait", 2500), "state"]),
    "center": ("centre only", ["servo-center", ("wait", 2000), "state"]),
}


def parse_step(text: str):
    """Console helper: ':wait 500' -> ('wait', 500); other text is a command."""
    text = text.strip()
    if text.startswith(":wait"):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            raise ValueError("usage: :wait <ms>")
        return ("wait", int(parts[1]))
    return text


class Bench:
    def __init__(self, port: str, log_path: Path, echo=print):
        import serial  # pyserial; imported here so tests need no hardware

        # Same control lines as platformio.ini (monitor_dtr=1, monitor_rts=0): with the
        # ESP32-S3 native USB serial, other DTR/RTS states can reset the board on open or
        # close, which stops the pulses (servo goes limp) mid-test.
        self.serial = serial.Serial()
        self.serial.port, self.serial.baudrate, self.serial.timeout = port, 115200, 0.1
        self.serial.dtr, self.serial.rts = True, False
        self.serial.open()
        self.log = open(log_path, "a", encoding="utf-8")
        self.echo = echo
        self.lines: list[str] = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _write_log(self, direction: str, text: str):
        stamp = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with self._lock:
            self.log.write(f"{stamp} {direction} {text}\n")
            self.log.flush()
        self.echo(f"{stamp} {direction} {text}")

    def _read_loop(self):
        while not self._stop.is_set():
            try:
                raw = self.serial.readline()
            except Exception as exc:  # port vanished (USB unplugged / reset)
                self._write_log("!", f"serial error: {exc}")
                return
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.lines.append(line)
                self._write_log("<", line)

    def send(self, command: str, settle_ms: int = 300):
        self._write_log(">", command)
        self.serial.write((command + "\n").encode())
        time.sleep(settle_ms / 1000)

    def run(self, steps):
        for step in steps:
            if isinstance(step, tuple):
                time.sleep(step[1] / 1000)
            else:
                self.send(step)

    def check_firmware(self) -> str | None:
        """'bench' for the servo test firmware, 'main' for a main firmware with servo commands."""
        start = len(self.lines)
        self.send("info", settle_ms=800)
        replies = self.lines[start:]
        if any(f"fw={EXPECTED_FW_PREFIX}" in line for line in replies):
            return "bench"
        if any(f"fw={v} " in line or line.endswith(f"fw={v}") for line in replies for v in MAIN_FW_WITH_SERVO):
            return "main"
        return None

    def close(self, off: bool):
        try:
            if off:
                self.send("servo-off")
        finally:
            self._stop.set()
            self._reader.join(timeout=1)
            self.serial.close()
            self.log.close()


def default_log_path(root: Path) -> Path:
    out = root / "captures" / "servo_test"
    out.mkdir(parents=True, exist_ok=True)
    return out / f"servo-bench-{dt.datetime.now():%Y%m%d-%H%M%S}.log"


def interactive(bench: Bench):
    print("Interactive servo console. Firmware commands, ':run <sequence>', ':wait <ms>', ':quit'.")
    while True:
        try:
            text = input()
        except EOFError:
            return
        text = text.strip()
        if not text:
            continue
        if text in (":quit", ":q", "exit", "quit"):
            return
        if text.startswith(":run"):
            name = text[4:].strip()
            if name not in SEQUENCES:
                print(f"unknown sequence {name!r}; choose from {', '.join(SEQUENCES)}")
                continue
            bench.run(SEQUENCES[name][1])
            continue
        try:
            step = parse_step(text)
        except ValueError as exc:
            print(exc)
            continue
        bench.run([step])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="serial port of the XIAO running servo-test firmware, e.g. COM3")
    parser.add_argument("--sequence", choices=sorted(SEQUENCES), help="run a preset and exit")
    parser.add_argument("--off-after", action="store_true",
                        help="send servo-off at exit (servo goes limp; default keeps holding position)")
    parser.add_argument("--log", type=Path, help="log file (default captures/servo_test/servo-bench-<time>.log)")
    parser.add_argument("--list-sequences", action="store_true")
    args = parser.parse_args(argv)

    if args.list_sequences:
        for name, (desc, _) in SEQUENCES.items():
            print(f"{name:7s} {desc}")
        return 0
    if not args.port:
        parser.error("--port is required")

    root = Path(__file__).resolve().parents[2]
    log_path = args.log or default_log_path(root)
    bench = Bench(args.port, log_path)
    print(f"log: {log_path}")
    off = args.off_after
    try:
        time.sleep(1.0)  # USB CDC may print the boot banner first
        kind = bench.check_firmware()
        if kind is None:
            print(f"The board did not report fw={EXPECTED_FW_PREFIX}* or a main firmware with servo commands; "
                  "refusing to drive it.", file=sys.stderr)
            return 2
        if kind == "main" and args.sequence:
            print("Preset sequences need the servo test firmware. On the main firmware use the console: "
                  "servo-status, servo-us <us>, servo-pose <FRONT|TOP|BACK>, servo-cal <POSE> <us|0>, servo-off.",
                  file=sys.stderr)
            return 2
        if args.sequence:
            bench.run(SEQUENCES[args.sequence][1])
        else:
            interactive(bench)
    except KeyboardInterrupt:
        print("interrupted: stopping pulses")
        off = True
    finally:
        bench.close(off)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
