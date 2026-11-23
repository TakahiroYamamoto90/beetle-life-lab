"""Synthetic, local-only fixtures. No hardware, biological data, training or LIVE Evidence."""
import copy
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import random
import shutil
import unittest
import uuid

from PIL import Image
from jsonschema.exceptions import ValidationError
from dataset import CONFIG, exclusion_reasons, main, split_manifest, validate_records


def fixture(image_id="fixture-0", label="VISIBLE_LARVA"):
    session = {"id": "session-" + image_id, "box_id": "case-" + image_id,
               "larva_id": None, "device_id": "fixture-device", "camera_pose": "FIXED",
               "illumination_profile": "fixture-light", "acquisition_protocol_id": "fixture-protocol",
               "continuity_group": "continuity-" + image_id, "purpose": "dataset"}
    return {"schema": "bll.vision_image_label.v1", "image_id": image_id, "image": image_id + ".jpg",
            "sha256": hashlib.sha256(image_id.encode()).hexdigest(),
            "capture_session_id": session["id"], "case_id": session["box_id"], "larva_id": None,
            **{field: session[field] for field in ("device_id", "camera_pose", "illumination_profile",
                                                   "acquisition_protocol_id", "continuity_group", "purpose")},
            "captured_at_utc": None, "timestamp_quality": "EXPOSURE_UNSYNCED",
            "pc_capture_bracket": {"request": "2026-11-01T00:00:00+00:00", "received": "2026-11-01T00:00:01Z"},
            "source_kind": "SYNTHETIC", "label": label, "labeler": "fixture-reviewer",
            "label_version": "human-visible-presence-v1",
            "annotation_history": [{"annotation_id": 1, "label": label,
                                    "original_label": "larva_visible" if label == "VISIBLE_LARVA" else "background",
                                    "labeler": "fixture-reviewer", "reviewed_at_utc": "2026-11-01T00:01:00Z",
                                    "note": "unit test, no biological observation", "label_version": "human-visible-presence-v1"}],
            "capture": {"capture_id": image_id, "simulated": True, "source": "unit_test_fixture"},
            "session": session}


