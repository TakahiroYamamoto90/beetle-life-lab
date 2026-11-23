"""Host tests for the v12 train-time augmentation (numpy/OpenCV only, no mmcv)."""
import unittest

import numpy as np

import bll_augment as aug
import colab_vision_v2_job as job


def image():
    rng = np.random.default_rng(1)
    return rng.integers(0, 256, (240, 240, 3), dtype=np.uint8)


class AugmentTests(unittest.TestCase):
    def test_shape_dtype_and_determinism(self):
        a = aug.augment(image(), np.random.default_rng(7), **aug.DEFAULTS)
        b = aug.augment(image(), np.random.default_rng(7), **aug.DEFAULTS)
        self.assertEqual(a.shape, (240, 240, 3))
        self.assertEqual(a.dtype, np.uint8)
        self.assertTrue(np.array_equal(a, b))

    def test_all_probabilities_zero_is_identity(self):
        src = image()
        out = aug.augment(src, np.random.default_rng(0), crop_prob=0, jpeg_prob=0, noise_prob=0)
        self.assertTrue(np.array_equal(out, src))

    def test_each_step_changes_pixels_but_keeps_size(self):
        src = np.full((240, 240, 3), 40, np.uint8)
        src[100:140, 100:140] = 200
        for options in ({"crop_prob": 1, "jpeg_prob": 0, "noise_prob": 0},
                        {"crop_prob": 0, "jpeg_prob": 1, "noise_prob": 0},
                        {"crop_prob": 0, "jpeg_prob": 0, "noise_prob": 1}):
            with self.subTest(options=options):
                out = aug.augment(src, np.random.default_rng(3), **options)
                self.assertEqual(out.shape, src.shape)
                self.assertFalse(np.array_equal(out, src))
        with self.assertRaises(ValueError):
            aug.augment(src.astype(np.float32), np.random.default_rng(0))

    def test_augmentation_only_enters_the_train_pipeline(self):
        load = {"type": "LoadImageFromFile"}
        resize = {"type": "mmengine.Resize", "scale": (96, 96)}
        cfg = {"train_dataloader": {"dataset": {"pipeline": [dict(load), dict(resize), {"type": "mmcls.PackClsInputs"}]}},
               "val_dataloader": {"dataset": {"pipeline": [dict(load), dict(resize)]}}}
        record = job.add_sensor_augmentation(cfg)
        train = cfg["train_dataloader"]["dataset"]["pipeline"]
        self.assertEqual(train[1]["type"], job.AUGMENT_TYPE)
        self.assertEqual(train[2]["type"], "mmengine.Resize")  # before the model resize
        self.assertEqual(record["seed"], aug.SEED)
        self.assertEqual([s["type"] for s in cfg["val_dataloader"]["dataset"]["pipeline"]],
                         ["LoadImageFromFile", "mmengine.Resize"])  # calibration untouched
        with self.assertRaises(ValueError):
            job.add_sensor_augmentation(cfg)  # never inserted twice
        self.assertEqual(job.RECIPE, "v12")


if __name__ == "__main__":
    unittest.main()
