"""Freeze reviewed real images for TRAIN-ONLY experiments; never qualifies a deployment dataset."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

from dataset import CONFIG, digest, validate_records, split_manifest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "host/vision_capture"))
from session_store import SessionStore

ALLOWED_TRIAL_GAPS = {
    "MISSING_DEVICE_ID", "MISSING_ILLUMINATION_PROFILE", "MISSING_ACQUISITION_PROTOCOL_ID",
    "MISSING_CONTINUITY_GROUP", "CAMERA_POSE_UNVERIFIED",
}
# v13 (2026-11-18): fixed-camera images plus the arm views of the observation loop, raised from 300.
MAX_TRIAL_IMAGES = 600
POSES = ("FIXED", "FRONT", "TOP", "BACK")
TRIAL_IMAGE_ID = re.compile(r"vision-[0-9a-f]{32}|obs-[0-9a-f]{32}-[1-9][0-9]{0,9}")
SOURCE_JPEG_SIZE = (240, 240)


def jpeg_size(data_root, row):
    from PIL import Image
    with Image.open(Path(data_root) / row["image"]) as image:
        return image.format, image.size


def split_by_source_size(rows, data_root):
    """Keep 240x240 JPEGs. 480x480 ones came from Vision FW 2024.08.19 (SenseCraft) and
    are left out of trials as a different acquisition condition; never resized here."""
    kept, skipped = [], []
    for row in rows:
        fmt, size = jpeg_size(data_root, row)
        if fmt == "JPEG" and tuple(size) == SOURCE_JPEG_SIZE:
            kept.append(row)
        else:
            skipped.append({"image_id": row["image_id"], "label": row["label"],
                            "format": fmt, "size": list(size),
                            "reason": "SOURCE_NOT_240_JPEG_DIFFERENT_ACQUISITION_CONDITION"})
    return kept, skipped


def trial_poses(conditions):
    """Poses the user-reported conditions cover: camera_poses (list), or the older camera_pose FIXED."""
    if conditions.get("schema") != "bll.vision_trial_conditions.v1" or not conditions.get("source"):
        raise ValueError("explicit user-reported conditions required")
    poses = conditions.get("camera_poses", [conditions.get("camera_pose")])
    if not isinstance(poses, list) or not poses or len(set(poses)) != len(poses) or set(poses) - set(POSES):
        raise ValueError("camera_poses must list FIXED, FRONT, TOP and/or BACK")
    return set(poses)


def row_pose(row):
    # Sessions from before the camera arm recorded no pose; they all used the fixed camera.
    return row["camera_pose"] or "FIXED"


def prepare(data_root, output, conditions):
    poses = trial_poses(conditions)
    all_rows = SessionStore(data_root, readonly=True).classification_snapshot()
    rows, skipped_source_size = split_by_source_size(all_rows, data_root)
    features = validate_records(rows, data_root)
    original_split = split_manifest(rows, features)
    selected = [row for row in rows if row["label"] in CONFIG["class_order"] and row_pose(row) in poses and
                not (set(features[row["image_id"]]["excluded"]) - ALLOWED_TRIAL_GAPS)]
    counts = Counter(row["label"] for row in selected)
    if any(not counts[label] for label in CONFIG["class_order"]):
        raise ValueError("both human-reviewed real-camera classes are required")
    if len(selected) > MAX_TRIAL_IMAGES:
        raise ValueError(f"trial is bounded to {MAX_TRIAL_IMAGES} images; review scope before expanding")
    # The conditions record is supplementary provenance. It never rewrites collection metadata.
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    images = []
    for row in selected:
        if not TRIAL_IMAGE_ID.fullmatch(row["image_id"]):
            raise ValueError("unsafe capture ID")
        label_id = CONFIG["class_order"].index(row["label"])
        relative = Path("train") / row["label"] / (row["image_id"] + ".jpg")
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        content = (Path(data_root) / row["image"]).read_bytes()
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise ValueError("source JPEG changed during snapshot")
        target.write_bytes(content)
        images.append({"image_id": row["image_id"], "image": relative.as_posix(),
                       "sha256": row["sha256"], "label": row["label"], "class_id": label_id,
                       "split": "train", "capture_session_id": row["capture_session_id"],
                       "case_id": row["case_id"], "source_kind": row["source_kind"],
                       "camera_pose": row_pose(row), "camera_pose_recorded": row["camera_pose"] is not None,
                       "original_exclusion_reasons": features[row["image_id"]]["excluded"]})
    manifest = {
        "schema": "bll.vision_trial_dataset.v1", "purpose": "TRAIN_ONLY_PIPELINE_CHECK",
        "class_order": CONFIG["class_order"], "counts": counts,
        "counts_by_pose": {pose: dict(Counter(r["label"] for r in selected if row_pose(r) == pose))
                           for pose in POSES if pose in poses},
        "snapshot_sha256": digest(all_rows),
        "skipped_source_size": skipped_source_size, "conditions": conditions,
        "conditions_sha256": digest(conditions), "images": images,
        "validation": [], "test": [], "independent_evaluation_available": False,
        "source_sessions": len({row["capture_session_id"] for row in selected}),
        "source_cases": len({row["case_id"] for row in selected}),
        "production_split_status": original_split["status"],
        "production_blockers": original_split["blockers"],
        "metadata_gaps_allowed_for_trial_only": sorted(ALLOWED_TRIAL_GAPS),
        "deployment_eligible": False,
    }
    (output / "dataset_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "source_snapshot.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in all_rows), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=ROOT / "captures")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--conditions", type=Path, required=True)
    parser.add_argument("--acknowledge-train-only", action="store_true", required=True)
    args = parser.parse_args()
    result = prepare(args.data_root, args.output, json.loads(args.conditions.read_text(encoding="utf-8")))
    print(json.dumps({"purpose": result["purpose"], "counts": result["counts"],
                      "evaluation_available": False, "deployment_eligible": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
