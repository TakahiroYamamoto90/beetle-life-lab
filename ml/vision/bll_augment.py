"""Train-time augmentation toward the Vision V2 device input (recipe v12).

2026-11-04: device scores fell far below PC scores on the saved low-bitrate JPEGs,
and Gaussian noise on those JPEGs reproduced the drop (the device infers on the
noisy pre-JPEG frame). Larvae cut by the frame edge were also missed. So training
images get, before the model resize:
  * a random crop (75-100 % area, resized back) -> partial / shifted subjects,
  * optional JPEG re-encode at random quality,
  * additive Gaussian sensor noise (applied last, as on the device).
The crop can cut a larva out of a VISIBLE_LARVA image (label noise); the area floor
keeps that rare. Never used for calibration, PC evaluation or the device.
"""
import math

import numpy as np

DEFAULTS = {
    "crop_prob": 0.5, "crop_area": (0.75, 1.0),
    "jpeg_prob": 0.3, "jpeg_quality": (20, 70),
    "noise_prob": 0.6, "noise_sigma": (2.0, 8.0),
}
SEED = 20261104


def augment(img, rng, crop_prob=0.5, crop_area=(0.75, 1.0), jpeg_prob=0.3,
            jpeg_quality=(20, 70), noise_prob=0.6, noise_sigma=(2.0, 8.0)):
    """uint8 HxWx3 -> uint8 HxWx3 of the same size. Channel order is untouched."""
    import cv2
    if img.dtype != np.uint8 or img.ndim != 3:
        raise ValueError("expected uint8 HxWxC image")
    height, width = img.shape[:2]
    if rng.random() < crop_prob:
        side = math.sqrt(rng.uniform(*crop_area))
        crop_h, crop_w = max(1, round(height * side)), max(1, round(width * side))
        top = int(rng.integers(0, height - crop_h + 1))
        left = int(rng.integers(0, width - crop_w + 1))
        img = cv2.resize(img[top:top + crop_h, left:left + crop_w], (width, height),
                         interpolation=cv2.INTER_LINEAR)
    if rng.random() < jpeg_prob:
        quality = int(rng.integers(jpeg_quality[0], jpeg_quality[1] + 1))
        ok, buffer = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            raise ValueError("JPEG re-encode failed")
        img = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if rng.random() < noise_prob:
        sigma = rng.uniform(*noise_sigma)
        img = np.clip(img.astype(np.float32) + rng.normal(0.0, sigma, img.shape), 0, 255).astype(np.uint8)
    return np.ascontiguousarray(img)


def register():
    """Register mmcls.BllSensorAugment (Colab ModelAssistant env only)."""
    from mmcv.transforms import BaseTransform
    from mmcls.registry import TRANSFORMS

    @TRANSFORMS.register_module(force=True)
    class BllSensorAugment(BaseTransform):
        def __init__(self, seed=SEED, **options):
            self.options = {**DEFAULTS, **options}
            self.rng = np.random.default_rng(seed)

        def transform(self, results):
            img = augment(results["img"], self.rng, **self.options)
            results["img"] = img
            results["img_shape"] = img.shape[:2]
            return results

    return BllSensorAugment
