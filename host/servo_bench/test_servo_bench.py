import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import servo_bench  # noqa: E402

FW = Path(__file__).resolve().parents[2] / "firmware/xiao_esp32s3/src/servo_test/servo_test_main.cpp"


class SequenceTests(unittest.TestCase):
    def test_sequences_stay_within_default_soft_limits(self):
        # The firmware clamps anyway; presets should not rely on clamping.
        for name, (_, steps) in servo_bench.SEQUENCES.items():
            for step in steps:
                if isinstance(step, tuple):
                    self.assertEqual(step[0], "wait")
                    self.assertGreater(step[1], 0)
                    continue
                parts = step.split()
                if parts[0] == "servo-us":
                    positions = parts[1:2]
                elif parts[0] == "servo-sweep":
                    positions = parts[1:3]  # then cycles, dwell_ms
                    self.assertTrue(1 <= int(parts[3]) <= 20, (name, step))
                else:
                    positions = []
                for value in positions:
                    self.assertTrue(1000 <= int(value) <= 2000, (name, step))

    def test_commands_exist_in_firmware(self):
        source = FW.read_text(encoding="utf-8")
        for _, steps in servo_bench.SEQUENCES.values():
            for step in steps:
                if isinstance(step, str):
                    self.assertIn(f'"{step.split()[0]}"', source, step)

    def test_parse_step(self):
        self.assertEqual(servo_bench.parse_step(":wait 250"), ("wait", 250))
        self.assertEqual(servo_bench.parse_step(" servo-us 1600 "), "servo-us 1600")
        with self.assertRaises(ValueError):
            servo_bench.parse_step(":wait x")

    def test_list_sequences_needs_no_port(self):
        self.assertEqual(servo_bench.main(["--list-sequences"]), 0)


if __name__ == "__main__":
    unittest.main()
