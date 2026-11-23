"""Offline audit and leakage-safe manifests. Never touches serial ports or trains a model."""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator
from PIL import Image, __version__ as pillow_version

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "dataset_config.json").read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(json.loads(
    (HERE / "schemas/image_label.schema.json").read_text(encoding="utf-8")))
LABEL_MAP = {"larva_visible": "VISIBLE_LARVA", "background": "NO_VISIBLE_LARVA",
             "uncertain": "AMBIGUOUS", "unusable": "UNUSABLE",
             "unreviewed": "UNREVIEWED", "exclude": "EXCLUDED"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("explicit UTC required")
    return parsed


def exclusion_reasons(row):
    reasons = []
    if row["purpose"] != "dataset":
        reasons.append("SETUP")
    capture = row["capture"]
    if (row["source_kind"] != "REAL_CAMERA" or capture.get("simulated") is not False or
            capture.get("source") != "grove_vision_ai_v2_via_xiao_i2c_usb"):
        reasons.append("NOT_VERIFIED_REAL_CAMERA")
    if row["label"] not in CONFIG["class_order"]:
        reasons.append("LABEL_" + row["label"])
    if not row["annotation_history"]:
        reasons.append("NO_HUMAN_REVIEW")
    for field in ("case_id", "device_id", "illumination_profile", "acquisition_protocol_id",
                  "continuity_group"):
        if not row[field] or not row[field].strip():
            reasons.append("MISSING_" + field.upper())
    if row["camera_pose"] != "FIXED":
        reasons.append("CAMERA_POSE_UNVERIFIED")
    if row.get("dataset_role") == "EVALUATION_ONLY":
        reasons.append("RESERVED_FOR_EVALUATION")
    if row.get("case_role") == "EVALUATION":
        reasons.append("EVALUATION_CASE")
    if not all(row["pc_capture_bracket"].values()):
        reasons.append("MISSING_PC_CAPTURE_BRACKET")
    return reasons


def validate_records(records, root):
    """Fail closed on corrupt records/images; incomplete metadata stays explicitly excluded."""
    if pillow_version != "12.0.0":
        raise ValueError("install ml/vision/requirements.txt (Pillow pin affects dHash)")
    if len(records) > CONFIG["max_images"]:
        raise ValueError("max_images exceeded; review the bounded near-duplicate audit")
    root = Path(root).resolve(strict=True)
    ids = set()
    features = {}
    for row in records:
        VALIDATOR.validate(row)
        image_id = row["image_id"]
        if image_id in ids:
            raise ValueError("duplicate image_id: " + image_id)
        ids.add(image_id)
        # Resolve symlinks/junctions and reject absolute paths as well as traversal.
        relative = Path(row["image"])
        if relative.is_absolute() or relative.drive:
            raise ValueError("image must be relative to root")
        path = (root / relative).resolve(strict=True)
        path.relative_to(root)
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual_hash != row["sha256"] or row["capture"].get("sha256") != actual_hash:
            raise ValueError("JPEG SHA256 mismatch: " + image_id)
        if row["capture"].get("capture_id") != image_id:
            raise ValueError("capture_id mismatch: " + image_id)
        if row["session"].get("id") != row["capture_session_id"]:
            raise ValueError("session mismatch: " + image_id)
        # ARM sessions (observation-loop runs) record the pose per frame.
        expected_pose = (row["capture"].get("camera_pose") if row["session"].get("camera_pose") == "ARM"
                         else row["session"].get("camera_pose"))
        if (row["camera_pose"] or None) != (expected_pose or None):
            raise ValueError("session field mismatch: camera_pose")
        # A human case correction (観察ノート) replaces the case recorded with the session.
        moves = row.get("case_assignment_history") or []
        previous_move = 0
        for move in moves:
            utc(move["set_at_utc"])
            if move["assignment_id"] <= previous_move:
                raise ValueError("invalid case assignment history: " + image_id)
            previous_move = move["assignment_id"]
        if row["case_id"] != (moves[-1]["case_id"] if moves else row["session"].get("box_id")):
            raise ValueError("session field mismatch: case_id")
        for field, session_key in (("larva_id", "larva_id"),
                                   ("device_id", "device_id"),
                                   ("illumination_profile", "illumination_profile"),
                                   ("acquisition_protocol_id", "acquisition_protocol_id"),
                                   ("continuity_group", "continuity_group"), ("purpose", "purpose")):
            if (row[field] or None) != (row["session"].get(session_key) or None):
                raise ValueError("session field mismatch: " + field)
        bracket = row["pc_capture_bracket"]
        for field, key in (("request", "request_sent_utc_pc"), ("received", "received_utc_pc")):
            if bracket[field] != row["capture"].get(key):
                raise ValueError("capture bracket mismatch")
            if bracket[field]:
                utc(bracket[field])
        if all(bracket.values()) and utc(bracket["request"]) > utc(bracket["received"]):
            raise ValueError("reversed PC capture bracket")
        previous_id = 0
        for annotation in row["annotation_history"]:
            utc(annotation["reviewed_at_utc"])
            if (annotation["annotation_id"] <= previous_id or
                    LABEL_MAP[annotation["original_label"]] != annotation["label"]):
                raise ValueError("invalid annotation history: " + image_id)
            # Ordering follows SQLite IDs, not the PC wall clock (which may jump).
            previous_id = annotation["annotation_id"]
        latest = row["annotation_history"][-1] if row["annotation_history"] else {}
        if (row["label"] != latest.get("label", "UNREVIEWED") or
                (row["labeler"] or None) != (latest.get("labeler") or None)):
            raise ValueError("latest annotation mismatch: " + image_id)
        with Image.open(path) as image:
            if image.format != "JPEG" or image.size != (240, 240):
                raise ValueError("expected original 240x240 JPEG: " + image_id)
            image.load()
            pixels = list(image.convert("L").resize((9, 8), Image.Resampling.LANCZOS).getdata())
        dhash = sum((pixels[y * 9 + x] > pixels[y * 9 + x + 1]) << (y * 8 + x)
                    for y in range(8) for x in range(8))
        features[image_id] = {"dhash64": f"{dhash:016x}", "excluded": exclusion_reasons(row)}
    # Identical bytes with conflicting binary human labels require re-review, not a split.
    labels_by_hash = {}
    for row in records:
        if row["label"] in CONFIG["class_order"]:
            labels_by_hash.setdefault(row["sha256"], set()).add(row["label"])
    if any(len(labels) > 1 for labels in labels_by_hash.values()):
        raise ValueError("identical JPEG has conflicting human labels; re-review required")
    return features


def split_manifest(records, features, previous=None):
    """Whole connected components only; includes excluded rows when finding links."""
    rows = sorted(records, key=lambda row: row["image_id"])
    parent = list(range(len(rows)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        a, b = find(a), find(b)
        parent[max(a, b)] = min(a, b)

    seen = {}
    for i, row in enumerate(rows):
        for field in CONFIG["group_fields"] + ["sha256"]:
            if row.get(field):
                key = (field, row[field])
                if key in seen:
                    union(i, seen[key])
                seen[key] = i
    near_pairs = []
    near_pair_count = 0
    for i, row in enumerate(rows):
        value = int(features[row["image_id"]]["dhash64"], 16)
        for j in range(i):
            other = rows[j]
            distance = (value ^ int(features[other["image_id"]]["dhash64"], 16)).bit_count()
            if distance <= CONFIG["near_duplicate_max_hamming"]:
                union(i, j)
                near_pair_count += 1
                if len(near_pairs) < 1000:
                    near_pairs.append({"a": other["image_id"], "b": row["image_id"], "hamming": distance,
                                       "labels_differ": other["label"] != row["label"]})
    groups = {}
    for i, row in enumerate(rows):
        groups.setdefault(find(i), []).append(row["image_id"])
    group_ids = {key: digest(members) for key, members in groups.items()}
    eligible_groups = sorted(
        (group_ids[key] for key, members in groups.items()
         if any(not features[member]["excluded"] for member in members)),
        key=lambda group: digest([CONFIG["seed"], group]))
    assignments = {}
    blockers = []
    if previous is not None:
        if (previous.get("schema") != "bll.vision_split_manifest.v1" or
                previous.get("status") != "READY_FOR_DATA_REVIEW" or
                previous.get("config_sha256") != digest(CONFIG) or
                previous.get("config") != CONFIG):
            raise ValueError("previous manifest must be READY with identical config")
        old = {item["image_id"]: item for item in previous["images"]}
        current = {row["image_id"]: row for row in rows}
        if not old.keys() <= current.keys():
            raise ValueError("frozen manifest image removed")
        for image_id, item in old.items():
            if item.get("record_sha256") != digest(current[image_id]):
                raise ValueError("frozen image/annotation/conditions changed: " + image_id)
        for key, members in groups.items():
            inherited = {old[member]["split"] for member in members
                         if member in old and old[member]["split"] is not None}
            if len(inherited) > 1:
                blockers.append("FROZEN_SPLIT_COLLISION:" + group_ids[key])
            elif inherited:
                assignments[group_ids[key]] = inherited.pop()
            elif group_ids[key] in eligible_groups:
                # Unseen independent data cannot enlarge or reshuffle frozen VAL/TEST.
                assignments[group_ids[key]] = "train"
    elif len(eligible_groups) >= 3:
        n = len(eligible_groups)
        val_n = max(1, int(n * CONFIG["ratios"]["validation"]))
        test_n = max(1, int(n * CONFIG["ratios"]["test"]))
        train_n = n - val_n - test_n
        for i, group in enumerate(eligible_groups):
            assignments[group] = "train" if i < train_n else "validation" if i < n - test_n else "test"
    items = []
    counts = {split: Counter() for split in ("train", "validation", "test")}
    for i, row in enumerate(rows):
        feature = features[row["image_id"]]
        group_id = group_ids[find(i)]
        excluded = list(feature["excluded"])
        if (previous is not None and row["image_id"] not in old and
                assignments.get(group_id) in {"validation", "test"}):
            excluded.append("NEW_IMAGE_IN_FROZEN_EVALUATION_GROUP")
        split = assignments.get(group_id) if not excluded else None
        if split:
            counts[split][row["label"]] += 1
        items.append({"image_id": row["image_id"], "sha256": row["sha256"],
                      "record_sha256": digest(row),
                      "capture_session_id": row["capture_session_id"], "label": row["label"],
                      "group_id": group_id, "split": split, **feature, "excluded": excluded})
    if len(eligible_groups) < 3:
        blockers.append("FEWER_THAN_THREE_INDEPENDENT_GROUPS")
    for split, count in counts.items():
        for label in CONFIG["class_order"]:
            if not count[label]:
                blockers.append("MISSING_CLASS:" + split + ":" + label)
    return {"schema": "bll.vision_split_manifest.v1", "config": CONFIG,
            "previous_manifest_sha256": digest(previous) if previous else None,
            "config_sha256": digest(CONFIG), "input_sha256": digest(rows),
            "status": "NO_GO" if blockers else "READY_FOR_DATA_REVIEW",
            "blockers": blockers, "counts": counts, "independent_eligible_groups": len(eligible_groups),
            "near_duplicate_pair_count": near_pair_count,
            "near_duplicate_pairs_first_1000": near_pairs, "images": items,
            "limitations": ["dHash does not prove independence; human leakage audit required",
                            "No training, threshold selection, performance or physical validation performed"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "split"))
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True, help="collection root containing sessions/")
    parser.add_argument("--output", type=Path, required=True, help="new JSON artifact; never overwritten")
    parser.add_argument("--previous-manifest", type=Path,
                        help="required operationally after first READY split: preserve frozen VAL/TEST")
    args = parser.parse_args(argv)
    try:
        records = [json.loads(line) for line in args.snapshot.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        features = validate_records(records, args.root)
        previous = (json.loads(args.previous_manifest.read_text(encoding="utf-8"))
                    if args.previous_manifest else None)
        if previous is not None and args.command != "split":
            raise ValueError("--previous-manifest is for split only")
        report = split_manifest(records, features, previous) if args.command == "split" else {
            "schema": "bll.vision_validation_report.v1", "input_sha256": digest(sorted(records, key=lambda r: r["image_id"])),
            "images": len(records), "eligible": sum(not f["excluded"] for f in features.values()),
            "features": features, "training_performed": False}
        # Exclusive creation protects a frozen test split. New data requires a new version/output.
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(json.dumps({"image_count": len(records), **{
            key: value for key, value in report.items() if key in
            {"status", "blockers", "eligible", "independent_eligible_groups"}}}, ensure_ascii=False))
        return 2 if report.get("status") == "NO_GO" else 0
    except Exception as exc:
        print("DATASET_FAILED " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
