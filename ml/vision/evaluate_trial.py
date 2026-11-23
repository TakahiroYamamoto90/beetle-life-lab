"""Compare frozen CPU diagnostic models on old/new train images; not independent evaluation."""
import argparse
import csv
import hashlib
import json
from pathlib import Path

from dataset import CONFIG, digest
from train_trial import MODEL_ID, create_model, validate_trial


def summarize(actual, predicted):
    if len(actual) != len(predicted) or not actual:
        raise ValueError("nonempty paired predictions required")
    confusion = [[0, 0], [0, 0]]
    for target, prediction in zip(actual, predicted):
        if target not in (0, 1) or prediction not in (0, 1):
            raise ValueError("binary class IDs required")
        confusion[target][prediction] += 1
    tn, fp = confusion[0]
    fn, tp = confusion[1]
    return {"n": len(actual), "correct": tn + tp, "accuracy": (tn + tp) / len(actual),
            "confusion_rows_actual_columns_predicted": confusion,
            "visible_precision": tp / (tp + fp) if tp + fp else None,
            "visible_recall": tp / (tp + fn) if tp + fn else None,
            "false_positive_count": fp, "false_negative_count": fn,
            "both_classes_present": bool(tn + fp) and bool(fn + tp)}


def evaluation_limitations(added_metrics):
    notes = ["not an independent test; old and new images share acquisition context",
             "new-model results are training resubstitution",
             "reflection/fingerprint causation not established"]
    if added_metrics is not None and not added_metrics["both_classes_present"]:
        notes.append("added images contain only one class; both-class performance cannot be evaluated")
    return notes


def evaluate(dataset_root, run_root, original_dataset, output, scope):
    from PIL import Image
    import numpy as np
    import torch
    dataset_root, run_root, original_dataset = map(Path, (dataset_root, run_root, original_dataset))
    current = json.loads((dataset_root / "dataset_manifest.json").read_text(encoding="utf-8"))
    original = json.loads((original_dataset / "dataset_manifest.json").read_text(encoding="utf-8"))
    validate_trial(current)
    validate_trial(original)
    old = {row["image_id"]: row for row in original["images"]}
    rows = current["images"]
    by_id = {row["image_id"]: row for row in rows}
    if not old.keys() <= by_id.keys():
        raise ValueError("original images missing; comparison requires identical old inputs")
    for image_id, prior in old.items():
        if any(prior[field] != by_id[image_id][field] for field in
               ("sha256", "label", "class_id", "capture_session_id", "case_id")):
            raise ValueError("original image/annotation/group changed: " + image_id)
    run = json.loads((run_root / "run_manifest.json").read_text(encoding="utf-8"))
    expected_training_manifest = (original if scope == "OLD_MODEL_BEFORE_RETRAINING_SAME_SESSION"
                                  else current if scope == "NEW_MODEL_TRAIN_RESUBSTITUTION" else None)
    if expected_training_manifest is None or run.get("dataset_manifest_sha256") != digest(expected_training_manifest):
        raise ValueError("comparison scope does not match the model's recorded training data")
    if (run["model_id"] != MODEL_ID or run["class_order"] != CONFIG["class_order"] or
            run["deployment_eligible"] is not False):
        raise ValueError("only the pinned non-deployable CPU diagnostic is supported")
    checkpoint = run_root / "diagnostic_model.pt"
    model_sha = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    expected = next(item["sha256"] for item in run["artifacts"] if item["file"] == checkpoint.name)
    if model_sha != expected:
        raise ValueError("model artifact SHA256 mismatch")
    saved = torch.load(checkpoint, map_location="cpu", weights_only=True)
    if saved["model_id"] != MODEL_ID or saved["class_order"] != CONFIG["class_order"]:
        raise ValueError("checkpoint class/model mismatch")
    model = create_model().cpu().eval()
    model.load_state_dict(saved["state_dict"])
    torch.set_num_threads(4)
    torch.use_deterministic_algorithms(True)
    arrays = []
    for row in rows:
        relative = Path(row["image"])
        if relative.is_absolute() or relative.drive:
            raise ValueError("relative image path required")
        path = (dataset_root / relative).resolve(strict=True)
        path.relative_to(dataset_root.resolve())
        if hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("image SHA256 mismatch")
        with Image.open(path) as image:
            if image.format != "JPEG" or image.size != (240, 240):
                raise ValueError("original JPEG240 required")
            pixels = np.asarray(image.convert("RGB").resize((96, 96), Image.Resampling.BILINEAR), dtype=np.float32)
            arrays.append((pixels / 127.5 - 1).transpose(2, 0, 1))
    with torch.no_grad():
        logits = model(torch.from_numpy(np.stack(arrays)))
        if not bool(torch.isfinite(logits).all()):
            raise ValueError("nonfinite model outputs")
        scores = logits.softmax(dim=1).tolist()
        predictions = logits.argmax(dim=1).tolist()
    groups = {"all_reviewed": list(range(len(rows))),
              "original_training_images": [i for i, row in enumerate(rows) if row["image_id"] in old],
              "added_images": [i for i, row in enumerate(rows) if row["image_id"] not in old]}
    result = {"schema": "bll.vision_cpu_trial_comparison.v1", "scope": scope,
              "model_sha256": model_sha, "model_id": MODEL_ID,
              "independent_evaluation_available": False, "deployment_eligible": False,
              "class_order": CONFIG["class_order"],
              "metrics": {name: summarize([rows[i]["class_id"] for i in indices],
                                           [predictions[i] for i in indices]) if indices else None
                          for name, indices in groups.items()},
              "added_images_share_original_session": any(row["capture_session_id"] in
                  {prior["capture_session_id"] for prior in old.values()} for row in rows if row["image_id"] not in old),
              "added_images_share_original_case": any(row["case_id"] in
                  {prior["case_id"] for prior in old.values()} for row in rows if row["image_id"] not in old),
              }
    result["limitations"] = evaluation_limitations(result["metrics"]["added_images"])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output / "predictions.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["image_id", "group", "human_label", "prediction", "no_visible_score_uncalibrated", "visible_score_uncalibrated"])
        for row, prediction, score in zip(rows, predictions, scores):
            writer.writerow([row["image_id"], "original" if row["image_id"] in old else "added",
                             row["label"], CONFIG["class_order"][prediction], *score])
    print(json.dumps(result, ensure_ascii=False))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--original-dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scope", choices=("OLD_MODEL_BEFORE_RETRAINING_SAME_SESSION", "NEW_MODEL_TRAIN_RESUBSTITUTION"), required=True)
    args = parser.parse_args()
    evaluate(args.dataset, args.run, args.original_dataset, args.output, args.scope)


if __name__ == "__main__":
    main()
