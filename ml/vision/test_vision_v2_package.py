"""Synthetic contract fixtures only. No actual TFLite, training, flash or browser operation."""
import ast
import base64
import copy
import hashlib
import json
from pathlib import Path
import shutil
import unittest
import uuid
import zipfile

from build_vision_v2_package import build, notebook
from check_vision_v2_artifact import inspect, validate_description
from test_trial_contract import fixture


class VisionV2PackageTests(unittest.TestCase):
    def setUp(self):
        self.workspace = (Path(__file__).resolve().parents[2] / "captures/test_tmp").resolve()
        self.root = self.workspace / ("vision-v2-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        def cleanup():
            resolved = self.root.resolve()
            resolved.relative_to(self.workspace)
            if resolved == self.workspace:
                raise ValueError("refuse parent removal")
            shutil.rmtree(resolved)
        self.addCleanup(cleanup)

    def sources(self):
        manifest = fixture()
        manifest["conditions"] = {"source": "unit_test_fixture"}
        for row in manifest["images"]:
            row.update(image=f"train/{row['label']}/{row['image_id']}.jpg",
                       capture_session_id="fixture-session", case_id="fixture-case")
            path = self.root / row["image"]
            path.parent.mkdir(parents=True)
            path.write_bytes(b"synthetic unit fixture, not an actual JPEG or sensing result")
            row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.root / "dataset_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        original = {"name": "fixture model", "version": "fixture", "url":
                    "https://files.seeedstudio.com/fixture_int8_sha1_" + "0" * 40 + "_vela.tflite"}
        summary = {"vision_replies": [{"name": "INFO?", "data": {"info": base64.b64encode(json.dumps(original).encode()).decode()}},
                                     {"name": "VER?", "data": {"software": "fixture"}}]}
        (self.root / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        return manifest

    def test_bundle_hash_classes_sources_and_no_cpu_weights(self):
        self.sources()
        out = self.root / "out"
        result = build(self.root, self.root, out)
        bundle = Path(result["bundle"])
        self.assertEqual(result["sha256"], hashlib.sha256(bundle.read_bytes()).hexdigest())
        with zipfile.ZipFile(bundle) as archive:
            manifest = json.loads(archive.read("smoke_manifest.json"))
            self.assertEqual(manifest["class_order"], ["NO_VISIBLE_LARVA", "VISIBLE_LARVA"])
            self.assertFalse(manifest["independent_evaluation_available"])
            self.assertFalse(manifest["production_eligible"])
            self.assertFalse(any(name.endswith((".pt", ".pth")) for name in archive.namelist()))
            for row in manifest["images"] + manifest["source_files"]:
                self.assertEqual(row["sha256"], hashlib.sha256(archive.read(row["file"])).hexdigest())
        with self.assertRaises(FileExistsError):
            build(self.root, self.root, out)

    def test_corrupted_image_refused_before_packaging(self):
        manifest = self.sources()
        (self.root / manifest["images"][0]["image"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "SHA256"):
            build(self.root, self.root, self.root / "out")

    def test_notebook_cells_parse_and_are_never_marked_executed(self):
        result = notebook("0" * 64)
        for cell in result["cells"]:
            if cell["cell_type"] == "code":
                ast.parse("".join(cell["source"]))
                self.assertIsNone(cell["execution_count"])
                self.assertEqual(cell["outputs"], [])

    def test_valid_description_is_only_a_necessary_bench_gate(self):
        info = {"input_shape": [1, 96, 96, 3], "output_shape": [1, 2],
                "input_dtype": "INT8", "output_dtype": "INT8",
                "input_scales": [.0039], "output_scales": [.0039],
                "input_zero_points": [-128], "output_zero_points": [-128],
                "used_custom_operators": ["ethos-u"]}
        validate_description(info)
        for field, value in (("used_custom_operators", []), ("input_dtype", "FLOAT32"),
                             ("output_shape", [1, 3]), ("input_scales", [float("nan")]),
                             ("input_zero_points", [256])):
            invalid = copy.deepcopy(info)
            invalid[field] = value
            with self.assertRaises(ValueError):
                validate_description(invalid)
        # The 192 comparison model must declare 192; a 96 model is refused for it and vice versa.
        with self.assertRaises(ValueError):
            validate_description(info, 192)
        large = dict(info, input_shape=[1, 192, 192, 3])
        validate_description(large, 192)
        with self.assertRaises(ValueError):
            validate_description(large)
        with self.assertRaises(ValueError):
            validate_description(dict(info, input_shape=[1, 160, 160, 3]), 160)

    def test_file_rename_or_text_ethos_marker_is_not_a_vela_model(self):
        path = self.root / "fake_int8_vela.tflite"
        path.write_bytes(b"this is not TFLite even if ethos-u appears")
        with self.assertRaisesRegex(ValueError, "FlatBuffer"):
            inspect(path)


if __name__ == "__main__":
    unittest.main()
