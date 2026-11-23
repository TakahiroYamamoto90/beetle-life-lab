"""Trial boundaries, no training or real images in these host tests."""
import copy
import unittest

from train_trial import validate_trial


def fixture():
    return {"schema": "bll.vision_trial_dataset.v1", "purpose": "TRAIN_ONLY_PIPELINE_CHECK",
            "deployment_eligible": False, "independent_evaluation_available": False,
            "validation": [], "test": [], "class_order": ["NO_VISIBLE_LARVA", "VISIBLE_LARVA"],
            "counts": {"NO_VISIBLE_LARVA": 1, "VISIBLE_LARVA": 1},
            "images": [{"image_id": "fixture-" + str(i), "source_kind": "REAL_CAMERA",
                        "split": "train", "label": label, "class_id": i,
                        "original_exclusion_reasons": ["MISSING_DEVICE_ID"]}
                       for i, label in enumerate(["NO_VISIBLE_LARVA", "VISIBLE_LARVA"])]}


class TrialContractTests(unittest.TestCase):
    def test_explicit_train_only_contract_with_metadata_gaps_is_accepted(self):
        validate_trial(fixture())

    def test_trial_cannot_claim_deployment_or_independent_evaluation(self):
        for field in ("deployment_eligible", "independent_evaluation_available"):
            row = fixture()
            row[field] = True
            with self.assertRaises(ValueError):
                validate_trial(row)
        for field in ("validation", "test"):
            row = fixture()
            row[field] = ["fixture-heldout"]
            with self.assertRaises(ValueError):
                validate_trial(row)

    def test_synthetic_unreviewed_setup_or_missing_history_never_bypass_trial_gate(self):
        for reason in ("NOT_VERIFIED_REAL_CAMERA", "NO_HUMAN_REVIEW", "SETUP", "LABEL_UNREVIEWED", "LABEL_AMBIGUOUS", "LABEL_UNUSABLE", "LABEL_EXCLUDED"):
            row = fixture()
            row["images"][0]["original_exclusion_reasons"].append(reason)
            with self.assertRaises(ValueError):
                validate_trial(row)
        row = fixture()
        row["images"][0]["source_kind"] = "SYNTHETIC"
        with self.assertRaises(ValueError):
            validate_trial(row)

    def test_labels_class_order_counts_and_duplicate_ids_fail_closed(self):
        mutations = [lambda row: row["class_order"].reverse(),
                     lambda row: row["images"][0].update(class_id=1),
                     lambda row: row["counts"].update(VISIBLE_LARVA=2),
                     lambda row: row["images"][1].update(image_id="fixture-0")]
        for mutate in mutations:
            row = copy.deepcopy(fixture())
            mutate(row)
            with self.assertRaises(ValueError):
                validate_trial(row)


    def test_conditions_name_the_poses_and_the_size_limit_is_600(self):
        from prepare_trial import MAX_TRIAL_IMAGES, TRIAL_IMAGE_ID, row_pose, trial_poses
        base = {"schema": "bll.vision_trial_conditions.v1", "source": "user"}
        self.assertEqual(trial_poses(dict(base, camera_pose="FIXED")), {"FIXED"})  # older conditions files
        self.assertEqual(trial_poses(dict(base, camera_poses=["FIXED", "TOP"])), {"FIXED", "TOP"})
        for bad in (dict(base), dict(base, camera_poses=[]), dict(base, camera_poses=["SIDE"]),
                    dict(base, camera_poses=["TOP", "TOP"]), {"schema": "x", "camera_pose": "FIXED", "source": "u"},
                    {"schema": "bll.vision_trial_conditions.v1", "camera_pose": "FIXED"}):
            with self.assertRaises(ValueError):
                trial_poses(bad)
        self.assertEqual((row_pose({"camera_pose": None}), row_pose({"camera_pose": "BACK"})), ("FIXED", "BACK"))
        self.assertTrue(TRIAL_IMAGE_ID.fullmatch("obs-" + "a" * 32 + "-12"))
        self.assertIsNone(TRIAL_IMAGE_ID.fullmatch("obs-" + "a" * 32 + "-0"))
        row = fixture()
        row["images"] = [dict(row["images"][i % 2], image_id=f"fixture-{i}") for i in range(MAX_TRIAL_IMAGES)]
        row["counts"] = {"NO_VISIBLE_LARVA": MAX_TRIAL_IMAGES // 2, "VISIBLE_LARVA": MAX_TRIAL_IMAGES // 2}
        validate_trial(row)
        row["images"].append(dict(row["images"][0], image_id="fixture-extra"))
        row["counts"]["NO_VISIBLE_LARVA"] += 1
        with self.assertRaises(ValueError):
            validate_trial(row)

if __name__ == "__main__":
    unittest.main()
