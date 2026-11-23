"""Audit the human-reviewed image collection before planning model training."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys

from session_store import SessionStore


def audit(data_root):
    root = Path(data_root).resolve()
    if not (root / "collection.sqlite3").is_file():
        raise ValueError(f"collection database not found: {root / 'collection.sqlite3'}")
    store = SessionStore(root)
    rows = store.export()
    issues = []
    labels = Counter()
    cases = defaultdict(Counter)
    larvae_cases = defaultdict(set)
    valid_rows = 0

    for row in rows:
        label = row["label"]
        labels[label] += 1
        cases[row["box_id"]][label] += 1
        if row["larva_id"]:
            larvae_cases[row["larva_id"]].add(row["box_id"])

        image_path = (root / row["image"]).resolve()
        try:
            image_path.relative_to(root)
        except ValueError:
            issues.append({"capture_id": row["capture_id"], "issue": "image_path_outside_data_root"})
            continue
        if not image_path.is_file():
            issues.append({"capture_id": row["capture_id"], "issue": "image_missing"})
            continue
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
        if digest != row["sha256"]:
            issues.append({"capture_id": row["capture_id"], "issue": "sha256_mismatch"})
            continue
        if label not in {"larva_visible", "background"}:
            issues.append({"capture_id": row["capture_id"], "issue": "unexpected_export_label"})
            continue
        valid_rows += 1

    cross_case_larvae = sorted(key for key, values in larvae_cases.items() if len(values) > 1)
    if cross_case_larvae:
        issues.append({"issue": "larva_id_used_across_cases", "larva_ids": cross_case_larvae})

    return {
        "schema": "bll.vision_dataset_audit.v1",
        "data_root": str(root),
        "exportable_rows": len(rows),
        "valid_image_rows": valid_rows,
        "labels": dict(sorted(labels.items())),
        "cases": {key: dict(sorted(value.items())) for key, value in sorted(cases.items())},
        "distinct_cases": len(cases),
        "issues": issues,
        "status": "issues_found" if issues else ("empty" if not rows else "reviewed"),
        "split": "unassigned",
        "model_training": "not_performed",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("captures"))
    args = parser.parse_args()
    try:
        report = audit(args.data_dir)
    except (OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
