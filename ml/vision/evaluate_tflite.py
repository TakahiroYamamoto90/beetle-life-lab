"""Evaluate INT8 TFLite classifiers on human-labelled collection images.

Shared by the PC CLI and the Colab job. The Vision V2 feeds the model RGB
(confirmed 2026-11-03 by matching recorded device scores), so RGB is the
default input order. Results are bench numbers, never a deployment approval.

Usage (PC, read-only DB):
  python evaluate_tflite.py --model v8=path/to/epoch_100_int8.tflite \
      --exclude-manifest captures/vision_ml/<trial>/dataset_manifest.json \
      --out captures/vision_ml/pc-eval-<date>/pc_eval.json
"""
import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[2]
CLASS_ORDER = ("NO_VISIBLE_LARVA", "VISIBLE_LARVA")
# Collection DB annotation label -> class id. Other labels (uncertain, setup...) are skipped.
LABEL_TO_ID = {"background": 0, "larva_visible": 1}
INPUT_SIZE = 96
DEVICE_INPUT_ORDER = "RGB"


def load_interpreter(path):
    try:
        from ai_edge_litert.interpreter import Interpreter
    except ImportError:  # Colab ModelAssistant env ships TensorFlow instead
        from tensorflow.lite.python.interpreter import Interpreter
    interpreter = Interpreter(model_path=str(path))
    interpreter.allocate_tensors()
    return interpreter


def load_image(path, order=DEVICE_INPUT_ORDER, size=INPUT_SIZE):
    """uint8 HxWx3 in the requested channel order, bilinear like mmcv.imresize."""
    import cv2
    bgr = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if bgr is None:
        raise ValueError(f"cannot decode image: {path}")
    small = cv2.resize(bgr, (size, size), interpolation=cv2.INTER_LINEAR)
    if order == "BGR":
        return small
    if order == "RGB":
        return small[:, :, ::-1].copy()
    raise ValueError("order must be RGB or BGR")


def quantize_input(image_uint8, scale, zero_point):
    """Training normalisation x/255, then the model's INT8 input quantisation."""
    import numpy as np
    x = image_uint8.astype(np.float32) / 255.0
    return np.clip(np.round(x / scale + zero_point), -128, 127).astype(np.int8)


def predict(interpreter, image_uint8):
    """Dequantised class probabilities for one image."""
    import numpy as np
    inp = interpreter.get_input_details()[0]
    out = interpreter.get_output_details()[0]
    scale, zero_point = inp["quantization"]
    interpreter.set_tensor(inp["index"], quantize_input(image_uint8, scale, zero_point)[None])
    interpreter.invoke()
    raw = interpreter.get_tensor(out["index"])[0].astype(np.float32)
    out_scale, out_zero = out["quantization"]
    return [float(v) for v in (raw - out_zero) * out_scale]


def auc(rows):
    """Probability that a positive scores above a negative (ties count half)."""
    pos = [r["p_visible"] for r in rows if r["label"] == 1]
    neg = [r["p_visible"] for r in rows if r["label"] == 0]
    if not pos or not neg:
        return None
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 4)


def wilson(successes, n, z=1.96):
    if n == 0:
        return None
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(centre - half, 4), round(centre + half, 4)]


def summarize(rows):
    confusion = [[sum(1 for r in rows if r["label"] == t and r["pred"] == p) for p in (0, 1)]
                 for t in (0, 1)]
    correct = confusion[0][0] + confusion[1][1]
    recalls = [confusion[t][t] / sum(confusion[t]) if sum(confusion[t]) else None for t in (0, 1)]
    present = [r for r in recalls if r is not None]
    return {"n": len(rows), "confusion_rows_true_cols_pred": confusion, "correct": correct,
            "accuracy": round(correct / len(rows), 4) if rows else None,
            "accuracy_wilson95": wilson(correct, len(rows)),
            "recall_by_class": [None if r is None else round(r, 4) for r in recalls],
            "balanced_accuracy": round(sum(present) / len(present), 4) if present else None,
            "auc": auc(rows), "collapsed": len({r["pred"] for r in rows}) < 2}