class DatasetTests(unittest.TestCase):
    def setUp(self):
        # Windows Store Python's mode-0700 TemporaryDirectory can deny the sandbox token.
        # Inherit the workspace ACL; never change system Temp permissions.
        repo = Path(__file__).resolve().parents[2]
        work = (repo / "captures/test_tmp").resolve()
        work.relative_to(repo)
        work.mkdir(parents=True, exist_ok=True)
        self.root = work / ("vision-ml-" + uuid.uuid4().hex)
        self.root.mkdir()

        def cleanup():
            resolved = self.root.resolve()
            resolved.relative_to(work)
            if resolved == work:
                raise ValueError("refuse to remove fixture parent")
            shutil.rmtree(resolved)
        self.addCleanup(cleanup)

    def image_row(self, image_id="fixture-0"):
        row = fixture(image_id)
        image = Image.frombytes("RGB", (240, 240), random.Random(image_id).randbytes(240 * 240 * 3))
        image.save(self.root / row["image"], format="JPEG")
        row["sha256"] = hashlib.sha256((self.root / row["image"]).read_bytes()).hexdigest()
        row["capture"].update(sha256=row["sha256"], request_sent_utc_pc=row["pc_capture_bracket"]["request"],
                              received_utc_pc=row["pc_capture_bracket"]["received"])
        return row

    def test_synthetic_is_valid_but_never_training_eligible(self):
        row = self.image_row()
        features = validate_records([row], self.root)
        self.assertIn("NOT_VERIFIED_REAL_CAMERA", features[row["image_id"]]["excluded"])
        self.assertEqual(len(features[row["image_id"]]["dhash64"]), 16)

    def test_unknown_schema_fields_and_invalid_labels_fail(self):
        row = self.image_row()
        for key, value in (("model_presence", "VISIBLE_CONFIRMED"), ("label", "NORMAL")):
            broken = copy.deepcopy(row)
            broken[key] = value
            with self.assertRaises(ValidationError):
                validate_records([broken], self.root)

    def test_integrity_and_root_escape_fail(self):
        row = self.image_row()
        broken = copy.deepcopy(row)
        broken["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "SHA256"):
            validate_records([broken], self.root)
        for path in (str((self.root / row["image"]).resolve()), "../outside.jpg"):
            broken = copy.deepcopy(row)
            broken["image"] = path
            with self.assertRaises((ValueError, FileNotFoundError)):
                validate_records([broken], self.root)

    def test_duplicate_ids_and_conflicting_human_labels_fail(self):
        row = self.image_row()
        with self.assertRaisesRegex(ValueError, "duplicate image_id"):
            validate_records([row, row], self.root)
        other = copy.deepcopy(row)
        other["image_id"] = "fixture-1"
        other["capture"]["capture_id"] = other["image_id"]
        other["label"] = "NO_VISIBLE_LARVA"
        other["annotation_history"][0].update(label="NO_VISIBLE_LARVA", original_label="background")
        with self.assertRaisesRegex(ValueError, "conflicting"):
            validate_records([row, other], self.root)

    def test_unknown_exposure_never_fabricated_and_bracket_checked(self):
        row = self.image_row()
        row["captured_at_utc"] = "2026-11-01T00:00:00Z"
        with self.assertRaises(ValidationError):
            validate_records([row], self.root)
        row["captured_at_utc"] = None
        row["pc_capture_bracket"]["received"] = "2026-10-31T00:00:00Z"
        row["capture"]["received_utc_pc"] = row["pc_capture_bracket"]["received"]
        with self.assertRaisesRegex(ValueError, "reversed"):
            validate_records([row], self.root)

    def test_history_latest_label_and_session_are_bound(self):
        row = self.image_row()
        row["labeler"] = None
        row["annotation_history"][0]["labeler"] = ""
        self.assertNotIn("NO_HUMAN_REVIEW", validate_records([row], self.root)[row["image_id"]]["excluded"])
        for mutate in (lambda r: r.update(label="NO_VISIBLE_LARVA"),
                       lambda r: r.update(case_id="other-case"),
                       lambda r: r["annotation_history"][0].update(original_label="exclude")):
            broken = copy.deepcopy(row)
            mutate(broken)
            with self.assertRaises(ValueError):
                validate_records([broken], self.root)

    def test_case_correction_replaces_the_recorded_case_only_with_history(self):
        row = self.image_row()
        moved = copy.deepcopy(row)
        moved["case_id"] = "case-corrected"
        moved["case_assignment_history"] = [{"assignment_id": 3, "case_id": "case-corrected", "reason": "取り違え",
                                             "set_by": None, "set_at_utc": "2026-11-17T00:00:00Z"}]
        validate_records([moved], self.root)
        for mutate in (lambda r: r.update(case_id=r["session"]["box_id"]),      # history says otherwise
                       lambda r: r.update(case_assignment_history=[]),           # empty history is not allowed
                       lambda r: r["case_assignment_history"].append(dict(r["case_assignment_history"][0]))):
            broken = copy.deepcopy(moved)
            mutate(broken)
            with self.assertRaises((ValueError, ValidationError)):
                validate_records([broken], self.root)

    def test_exclusion_rules_are_fail_closed(self):
        # Pure contract predicate exercise; these objects never enter a device or collection DB.
        row = fixture()
        row["source_kind"] = "REAL_CAMERA"
        row["capture"].update(simulated=False, source="grove_vision_ai_v2_via_xiao_i2c_usb")
        self.assertEqual(exclusion_reasons(row), [])
        row["labeler"] = None
        self.assertNotIn("NO_HUMAN_REVIEW", exclusion_reasons(row))
        missing_history = copy.deepcopy(row)
        missing_history["annotation_history"] = []
        self.assertIn("NO_HUMAN_REVIEW", exclusion_reasons(missing_history))
        for label in ("UNREVIEWED", "AMBIGUOUS", "UNUSABLE", "EXCLUDED"):
            row["label"] = label
            self.assertIn("LABEL_" + label, exclusion_reasons(row))
        row["label"] = "VISIBLE_LARVA"
        row["purpose"] = "setup"
        row["device_id"] = None
        row["capture"]["simulated"] = None
        self.assertTrue({"SETUP", "MISSING_DEVICE_ID", "NOT_VERIFIED_REAL_CAMERA"}.issubset(exclusion_reasons(row)))

    def partition_fixture(self):
        # Deliberately injected feature eligibility tests ONLY the pure partition function.
        rows, features = [], {}
        values = ["0000000000000000", "ffffffffffffffff", "aaaaaaaaaaaaaaaa"]
        for group in range(3):
            for klass, label in enumerate(CONFIG["class_order"]):
                row = fixture(f"fixture-{group}-{klass}", label)
                row["case_id"] = f"case-{group}"
                rows.append(row)
                features[row["image_id"]] = {"excluded": [], "dhash64": values[group]}
        return rows, features

    def test_deterministic_group_partition_both_classes_and_order(self):
        rows, features = self.partition_fixture()
        manifest = split_manifest(rows, features)
        self.assertEqual(manifest, split_manifest(list(reversed(rows)), features))
        self.assertEqual(manifest["status"], "READY_FOR_DATA_REVIEW")
        self.assertEqual(manifest["independent_eligible_groups"], 3)
        for count in manifest["counts"].values():
            self.assertEqual(dict(count), {"NO_VISIBLE_LARVA": 1, "VISIBLE_LARVA": 1})

    def test_each_leakage_link_merges_groups(self):
        for field in CONFIG["group_fields"] + ["sha256"]:
            rows, features = self.partition_fixture()
            rows[2][field] = rows[0][field] or "same-larva"
            rows[0][field] = rows[2][field]
            manifest = split_manifest(rows, features)
            self.assertEqual(manifest["status"], "NO_GO", field)
            items = {item["image_id"]: item for item in manifest["images"]}
            self.assertEqual(items[rows[0]["image_id"]]["group_id"], items[rows[2]["image_id"]]["group_id"])

    def test_near_duplicates_and_excluded_bridge_cannot_leak(self):
        rows, features = self.partition_fixture()
        # A group excluded from training still bridges two otherwise independent cases.
        bridge = fixture("fixture-bridge")
        bridge["case_id"] = rows[0]["case_id"]
        rows.append(bridge)
        features[bridge["image_id"]] = {"excluded": ["LABEL_UNREVIEWED"], "dhash64": "fffffffffffffffe"}
        manifest = split_manifest(rows, features)
        self.assertEqual(manifest["independent_eligible_groups"], 2)
        self.assertEqual(manifest["status"], "NO_GO")
        self.assertIsNone(next(i for i in manifest["images"] if i["image_id"] == bridge["image_id"])["split"])

    def test_empty_and_missing_class_are_no_go(self):
        self.assertEqual(split_manifest([], {})["status"], "NO_GO")
        rows, features = self.partition_fixture()
        for row in rows:
            row["label"] = "VISIBLE_LARVA"
        self.assertEqual(split_manifest(rows, features)["status"], "NO_GO")

    def test_incremental_manifest_freezes_old_assignments_and_new_groups_train_only(self):
        rows, features = self.partition_fixture()
        frozen = split_manifest(rows, features)
        original = {item["image_id"]: item["split"] for item in frozen["images"]}
        new = fixture("new-independent")
        rows.append(new)
        features[new["image_id"]] = {"excluded": [], "dhash64": "5555555555555555"}
        updated = split_manifest(rows, features, frozen)
        self.assertEqual(updated["status"], "READY_FOR_DATA_REVIEW")
        for item in updated["images"]:
            self.assertEqual(item["split"], original.get(item["image_id"], "train"))
        self.assertIsNotNone(updated["previous_manifest_sha256"])

    def test_incremental_cross_split_bridge_is_no_go(self):
        rows, features = self.partition_fixture()
        frozen = split_manifest(rows, features)
        bridge = fixture("new-bridge")
        bridge["case_id"] = rows[0]["case_id"]
        rows.append(bridge)
        features[bridge["image_id"]] = {"excluded": [], "dhash64": "ffffffffffffffff"}
        updated = split_manifest(rows, features, frozen)
        self.assertEqual(updated["status"], "NO_GO")
        self.assertTrue(any(reason.startswith("FROZEN_SPLIT_COLLISION") for reason in updated["blockers"]))

    def test_new_image_in_frozen_test_group_is_excluded_not_added_to_test(self):
        rows, features = self.partition_fixture()
        frozen = split_manifest(rows, features)
        test_id = next(item["image_id"] for item in frozen["images"] if item["split"] == "test")
        old = next(row for row in rows if row["image_id"] == test_id)
        new = fixture("new-in-test-case")
        new["case_id"] = old["case_id"]
        rows.append(new)
        features[new["image_id"]] = {"excluded": [], "dhash64": features[test_id]["dhash64"]}
        updated = split_manifest(rows, features, frozen)
        item = next(item for item in updated["images"] if item["image_id"] == new["image_id"])
        self.assertIsNone(item["split"])
        self.assertIn("NEW_IMAGE_IN_FROZEN_EVALUATION_GROUP", item["excluded"])
        self.assertEqual(updated["counts"], frozen["counts"])

    def test_frozen_record_removal_edit_or_config_change_rejected(self):
        rows, features = self.partition_fixture()
        frozen = split_manifest(rows, features)
        with self.assertRaisesRegex(ValueError, "removed"):
            split_manifest(rows[:-1], features, frozen)
        broken = copy.deepcopy(rows)
        broken[0]["label"] = "UNREVIEWED"
        with self.assertRaisesRegex(ValueError, "changed"):
            split_manifest(broken, features, frozen)
        frozen["config_sha256"] = "incorrect"
        with self.assertRaisesRegex(ValueError, "identical config"):
            split_manifest(rows, features, frozen)

    def test_cli_writes_no_go_but_never_overwrites_frozen_manifest(self):
        snapshot = self.root / "snapshot.jsonl"
        snapshot.write_text("", encoding="utf-8")
        output = self.root / "split.json"
        args = ["split", "--snapshot", str(snapshot), "--root", str(self.root), "--output", str(output)]
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(args), 2)
        original = output.read_bytes()
        with redirect_stderr(io.StringIO()):
            self.assertEqual(main(args), 1)
        self.assertEqual(original, output.read_bytes())


