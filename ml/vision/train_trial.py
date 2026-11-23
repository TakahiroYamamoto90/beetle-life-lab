"""Bounded CPU-only TRAIN-ONLY diagnostic. Not the official SSCMA model or a deployable model."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import random
import time

from dataset import CONFIG, digest
from prepare_trial import ALLOWED_TRIAL_GAPS, MAX_TRIAL_IMAGES

MODEL_ID = "small-cnn96-cpu-diagnostic-v1"
MAX_SECONDS = 120


def validate_trial(manifest):
    if (manifest.get("schema") != "bll.vision_trial_dataset.v1" or
            manifest.get("purpose") != "TRAIN_ONLY_PIPELINE_CHECK" or
            manifest.get("deployment_eligible") is not False or
            manifest.get("independent_evaluation_available") is not False or
            manifest.get("validation") != [] or manifest.get("test") != [] or
            manifest.get("class_order") != CONFIG["class_order"]):
        raise ValueError("explicit train-only non-deployable trial contract required")
    images = manifest.get("images", [])
    if not 2 <= len(images) <= MAX_TRIAL_IMAGES or len({row["image_id"] for row in images}) != len(images):
        raise ValueError(f"2..{MAX_TRIAL_IMAGES} unique images required")
    counts = {label: 0 for label in CONFIG["class_order"]}
    for row in images:
        if (row.get("source_kind") != "REAL_CAMERA" or row.get("split") != "train" or
                row.get("label") not in counts or
                row.get("class_id") != CONFIG["class_order"].index(row["label"]) or
                not isinstance(row.get("original_exclusion_reasons"), list) or
                set(row["original_exclusion_reasons"]) - ALLOWED_TRIAL_GAPS):
            raise ValueError("only reviewed real-camera binary train images accepted")
        counts[row["label"]] += 1
    if counts != manifest.get("counts") or not all(counts.values()):
        raise ValueError("class counts mismatch or missing class")


def create_model():
    from torch import nn
    # A separate diagnostic architecture, not a replacement for Seeed MobileNetV2.
    return nn.Sequential(
        nn.Conv2d(3, 8, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
        nn.Conv2d(8, 16, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
        nn.Conv2d(16, 32, 3, padding=1), nn.ReLU(), nn.AdaptiveAvgPool2d((4, 4)),
        nn.Flatten(), nn.Linear(512, 64), nn.ReLU(), nn.Linear(64, 2),
    )


def train(dataset_root, output, epochs=60, seed=20261102):
    if not 1 <= epochs <= 100:
        raise ValueError("epochs must be 1..100 for a short CPU diagnostic")
    from PIL import Image
    import numpy as np
    import torch
    versions = {name: importlib.metadata.version(name) for name in ("torch", "numpy", "Pillow")}
    if (versions != {"torch": "2.9.1", "numpy": "2.2.6", "Pillow": "12.0.0"} or
            str(torch.__version__) != "2.9.1+cpu"):
        raise ValueError("install the pinned requirements-trial.txt environment")
    dataset_root = Path(dataset_root).resolve(strict=True)
    manifest = json.loads((dataset_root / "dataset_manifest.json").read_text(encoding="utf-8"))
    validate_trial(manifest)
    arrays = []
    for row in manifest["images"]:
        relative = Path(row["image"])
        if relative.is_absolute() or relative.drive:
            raise ValueError("relative image path required")
        path = (dataset_root / relative).resolve(strict=True)
        path.relative_to(dataset_root)
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("trial JPEG SHA256 mismatch")
        with Image.open(path) as image:
            if image.format != "JPEG" or image.size != (240, 240):
                raise ValueError("original JPEG240 required")
            pixels = np.asarray(image.convert("RGB").resize((96, 96), Image.Resampling.BILINEAR), dtype=np.float32)
            arrays.append((pixels / 127.5 - 1).transpose(2, 0, 1))
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.set_num_threads(4)
    torch.use_deterministic_algorithms(True)
    x = torch.from_numpy(np.stack(arrays))
    y = torch.tensor([row["class_id"] for row in manifest["images"]], dtype=torch.long)
    model = create_model().cpu()
    optimizer = torch.optim.Adam(model.parameters(), lr=0.003)
    loss_fn = torch.nn.CrossEntropyLoss()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    clock = time.monotonic()
    with torch.no_grad():
        initial_loss = float(loss_fn(model(x), y))
    history = []
    for epoch in range(epochs):
        if time.monotonic() - clock > MAX_SECONDS:
            break
        model.train()
        optimizer.zero_grad()
        loss = loss_fn(model(x), y)
        loss.backward()
        optimizer.step()
        history.append({"epoch": epoch + 1, "train_loss_before_step": float(loss.detach())})
    model.eval()
    with torch.no_grad():
        logits = model(x)
        final_loss = float(loss_fn(logits, y))
        scores = logits.softmax(dim=1)
        predicted = logits.argmax(dim=1)
    confusion = [[0, 0], [0, 0]]
    for actual, pred in zip(y.tolist(), predicted.tolist()):
        confusion[actual][pred] += 1
    checkpoint = output / "diagnostic_model.pt"
    torch.save({"model_id": MODEL_ID, "state_dict": model.state_dict(),
                "class_order": manifest["class_order"], "seed": seed}, checkpoint)
    # Verify actual saved weights reproduce the trained output; never report missing artifacts as success.
    restored = create_model().cpu().eval()
    saved = torch.load(checkpoint, map_location="cpu", weights_only=True)
    restored.load_state_dict(saved["state_dict"])
    with torch.no_grad():
        if not torch.equal(restored(x), logits):
            raise ValueError("saved-model reload outputs differ")
    with (output / "train_resubstitution_predictions.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["image_id", "split", "human_label", "train_prediction", "no_visible_score_uncalibrated", "visible_score_uncalibrated"])
        for row, pred, score in zip(manifest["images"], predicted.tolist(), scores.tolist()):
            writer.writerow([row["image_id"], "train", row["label"], manifest["class_order"][pred], *score])
    resubstitution = {"scope": "TRAIN_IMAGES_ONLY_NOT_GENERALIZATION",
                      "correct": int((predicted == y).sum()), "total": len(y),
                      "accuracy": float((predicted == y).float().mean()),
                      "confusion_rows_actual_columns_predicted": confusion}
    result = {
        "schema": "bll.vision_cpu_trial_run.v1", "status": "TRAIN_ONLY_COMPLETED",
        "model_id": MODEL_ID, "official_sscma_pipeline": False,
        "pretrained_weights_used": False, "training_device": "cpu", "seed": seed,
        "python": platform.python_version(), "versions": versions, "torch_runtime_version": str(torch.__version__),
        "platform": platform.platform(), "cpu_threads": 4,
        "started_at_utc": started, "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "training_seconds": time.monotonic() - clock, "requested_epochs": epochs,
        "completed_epochs": len(history), "max_training_seconds": MAX_SECONDS,
        "stop_reason": "EPOCH_LIMIT" if len(history) == epochs else "TIME_LIMIT",
        "optimizer": {"name": "Adam", "learning_rate": 0.003},
        "loss": "unweighted_cross_entropy", "batch_size": len(y), "augmentation": None,
        "model_parameters": sum(p.numel() for p in model.parameters()),
        "initial_train_loss": initial_loss, "final_train_loss": final_loss,
        "train_resubstitution": resubstitution, "history": history,
        "dataset_manifest_sha256": digest(manifest), "class_order": manifest["class_order"],
        "input": {"shape": [1, 3, 96, 96], "dtype": "float32", "source_jpeg": [240, 240],
                  "resize": "Pillow12-BILINEAR-entire-image", "normalization": "RGB / 127.5 - 1"},
        "saved_model_reload_verified": True, "independent_evaluation": None,
        "deployment_eligible": False, "int8_vela_exported": False, "hardware_test_performed": False,
        "limitations": ["all images used for training; no validation/test split",
                        "training resubstitution is not independent accuracy",
                        "small CNN from scratch, not Seeed MobileNetV2 transfer learning",
                        "no natural activity, movement, health or hidden-larva inference",
                        "not suitable for Vision V2 deployment or LIVE Evidence"]}
    artifact_names = ["diagnostic_model.pt", "train_resubstitution_predictions.csv"]
    result["artifacts"] = [{"file": name, "sha256": hashlib.sha256((output / name).read_bytes()).hexdigest()}
                           for name in artifact_names]
    (output / "run_manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "epochs": len(history),
                      "seconds": result["training_seconds"], "initial_train_loss": initial_loss,
                      "final_train_loss": final_loss, "train_resubstitution_only": resubstitution,
                      "independent_evaluation": None, "deployment_eligible": False}, ensure_ascii=False))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--epochs", type=int, default=60)
    parser.add_argument("--acknowledge-train-only-cpu-diagnostic", action="store_true", required=True)
    args = parser.parse_args()
    train(args.dataset, args.output, args.epochs)


if __name__ == "__main__":
    main()