def collect_labelled(db_path, captures_root, exclude_ids=(), role=None):
    """Latest human label per sample from the collection DB, opened read-only.

    role="evaluation_only" keeps only images a human reserved for evaluation
    (latest dataset_roles entry); images without that role are skipped.
    role="evaluation" also keeps every image of a case whose latest case profile
    is EVALUATION. Observation-loop frames are grouped by their arm pose.
    """
    db = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    try:
        latest = {}
        for sample_id, label in db.execute("select sample_id, label from annotations order by id"):
            latest[sample_id] = label
        def has(table):
            return db.execute("select 1 from sqlite_master where type='table' and name=?", (table,)).fetchone()

        roles, eval_samples = {}, set()
        if role is not None:
            if has("dataset_roles"):
                for sample_id, value in db.execute("select sample_id, role from dataset_roles order by id"):
                    roles[sample_id] = value
            if role == "evaluation" and has("case_profiles"):
                case_role = {}
                for case_id, value in db.execute("select case_id, role from case_profiles order by id"):
                    case_role[case_id] = value
                recorded = {session_id: json.loads(details).get("box_id")
                            for session_id, details in db.execute("select id, details from sessions")}
                moved = {}
                if has("case_assignments"):  # 観察ノートのケース訂正: the latest one wins
                    for sample_id, case_id in db.execute("select sample_id, case_id from case_assignments order by id"):
                        moved[sample_id] = case_id
                for sample_id, session_id in db.execute("select id, session_id from samples"):
                    if case_role.get(moved.get(sample_id, recorded.get(session_id))) == "evaluation":
                        eval_samples.add(sample_id)
        items = []
        for sample_id, session_id, image_path, meta in db.execute(
                "select id, session_id, image_path, metadata from samples"):
            label = latest.get(sample_id)
            if label not in LABEL_TO_ID or sample_id in exclude_ids:
                continue
            reserved = roles.get(sample_id) == "evaluation_only"
            if role == "evaluation_only" and not reserved:
                continue
            if role == "evaluation" and not (reserved or sample_id in eval_samples):
                continue
            metadata = json.loads(meta) if meta else {}
            path = Path(image_path)
            if not path.is_absolute():
                path = Path(captures_root) / path
            group = f"jpeg_{metadata.get('width')}x{metadata.get('height')}"
            if metadata.get("camera_pose"):
                group += "_" + metadata["camera_pose"]
            items.append({"id": sample_id, "path": path, "label": LABEL_TO_ID[label], "group": group})
        return items
    finally:
        db.close()


def model_input_size(interpreter):
    """Square input side from the model ([1, H, W, 3]); 96 and 192 are supported."""
    _, height, width, _ = (int(v) for v in interpreter.get_input_details()[0]["shape"])
    if height != width:
        raise ValueError(f"expected a square model input, got {height}x{width}")
    return height


def evaluate(interpreter, items, order=DEVICE_INPUT_ORDER):
    size = model_input_size(interpreter)
    rows = []
    for item in items:
        probs = predict(interpreter, load_image(item["path"], order, size))
        rows.append({"id": item["id"], "group": item.get("group", "all"), "label": item["label"],
                     "p_visible": round(probs[1], 4), "pred": int(probs[1] > probs[0])})
    groups = defaultdict(list)
    for row in rows:
        groups[row["group"]].append(row)
    return {"input_order": order, "input_size": size, "all": summarize(rows),
            "by_group": {g: summarize(rs) for g, rs in sorted(groups.items())}, "rows": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", action="append", required=True, help="name=path to an INT8 (non-Vela) .tflite")
    parser.add_argument("--db", type=Path, default=ROOT / "captures/collection.sqlite3")
    parser.add_argument("--exclude-manifest", type=Path, action="append", default=[],
                        help="dataset_manifest.json whose training images are excluded")
    parser.add_argument("--order", choices=("RGB", "BGR"), action="append",
                        help="input channel order(s); default RGB (device order)")
    parser.add_argument("--role", choices=("evaluation_only", "evaluation"),
                        help="evaluation_only: images reserved one by one; "
                             "evaluation: those plus every image of an evaluation case")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    exclude = set()
    for manifest in args.exclude_manifest:
        exclude |= {r["image_id"] for r in json.loads(manifest.read_text(encoding="utf-8"))["images"]}
    items = collect_labelled(args.db, args.db.parent, exclude, args.role)
    report = {"schema": "bll.vision_pc_eval.v1", "scope": "HUMAN_LABELLED_NOT_IN_EXCLUDED_TRAINING_SETS",
              "independent_evaluation": False,
              "note": "Same case/period frames may be included; bench evidence only.",
              "excluded_training_images": len(exclude), "role_filter": args.role,
              "items": len(items), "models": {}}
    for spec in args.model:
        name, _, path = spec.partition("=")
        interpreter = load_interpreter(path)
        report["models"][name] = {"path": path, "results": [evaluate(interpreter, items, order)
                                                           for order in (args.order or [DEVICE_INPUT_ORDER])]}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for name, model in report["models"].items():
        for result in model["results"]:
            s = result["all"]
            print(f"{name} {result['input_order']} {result['input_size']}px: n={s['n']} correct={s['correct']} "
                  f"acc={s['accuracy']} 95%={s['accuracy_wilson95']} balanced={s['balanced_accuracy']} "
                  f"auc={s['auc']} confusion={s['confusion_rows_true_cols_pred']}")
    return report


if __name__ == "__main__":
    main()
