"""Exercise real child-process log forwarding using local synthetic scripts, no GPU/network."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid

import colab_vision_v2_job as job


class ColabLoggingTests(unittest.TestCase):
    def setUp(self):
        self.parent = (Path(__file__).resolve().parents[2] / "captures/test_tmp").resolve()
        self.root = self.parent / ("colab-logging-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        def cleanup():
            resolved = self.root.resolve()
            resolved.relative_to(self.parent)
            if resolved == self.parent:
                raise ValueError("refuse parent removal")
            shutil.rmtree(resolved)
        self.addCleanup(cleanup)

    def test_success_stdout_stderr_forwarded_and_status_persisted(self):
        child = self.root / "child.py"
        child.write_text("import sys\nprint('fixture output',flush=True)\nprint('fixture error stream',file=sys.stderr,flush=True)\n", encoding="utf-8")
        visible = io.StringIO()
        with patch.object(job, "OUT", self.root), redirect_stdout(visible):
            job.run([sys.executable, "-u", str(child)], "success", self.root)
        saved = (self.root / "success.log").read_text(encoding="utf-8")
        for message in ("fixture output", "fixture error stream"):
            self.assertIn(message, saved)
            self.assertIn(message, visible.getvalue())
        state = json.loads((self.root / "success.status.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "COMPLETED")
        self.assertEqual(state["exit_code"], 0)

    def test_failure_keeps_cause_and_exit_code(self):
        child = self.root / "child.py"
        child.write_text("import sys\nprint('fixture root cause',flush=True)\nsys.exit(7)\n", encoding="utf-8")
        visible = io.StringIO()
        with patch.object(job, "OUT", self.root), redirect_stdout(visible):
            with self.assertRaisesRegex(RuntimeError, "failed, exit=7"):
                job.run([sys.executable, "-u", str(child)], "failure", self.root)
        self.assertIn("fixture root cause", visible.getvalue())
        self.assertIn("fixture root cause", (self.root / "failure.log").read_text(encoding="utf-8"))
        state = json.loads((self.root / "failure.status.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "FAILED")
        self.assertEqual(state["exit_code"], 7)


if __name__ == "__main__":
    unittest.main()
