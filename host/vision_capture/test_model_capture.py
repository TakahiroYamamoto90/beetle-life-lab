import base64
import hashlib
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import capture_vision
from capture_vision import _interpret_classes


class ModelOutputTests(unittest.TestCase):
    CLASSES = ["NO_VISIBLE_LARVA", "VISIBLE_LARVA"]

    def test_maps_official_score_target_pair_without_touching_human_label(self):
        result = _interpret_classes([[73, 1]], self.CLASSES)
        self.assertEqual(result["status"], "MODEL_OUTPUT_UNVERIFIED")
        self.assertEqual(result["top"], {
            "class_id": 1, "class_name": "VISIBLE_LARVA", "score": 73,
        })

    def test_unknown_for_missing_invalid_or_tied_output(self):
        for rows in ([], [[1]], [[101, 1]], [[73, 7]], [[70, 0], [70, 1]]):
            with self.subTest(rows=rows):
                self.assertEqual(_interpret_classes(rows, self.CLASSES)["status"], "UNKNOWN")

    def test_highest_score_wins_for_multi_class_event(self):
        result = _interpret_classes([[52, 0], [81, 1]], self.CLASSES)
        self.assertEqual(result["top"]["class_name"], "VISIBLE_LARVA")
        self.assertEqual(result["top"]["score"], 81)

    def test_same_invoke_event_frame_and_result_are_persisted_together(self):
        jpeg = bytes.fromhex("ffd8ffc0000b08000a000a01011100ffd9")
        transport = jpeg + b"\x00\x00"
        encoded = base64.b64encode(transport).decode("ascii")
        model = {"name": "larva_visibility_int8_vela", "version": "1.0.0",
                 "classes": self.CLASSES}
        model_info = base64.b64encode(json.dumps(model).encode()).decode()
        event = {"type": 1, "name": "INVOKE", "code": 0,
                 "data": {"count": 1, "image": encoded, "classes": [[73, 1]]}}
        replies = {
            "info": ["STATUS fw=0.10.2-observation-policy-vision-classify\n"],
            "vision": ["VISION_REPLY " + json.dumps({"name": "INFO?", "type": 0,
                       "code": 0, "data": {"info": model_info}}) + "\n",
                       "VISION_INSPECT_DONE\n"],
            "vision-classify-capture": [
                "VISION_CLASSIFY_REPLY " + json.dumps(event) + "\n",
                "VISION_CLASSIFY_DONE status=ok reason=none image_bytes=" + str(len(transport)) +
                " image_sha256=" + hashlib.sha256(transport).hexdigest() + "\n"],
        }

        class FakeSerial:
            def __init__(self, **_kwargs):
                self.queue = bytearray()
                self.dtr = False
                self.rts = False

            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def open(self): pass
            def close(self): pass
            def reset_input_buffer(self): self.queue.clear()
            @property
            def in_waiting(self): return len(self.queue)
            def read(self, size):
                out = self.queue[:size]
                del self.queue[:size]
                return bytes(out)
            def write(self, data):
                key = data.decode().strip()
                self.queue.extend("".join(replies[key]).encode())
                return len(data)

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(capture_vision.serial, "Serial", FakeSerial):
            path = capture_vision.capture_classified("COM-test", Path(directory))
            metadata = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
            self.assertEqual(path.read_bytes(), jpeg)
            self.assertEqual(metadata["sha256"], hashlib.sha256(jpeg).hexdigest())
            inference = metadata["model_inference"]
            self.assertTrue(inference["same_frame_as_image"])
            self.assertEqual(inference["raw_classes_score_target"], [[73, 1]])
            self.assertEqual(inference["prediction"]["top"]["class_name"], "VISIBLE_LARVA")
            self.assertEqual(metadata["label"], "unknown")
            self.assertEqual(inference["model_artifact_sha256"],
                             capture_vision.KNOWN_MODEL_ARTIFACTS["1.0.0"])

    def test_unknown_model_version_is_refused(self):
        model = {"name": "larva_visibility_int8_vela", "version": "9.9.9", "classes": self.CLASSES}
        info = base64.b64encode(json.dumps(model).encode()).decode()
        reply = "VISION_REPLY " + json.dumps({"name": "INFO?", "type": 0, "code": 0,
                                              "data": {"info": info}})

        class Port:
            def write(self, _data): pass

        with patch.object(capture_vision, "receive_lines",
                          return_value=iter([reply, "VISION_INSPECT_DONE"])):
            with self.assertRaisesRegex(RuntimeError, "expected larva visibility model"):
                capture_vision._model_info(Port())


if __name__ == "__main__":
    unittest.main()
