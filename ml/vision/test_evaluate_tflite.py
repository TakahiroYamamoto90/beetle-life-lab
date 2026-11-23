"""Host tests for the TFLite bench evaluator (no TFLite runtime needed)."""
import json
from pathlib import Path
import shutil
import sqlite3
import unittest
import uuid

import numpy as np

import evaluate_tflite as ev


class EvaluateTfliteTests(unittest.TestCase):
    def setUp(self):
        self.parent = (Path(__file__).resolve().parents[2] / "captures/test_tmp").resolve()
        self.root = self.parent / ("eval-tflite-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)

        def cleanup():
            resolved = self.root.resolve()
            resolved.relative_to(self.parent)
            if resolved == self.parent:
                raise ValueError("refuse parent removal")
            shutil.rmtree(resolved)
        self.addCleanup(cleanup)

    def test_device_order_defaults_to_rgb_and_swaps_channels(self):
        import cv2
        self.assertEqual(ev.DEVICE_INPUT_ORDER, "RGB")
        image = np.zeros((240, 240, 3), np.uint8)
        image[:, :, 2] = 200  # red in OpenCV BGR storage
        path = self.root / "red.png"
        cv2.imwrite(str(path), image)
        rgb = ev.load_image(path)
        bgr = ev.load_image(path, "BGR")
        self.assertEqual(rgb.shape, (96, 96, 3))
        self.assertEqual(int(rgb[0, 0, 0]), 200)
        self.assertEqual(int(bgr[0, 0, 2]), 200)
        with self.assertRaises(ValueError):
            ev.load_image(path, "GRB")

    def test_input_size_comes_from_the_model(self):
        class Fake:
            def __init__(self, shape): self.shape = shape
            def get_input_details(self): return [{"shape": self.shape}]
        self.assertEqual(ev.model_input_size(Fake([1, 192, 192, 3])), 192)
        self.assertEqual(ev.model_input_size(Fake(np.array([1, 96, 96, 3]))), 96)
        with self.assertRaises(ValueError):
            ev.model_input_size(Fake([1, 96, 192, 3]))

    def test_quantize_matches_training_normalisation(self):
        image = np.array([[[0, 128, 255]]], np.uint8)
        q = ev.quantize_input(image, 1 / 255, -128)
        self.assertEqual(q.dtype, np.int8)
        self.assertEqual(q.tolist(), [[[-128, 0, 127]]])

    def test_summary_detects_collapse_and_computes_auc(self):
        collapsed = [{"label": 0, "p_visible": 0.7, "pred": 1}, {"label": 1, "p_visible": 0.6, "pred": 1}]
        s = ev.summarize(collapsed)
        self.assertTrue(s["collapsed"])
        self.assertEqual(s["auc"], 0.0)
        self.assertEqual(s["balanced_accuracy"], 0.5)
        good = [{"label": 0, "p_visible": 0.2, "pred": 0}, {"label": 0, "p_visible": 0.4, "pred": 0},
                {"label": 1, "p_visible": 0.4, "pred": 1}, {"label": 1, "p_visible": 0.9, "pred": 1}]
        s = ev.summarize(good)
        self.assertFalse(s["collapsed"])
        self.assertEqual(s["confusion_rows_true_cols_pred"], [[2, 0], [0, 2]])
        self.assertEqual(s["auc"], 0.875)  # one tie counts half
        low, high = s["accuracy_wilson95"]
        self.assertLess(low, 1.0)
        self.assertAlmostEqual(high, 1.0, places=4)

    def test_collect_uses_latest_human_label_and_excludes_training(self):
        db_path = self.root / "collection.sqlite3"
        db = sqlite3.connect(db_path)
        db.executescript("""
            create table samples (id text, session_id text, created text, image_path text, metadata text);
            create table annotations (id integer primary key, sample_id text, created text, label text,
                                      reviewer text, note text);""")
        rows = [("a", "sessions/s/a.jpg", {"width": 240, "height": 240}),
                ("b", "sessions/s/b.jpg", {"width": 480, "height": 480}),
                ("c", "sessions/s/c.jpg", {"width": 240, "height": 240}),
                ("d", "sessions/s/d.jpg", {"width": 240, "height": 240})]
        for sample_id, path, meta in rows:
            db.execute("insert into samples values (?,?,?,?,?)", (sample_id, "s", "t", path, json.dumps(meta)))
        for sample_id, label in [("a", "larva_visible"), ("a", "background"), ("b", "larva_visible"),
                                 ("c", "uncertain"), ("d", "background")]:
            db.execute("insert into annotations (sample_id, created, label) values (?,?,?)", (sample_id, "t", label))
        db.commit()
        db.close()
        items = ev.collect_labelled(db_path, self.root, exclude_ids={"d"})
        by_id = {item["id"]: item for item in items}
        self.assertEqual(sorted(by_id), ["a", "b"])  # c is not binary, d is training
        self.assertEqual(by_id["a"]["label"], 0)    # latest annotation wins
        self.assertEqual(by_id["b"]["group"], "jpeg_480x480")
        self.assertEqual(by_id["a"]["path"], self.root / "sessions/s/a.jpg")
        # No role table yet: an evaluation-only request finds nothing rather than everything.
        self.assertEqual(ev.collect_labelled(db_path, self.root, role="evaluation_only"), [])
        db = sqlite3.connect(db_path)
        db.execute("create table dataset_roles (id integer primary key, sample_id text, created text, "
                   "role text, reviewer text)")
        for sample_id, role in [("a", "evaluation_only"), ("b", "evaluation_only"), ("b", "unassigned")]:
            db.execute("insert into dataset_roles (sample_id, created, role, reviewer) values (?,?,?,?)",
                       (sample_id, "t", role, ""))
        db.commit()
        db.close()
        held_out = ev.collect_labelled(db_path, self.root, role="evaluation_only")
        self.assertEqual([item["id"] for item in held_out], ["a"])  # b's latest role is unassigned
        # Evaluation case: every labelled image of the case, grouped by arm pose when known.
        db = sqlite3.connect(db_path)
        db.executescript("""
            create table sessions (id text, created text, details text);
            create table case_profiles (id integer primary key, case_id text, created text,
                                        display_name text, role text, note text, editor text);""")
        db.execute("insert into sessions values ('s','t',?)", (json.dumps({"box_id": "box-e"}),))
        db.execute("insert into samples values ('e','s','t','sessions/s/e.jpg',?)",
                   (json.dumps({"width": 240, "height": 240, "camera_pose": "TOP"}),))
        db.execute("insert into annotations (sample_id, created, label) values ('e','t','background')")
        self.assertEqual(sorted(i["id"] for i in ev.collect_labelled(db_path, self.root, {"d"}, "evaluation")), ["a"])
        for role in ("training", "evaluation"):
            db.execute("insert into case_profiles (case_id, created, display_name, role, note, editor) "
                       "values ('box-e','t','',?, '', '')", (role,))
        db.commit()
        db.close()
        items = {i["id"]: i for i in ev.collect_labelled(db_path, self.root, {"d"}, "evaluation")}
        self.assertEqual(sorted(items), ["a", "b", "e"])  # d stays excluded as training
        self.assertEqual(items["e"]["group"], "jpeg_240x240_TOP")
        self.assertEqual([i["id"] for i in ev.collect_labelled(db_path, self.root, role="evaluation_only")], ["a"])


if __name__ == "__main__":
    unittest.main()