class DatasetRoleTests(unittest.TestCase):
    def test_evaluation_only_images_are_never_training_candidates(self):
        from dataset import exclusion_reasons
        row = {"purpose": "dataset", "source_kind": "REAL_CAMERA", "label": "VISIBLE_LARVA",
               "capture": {"simulated": False, "source": "grove_vision_ai_v2_via_xiao_i2c_usb"},
               "annotation_history": [{"label": "VISIBLE_LARVA"}], "case_id": "box", "device_id": "dev",
               "illumination_profile": "light", "acquisition_protocol_id": "proto",
               "continuity_group": "group", "camera_pose": "FIXED",
               "pc_capture_bracket": {"request": "t", "received": "t"}}
        self.assertEqual(exclusion_reasons(row), [])
        self.assertEqual(exclusion_reasons(dict(row, dataset_role="UNASSIGNED")), [])
        self.assertEqual(exclusion_reasons(dict(row, dataset_role="EVALUATION_ONLY")), ["RESERVED_FOR_EVALUATION"])
        self.assertEqual(exclusion_reasons(dict(row, case_role="TRAINING")), [])
        self.assertEqual(exclusion_reasons(dict(row, case_role="EVALUATION")), ["EVALUATION_CASE"])


class ArmSessionTests(unittest.TestCase):
    setUp = DatasetTests.setUp
    image_row = DatasetTests.image_row

    def test_arm_session_pose_comes_from_the_frame(self):
        row = self.image_row()
        row["session"]["camera_pose"] = "ARM"
        row["capture"]["camera_pose"] = "TOP"
        row["camera_pose"] = "TOP"
        row.update(case_role="EVALUATION", case_role_history=[])
        features = validate_records([row], self.root)
        self.assertIn("CAMERA_POSE_UNVERIFIED", features[row["image_id"]]["excluded"])
        self.assertIn("EVALUATION_CASE", features[row["image_id"]]["excluded"])
        row["camera_pose"] = "FRONT"
        with self.assertRaisesRegex(ValueError, "camera_pose"):
            validate_records([row], self.root)


if __name__ == "__main__":
    unittest.main()
