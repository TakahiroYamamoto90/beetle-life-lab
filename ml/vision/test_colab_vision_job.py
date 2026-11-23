"""Tests for Colab runner environment and optional rollback artifact handling."""
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
import uuid

import colab_vision_v2_job as job


class ColabVisionJobTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parents[2] / "captures/test_tmp"
        self.parent.mkdir(parents=True, exist_ok=True)
        self.output = self.parent / ("colab-job-" + uuid.uuid4().hex)
        self.output.mkdir()

        def cleanup():
            resolved = self.output.resolve()
            resolved.relative_to(self.parent.resolve())
            if resolved == self.parent.resolve():
                raise ValueError("refuse parent removal")
            shutil.rmtree(resolved)

        self.addCleanup(cleanup)

    def test_noninteractive_backend_overrides_colab_inline_backend(self):
        with patch.dict(job.os.environ, {"MPLBACKEND": "module://matplotlib_inline.backend_inline"}):
            job.configure_noninteractive_matplotlib()
            self.assertEqual(job.os.environ["MPLBACKEND"], "Agg")
            self.assertEqual(job.os.environ["CUBLAS_WORKSPACE_CONFIG"], ":4096:8")

    def test_training_runner_omits_val_loop_but_preserves_export_config(self):
        source = {
            "train_dataloader": {"dataset": "train"},
            "val_dataloader": {"dataset": "train_for_export_calibration"},
            "val_cfg": {},
            "val_evaluator": {"type": "Accuracy"},
        }
        training = job.disable_training_validation(source)
        self.assertIsNone(training["val_dataloader"])
        self.assertIsNone(training["val_cfg"])
        self.assertIsNone(training["val_evaluator"])
        self.assertEqual(source["val_dataloader"]["dataset"], "train_for_export_calibration")
        self.assertIsNotNone(source["val_cfg"])

    def test_class_weights_favor_the_rare_class(self):
        weights = job.class_weights({0: 17, 1: 63}, 2)
        self.assertAlmostEqual(weights[0], 80 / 34, places=5)
        self.assertAlmostEqual(weights[1], 80 / 126, places=5)
        self.assertGreater(weights[0], weights[1])
        with self.assertRaises(ValueError):
            job.class_weights({0: 0, 1: 63}, 2)

    def test_recipe_matches_pretrained_architecture_and_trains_longer(self):
        cfg = {
            "model": {"backbone": {"type": "MobileNetv2", "widen_factor": 0.35, "out_indices": (2,)},
                      "data_preprocessor": {"type": "mmdet.DetDataPreprocessor",
                                            "mean": [0.0, 0.0, 0.0], "std": [255.0, 255.0, 255.0]},
                      "head": {"in_channels": 32, "num_classes": 3, "topk": (1, 5)}},
            "train_cfg": {"max_epochs": 10, "val_interval": 11},
            "optim_wrapper": {"optimizer": {"type": "SGD", "lr": 0.01, "momentum": 0.9}},
            "param_scheduler": [],
            "default_hooks": {"checkpoint": {"interval": 10, "save_best": "auto"}},
        }
        weights = job.apply_training_recipe(cfg, {0: 17, 1: 63}, 2)
        self.assertEqual(cfg["model"]["backbone"], job.PRETRAIN_ARCHITECTURE["backbone"])
        self.assertNotIn("out_indices", cfg["model"]["backbone"])
        # Vision V2 feeds RGB; mmcv loads BGR, so training must swap.
        self.assertTrue(cfg["model"]["data_preprocessor"]["bgr_to_rgb"])
        self.assertEqual(cfg["model"]["data_preprocessor"]["std"], [255.0, 255.0, 255.0])
        self.assertTrue(job.INPUT_COLOR_ORDER.startswith("RGB"))
        self.assertEqual(cfg["model"]["head"]["in_channels"], 128)
        self.assertEqual(cfg["model"]["head"]["num_classes"], 2)
        self.assertEqual(cfg["model"]["head"]["loss"]["class_weight"], weights)
        self.assertGreaterEqual(cfg["train_cfg"]["max_epochs"], 100)
        self.assertGreater(cfg["train_cfg"]["val_interval"], cfg["train_cfg"]["max_epochs"])
        self.assertEqual(cfg["param_scheduler"][1]["end"], cfg["train_cfg"]["max_epochs"])
        self.assertTrue(all(m < cfg["train_cfg"]["max_epochs"] for m in cfg["param_scheduler"][1]["milestones"]))
        self.assertEqual(cfg["default_hooks"]["checkpoint"]["interval"], cfg["train_cfg"]["max_epochs"])
        self.assertIsNone(cfg["default_hooks"]["checkpoint"]["save_best"])
        # v11: gentle fine-tuning so the pretrained backbone is not destroyed (v10 collapse).
        optim = cfg["optim_wrapper"]
        self.assertLess(optim["optimizer"]["lr"], 0.01)
        self.assertLess(optim["paramwise_cfg"]["custom_keys"]["backbone"]["lr_mult"], 1.0)
        self.assertGreater(optim["clip_grad"]["max_norm"], 0)
        self.assertEqual(optim["optimizer"]["momentum"], 0.9)

    def test_checkpoint_compatibility_rejects_v7_style_shape_mismatch(self):
        checkpoint = {"backbone.conv1.conv.weight": (32, 3, 3, 3), "head.fc.weight": (2, 128)}
        v7_model = {"backbone.conv1.conv.weight": (16, 3, 3, 3), "head.fc.weight": (2, 32)}
        report = job.checkpoint_compatibility(v7_model, checkpoint)
        self.assertFalse(report["compatible"])
        self.assertEqual(report["loaded"], 0)
        self.assertEqual(len(report["shape_mismatch"]), 2)
        fixed = job.checkpoint_compatibility(dict(checkpoint), checkpoint)
        self.assertTrue(fixed["compatible"])
        self.assertEqual(fixed["loaded"], 2)
        missing = job.checkpoint_compatibility({"extra.weight": (1,)}, checkpoint)
        self.assertFalse(missing["compatible"])
        self.assertEqual(missing["missing"], ["extra.weight"])

    def test_input_size_reaches_every_resize_and_rejects_unsupported(self):
        resize = lambda: {"type": "mmengine.Resize", "scale": (96, 96)}
        cfg = {"imgsz": (96, 96), "height": 96, "width": 96,
               "train_dataloader": {"dataset": {"pipeline": [{"type": "LoadImageFromFile"}, resize()]}},
               "val_dataloader": {"dataset": {"pipeline": [resize()]}},
               "test_dataloader": None, "train_pipeline": [resize()], "test_pipeline": [resize()]}
        self.assertEqual(job.set_input_size(cfg, 192), 4)
        self.assertEqual(cfg["val_dataloader"]["dataset"]["pipeline"][0]["scale"], (192, 192))
        self.assertEqual(cfg["train_dataloader"]["dataset"]["pipeline"][1]["scale"], (192, 192))
        self.assertEqual((cfg["imgsz"], cfg["height"], cfg["width"]), ((192, 192), 192, 192))
        with self.assertRaises(ValueError):
            job.set_input_size(cfg, 160)
        with self.assertRaises(ValueError):
            job.set_input_size({"train_dataloader": {"dataset": {"pipeline": []}}}, 96)

    def test_vela_summary_reads_network_summary(self):
        log = ("Network summary for epoch_100_int8\n"
               "System configuration             Ethos_U55_High_End_Embedded\n"
               "Memory mode                               Shared_Sram\n"
               "Total SRAM used                                165.27 KiB\n"
               "Total Off-chip Flash used                      587.34 KiB\n"
               "CPU operators = 0 (0.0%)\n"
               "Neural network macs                          76313344 MACs/batch\n")
        summary = job.vela_summary(log)
        self.assertEqual(summary["sram_used"], "165.27 KiB")
        self.assertEqual(summary["flash_used"], "587.34 KiB")
        self.assertEqual(summary["macs_per_inference"], "76313344 MACs/batch")
        self.assertIn("not a measured", summary["note"])

    def test_prediction_collapse_flags_constant_output(self):
        self.assertTrue(job.prediction_collapse([1] * 80, 2))
        self.assertFalse(job.prediction_collapse([1] * 79 + [0], 2))

    def test_rollback_hash_mismatch_is_recorded_without_accepting_artifact(self):
        url = ("https://files.seeedstudio.com/model_int8_sha1_"
               "1111111111111111111111111111111111111111_vela.tflite")
        with patch.object(job, "OUT", self.output), patch.object(
                job, "download", side_effect=ValueError("SHA1 mismatch")):
            result = job.download_rollback_reference({"model_url": url})
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["expected_sha1_from_filename"], "1" * 40)
        self.assertFalse((self.output / "original_face_model_int8_vela.tflite").exists())
        saved = json.loads((self.output / "original_model_download.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "UNVERIFIED")
        self.assertFalse(saved["flash_readback"])
        self.assertFalse(saved["firmware_backup"])


if __name__ == "__main__":
    unittest.main()
