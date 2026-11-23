"""Observation-loop import into the collection store. Synthetic fixture run in a temp dir.

The fixture bytes are a hand-made minimal JPEG; the run is marked LIVE only to exercise
the importer's filters inside a temporary store, never the real collection database.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from observation_import import import_run, jpeg_size
from session_store import SessionStore

RUN = "e" * 32
BOOT = "b" * 32
# SOI, APP0 stub, SOF0 240x240, EOI
JPEG = (b"\xff\xd8\xff\xe0\x00\x04\x00\x00\xff\xc0\x00\x0b\x08\x00\xf0\x00\xf0\x01\x01\x11\x00"
        + b"\x11" * 8 + b"\xff\xd9")


def write_run(root, origin="LIVE", tamper=None, case_id=""):
    run_dir = Path(root) / "ad3" / f"ad3-{RUN}"
    (run_dir / "observations").mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps(dict(
        run_id=RUN, origin=origin, request=dict(case_id=case_id, note="tap test"),
        xiao_initial_state=dict(device_id="xiao-0123456789ab"))), encoding="utf-8")
    steps = []
    for n, (action, pose, verified) in enumerate(
            [("CAPTURE_FRONT", "FRONT", True), ("CAPTURE_TOP", None, True),
             ("CAPTURE_BACK", "BACK", False)], start=1):
        action_id = f"obs-{BOOT}-{n}"
        sha = hashlib.sha256(JPEG).hexdigest()
        (run_dir / "observations" / f"{action_id}.jpg").write_bytes(JPEG)
        sidecar = dict(action_id=action_id, received_at_pc="2026-11-11T01:05:09+00:00",
                       hash_matches_xiao=True, jpeg_markers_ok=True, image_sha256=sha,
                       xiao_payload_sha256="f" * 64, transport_sha256="f" * 64)
        if n == 1:
            sidecar["vision_model_output"] = dict(
                reference_only=True, pose="FRONT", raw_classes_score_target=[[96, 1]],
                prediction=dict(status="MODEL_OUTPUT_UNVERIFIED",
                                top=dict(class_id=1, class_name="VISIBLE_LARVA", score=96)))
        if tamper == n:
            sidecar["image_sha256"] = "0" * 64
        (run_dir / "observations" / f"{action_id}.json").write_text(json.dumps(sidecar), encoding="utf-8")
        steps.append(dict(action=action, target_pose=pose, action_id=action_id, image_verified=verified,
                          trigger_event_id="xiao-0123456789ab/x/2", decision_event_id=f"d{n}",
                          result=dict(capture="SUCCESS", result_event_id=f"r{n}"),
                          reason_codes=[], follow_up=n > 1))
    lines = [dict(candidate_seq=1, status="sending", received_at_pc="2026-11-11T01:04:50+00:00"),
             dict(candidate_seq=1, status="acknowledged", observation=steps[0], follow_ups=steps[1:]),
             dict(candidate_seq=2, status="sending", received_at_pc="2026-11-11T01:05:20+00:00"),
             dict(candidate_seq=2, status="acknowledged",
                  observation=dict(action="WAIT", action_id=None, result=None), follow_ups=[])]
    (run_dir / "delivery.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return run_dir


class ObservationImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SessionStore(self.temporary.name)

    def test_verified_frames_become_unreviewed_samples_per_pose(self):
        run_dir = write_run(self.temporary.name)
        report = import_run(self.store, run_dir, case_id="box-02")
        # BACK was not verified against the XIAO hash; the WAIT has no frame.
        self.assertEqual(report["imported"], [f"obs-{BOOT}-1", f"obs-{BOOT}-2"])
        self.assertIn((f"obs-{BOOT}-3", "image not verified against the XIAO hash"), report["skipped"])
        # One session per run; each frame keeps its own pose.
        [session] = self.store.sessions()
        self.assertEqual(session["camera_pose"], "ARM")
        self.assertEqual(session["continuity_group"], "ad3-" + RUN)
        self.assertEqual((session["box_id"], session["acquisition"]), ("box-02", "OBSERVATION_LOOP"))
        self.assertEqual(session["source_run_id"], RUN)
        self.assertEqual(self.store.sample(f"obs-{BOOT}-2")["metadata"]["camera_pose"], "TOP")  # from the action name
        front = self.store.sample(f"obs-{BOOT}-1")
        self.assertEqual(front["label"], "unreviewed")
        meta = front["metadata"]
        self.assertEqual((meta["camera_pose"], meta["width"], meta["height"]), ("FRONT", 240, 240))
        self.assertEqual(meta["model_inference"]["prediction"]["top"]["class_name"], "VISIBLE_LARVA")
        self.assertEqual(meta["request_sent_utc_pc"], "2026-11-11T01:04:50+00:00")
        self.assertEqual(meta["provenance"]["result_event_id"], "r1")
        self.assertTrue((run_dir / "observations" / f"obs-{BOOT}-1.jpg").is_file())  # original kept
        self.assertNotIn("model_inference", self.store.sample(f"obs-{BOOT}-2")["metadata"])

    def test_rerun_is_idempotent_and_snapshot_keeps_pose(self):
        run_dir = write_run(self.temporary.name)
        import_run(self.store, run_dir, case_id="box-02")
        again = import_run(self.store, run_dir, case_id="box-02")
        self.assertEqual(again["imported"], [])
        self.assertEqual(len(self.store.samples()), 2)
        self.assertEqual(len(self.store.sessions()), 1)
        rows = {r["image_id"]: r for r in self.store.classification_snapshot()}
        row = rows[f"obs-{BOOT}-2"]
        self.assertEqual((row["camera_pose"], row["source_kind"], row["label"]), ("TOP", "REAL_CAMERA", "UNREVIEWED"))
        self.assertEqual(row["continuity_group"], "ad3-" + RUN)
        # Not a training export until a human labels it.
        self.assertEqual(self.store.export(), [])

    def test_mock_runs_tampered_files_and_missing_case_are_refused(self):
        run_dir = write_run(self.temporary.name, origin="SYNTHETIC")
        self.assertEqual(import_run(self.store, run_dir, case_id="box-02")["imported"], [])
        other = tempfile.TemporaryDirectory(); self.addCleanup(other.cleanup)
        store = SessionStore(other.name)
        run_dir = write_run(other.name, tamper=1)
        with self.assertRaisesRegex(ValueError, "ケースID"):
            import_run(store, run_dir)
        report = import_run(store, run_dir, purpose="setup")
        self.assertEqual(report["imported"], [f"obs-{BOOT}-2"])
        self.assertIn((f"obs-{BOOT}-1", "image file does not match its sidecar"), report["skipped"])

    def test_dry_run_writes_nothing_and_jpeg_size(self):
        run_dir = write_run(self.temporary.name, case_id="box-02")
        self.assertEqual(len(import_run(self.store, run_dir, dry_run=True)["imported"]), 2)
        self.assertEqual(self.store.samples(), [])
        self.assertEqual(jpeg_size(JPEG), (240, 240))
        with self.assertRaises(ValueError):
            jpeg_size(b"not a jpeg")


    def test_collection_server_serves_imported_frames_by_action_id(self):
        from server import App
        import_run(self.store, write_run(self.temporary.name), case_id="box-02")
        app = App(self.temporary.name)
        path = app.image_path(f"obs-{BOOT}-1")
        self.assertEqual(path.read_bytes(), JPEG)
        for bad in ("obs-../x", f"obs-{BOOT}-0", "../collection.sqlite3"):
            with self.assertRaises(ValueError):
                app.image_path(bad)

    def test_merge_converts_per_pose_sessions_and_keeps_labels(self):
        run_dir = write_run(self.temporary.name)
        # Sessions as the first importer version made them: one per run and pose.
        moved = {}
        for n, pose in ((1, "FRONT"), (2, "TOP")):
            old = self.store.create_session(dict(
                box_id="box-02", device_id="xiao-0123456789ab", camera_pose=pose, source_run_id=RUN,
                acquisition="OBSERVATION_LOOP", continuity_group="ad3-" + RUN,
                notes=f"AD3観測ループ ad3-{RUN} から取込み（{pose}）。 メモ: tap test"))
            folder = Path(self.temporary.name) / "sessions" / old["id"]
            folder.mkdir(parents=True)
            image = folder / f"obs-{BOOT}-{n}.jpg"
            image.write_bytes(JPEG)
            image.with_suffix(".json").write_text(json.dumps(dict(
                capture_id=image.stem, simulated=False, camera_pose=pose,
                sha256=hashlib.sha256(JPEG).hexdigest())), encoding="utf-8")
            self.store.add_capture(old["id"], image)
            moved[image.stem] = old["id"]
        self.store.set_session_title(moved[f"obs-{BOOT}-1"], {"title": "old"})
        self.store.annotate(f"obs-{BOOT}-1", {"label": "background"})
        from observation_import import merge_run_sessions
        self.assertEqual(merge_run_sessions(self.store, dry_run=True)[0]["samples"], 2)
        self.assertEqual(len(self.store.sessions()), 2)  # dry run changed nothing
        [report] = merge_run_sessions(self.store)
        [session] = self.store.sessions()
        self.assertEqual((session["id"], session["camera_pose"]), (report["new_session"], "ARM"))
        self.assertIn("メモ: tap test", session["notes"])
        self.assertEqual(session["title"], "")
        for sample_id, old_id in moved.items():
            sample = self.store.sample(sample_id)
            self.assertEqual(sample["session_id"], session["id"])
            self.assertEqual((Path(self.temporary.name) / sample["image_path"]).read_bytes(), JPEG)
            self.assertFalse((Path(self.temporary.name) / "sessions" / old_id).exists())
        self.assertEqual(self.store.sample(f"obs-{BOOT}-1")["label"], "background")
        rows = {r["image_id"]: r for r in self.store.classification_snapshot()}
        self.assertEqual(rows[f"obs-{BOOT}-2"]["camera_pose"], "TOP")
        # New imports of the same run now go into the merged session.
        self.assertEqual(import_run(self.store, run_dir, case_id="box-02")["imported"], [])
        self.assertEqual(len(self.store.sessions()), 1)


class CaseProfileAndTitleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.store = SessionStore(root)
        import_run(self.store, write_run(root), case_id="box-02")
        self.session = self.store.sessions()[0]
        manifest = root / "train_manifest.json"
        manifest.write_text(json.dumps({"images": [
            {"image_id": f"obs-{BOOT}-1", "split": "train"}]}), encoding="utf-8")
        registry = root / "sets.json"
        registry.write_text(json.dumps({"models": [
            {"model": "v-test", "training_manifest": "train_manifest.json"},
            {"model": "v-missing", "training_manifest": "nope.json"}]}), encoding="utf-8")
        from server import App
        self.app = App(root, training_sets=registry, training_base=root)

    def test_case_profile_is_appended_and_reaches_snapshot(self):
        self.assertEqual(self.store.cases()[0]["role"], "unassigned")
        case = self.store.set_case_profile("box-02", {"display_name": "容器2（評価用）", "role": "training"})
        self.assertEqual((case["display_name"], case["role"], case["samples"]), ("容器2（評価用）", "training", 2))
        rows = self.store.classification_snapshot()
        self.assertEqual({r["case_role"] for r in rows}, {"TRAINING"})
        self.assertEqual(len(rows[0]["case_role_history"]), 1)
        with self.assertRaises(ValueError):
            self.store.set_case_profile("no-such-case", {"role": "training"})
        with self.assertRaises(ValueError):
            self.store.set_case_profile("box-02", {"role": "test"})

    def test_images_move_to_another_or_new_case_with_history(self):
        first, second = f"obs-{BOOT}-1", f"obs-{BOOT}-2"
        before = self.store.session(self.session["id"])
        result = self.store.assign_case({"sample_ids": [second], "case_id": " box-03 ", "reason": "取り違え"})
        self.assertEqual((result["moved"], result["unchanged"], result["case_id"]), ([second], [], "box-03"))
        moved = self.store.sample(second)
        self.assertEqual((moved["case_id"], moved["recorded_case_id"], moved["case_corrected"]), ("box-03", "box-02", True))
        self.assertEqual(self.store.sample(first)["case_corrected"], False)
        self.assertEqual(self.store.session(self.session["id"]), before)  # the session record never changes
        cases = {c["case_id"]: c for c in self.store.cases()}
        self.assertEqual((cases["box-02"]["samples"], cases["box-03"]["samples"]), (1, 1))
        self.assertEqual(self.store.set_case_profile("box-03", {"role": "evaluation"})["role"], "evaluation")
        rows = {r["image_id"]: r for r in self.store.classification_snapshot()}
        self.assertEqual((rows[second]["case_id"], rows[second]["case_role"]), ("box-03", "EVALUATION"))
        self.assertEqual([m["case_id"] for m in rows[second]["case_assignment_history"]], ["box-03"])
        self.assertNotIn("case_assignment_history", rows[first])  # unchanged rows keep their digests
        self.assertEqual(rows[first]["case_id"], "box-02")
        # Moving again to the same case changes nothing; null returns to the recorded case.
        self.assertEqual(self.store.assign_case({"sample_ids": [second], "case_id": "box-03"})["moved"], [])
        self.assertEqual(self.store.assign_case({"sample_ids": [first, second], "case_id": None})["moved"], [second])
        rows = {r["image_id"]: r for r in self.store.classification_snapshot()}
        self.assertEqual(rows[second]["case_id"], "box-02")
        self.assertEqual([m["case_id"] for m in rows[second]["case_assignment_history"]], ["box-03", "box-02"])
        for bad in ({"sample_ids": [], "case_id": "box-03"}, {"sample_ids": [first, first], "case_id": "box-03"},
                    {"sample_ids": ["nope"], "case_id": "box-03"}, {"sample_ids": [first], "case_id": "box 03"},
                    {"sample_ids": [first], "case_id": ""}, {"sample_ids": [first], "case_id": "../x"}):
            with self.assertRaises(ValueError):
                self.store.assign_case(bad)
        self.assertEqual(self.store.sample(first)["case_id"], "box-02")  # refused moves change nothing

    def test_trained_images_cannot_move_into_an_evaluation_case(self):
        self.store.assign_case({"sample_ids": [f"obs-{BOOT}-2"], "case_id": "box-09"})
        self.app.set_case_profile({"case_id": "box-09", "role": "evaluation"})
        with self.assertRaisesRegex(ValueError, "v-test"):
            self.app.assign_case({"sample_ids": [f"obs-{BOOT}-1"], "case_id": "box-09"})
        self.assertEqual(self.store.sample(f"obs-{BOOT}-1")["case_id"], "box-02")
        # A trained image that moved makes its new case unfit for evaluation.
        self.app.assign_case({"sample_ids": [f"obs-{BOOT}-1"], "case_id": "box-10"})
        with self.assertRaisesRegex(ValueError, "v-test"):
            self.app.set_case_profile({"case_id": "box-10", "role": "evaluation"})

    def test_deleted_session_is_hidden_everywhere_and_restores_unchanged(self):
        session_id, second = self.session["id"], f"obs-{BOOT}-2"
        self.store.annotate(second, {"label": "background", "reviewer": "r"})
        self.store.set_session_title(session_id, {"title": "テスト"})
        before = (self.store.session(session_id), self.store.samples(), self.store.classification_snapshot())
        files = sorted(p.name for p in (self.store.root / "sessions" / session_id).iterdir())
        result = self.store.set_session_deleted(session_id, True, {"reason": " テスト撮影 ", "editor": "r"})
        self.assertEqual((result["deleted"], result["samples"]), (True, 2))
        self.assertEqual((self.store.sessions(), self.store.samples(), self.store.cases()), ([], [], []))
        self.assertEqual((self.store.export(), self.store.classification_snapshot()), ([], []))
        for call in (lambda: self.store.session(session_id), lambda: self.store.sample(second),
                     lambda: self.store.annotate(second, {"label": "background"}),
                     lambda: self.store.set_session_title(session_id, {"title": "x"}),
                     lambda: self.store.set_session_deleted(session_id, True, {})):
            with self.assertRaises(ValueError):
                call()
        [trash] = self.store.deleted_sessions()
        self.assertEqual((trash["id"], trash["samples"], trash["delete_reason"], trash["title"]),
                         (session_id, 2, "テスト撮影", "テスト"))
        # The files stay; a re-import skips the frames instead of bringing them back.
        self.assertEqual(sorted(p.name for p in (self.store.root / "sessions" / session_id).iterdir()), files)
        report = import_run(self.store, self.store.root / "ad3" / f"ad3-{RUN}", case_id="box-02")
        self.assertEqual((report["imported"], self.store.sessions()), ([], []))
        self.store.set_session_deleted(session_id, False, {})
        self.assertEqual((self.store.session(session_id), self.store.samples(), self.store.classification_snapshot()), before)
        self.assertEqual(self.store.deleted_sessions(), [])
        with self.assertRaises(ValueError):
            self.store.set_session_deleted(session_id, False, {})
        with self.assertRaises(ValueError):
            self.store.set_session_deleted("session-" + "0" * 32, True, {})

    def test_sessions_in_a_frozen_dataset_cannot_be_deleted(self):
        with self.assertRaisesRegex(ValueError, "v-test"):
            self.app.delete_session({"session_id": self.session["id"]})
        self.assertEqual(len(self.store.sessions()), 1)
        self.app.frozen = {}
        self.assertTrue(self.app.delete_session({"session_id": self.session["id"]})["deleted"])
        self.assertEqual(self.app.state()["deleted_sessions"][0]["id"], self.session["id"])

    def test_session_title_does_not_touch_recorded_details(self):
        before = self.store.session(self.session["id"])
        after = self.store.set_session_title(self.session["id"], {"title": "11/11 観測ループ 1回目"})
        self.assertEqual(after["title"], "11/11 観測ループ 1回目")
        self.assertEqual({k: v for k, v in after.items() if k != "title"},
                         {k: v for k, v in before.items() if k != "title"})
        snapshot_session = self.store.classification_snapshot()[0]["session"]
        self.assertNotIn("title", snapshot_session)  # audit export keeps the recorded conditions only

    def test_trained_images_cannot_become_evaluation(self):
        self.assertEqual(self.app.trained, {f"obs-{BOOT}-1": ["v-test"]})
        self.assertEqual([m["status"] for m in self.app.training_models], ["OK", "MANIFEST_MISSING"])
        with self.assertRaisesRegex(ValueError, "v-test"):
            self.app.set_case_profile({"case_id": "box-02", "role": "evaluation"})
        with self.assertRaisesRegex(ValueError, "v-test"):
            self.app.set_dataset_role({"sample_id": f"obs-{BOOT}-1", "role": "evaluation_only"})
        self.app.set_dataset_role({"sample_id": f"obs-{BOOT}-2", "role": "evaluation_only"})
        self.assertEqual(self.app.set_case_profile({"case_id": "box-02", "role": "training"})["role"], "training")
        state = self.app.state()
        self.assertEqual(state["cases"][0]["case_id"], "box-02")
        self.assertIn(f"obs-{BOOT}-1", state["trained"])


if __name__ == "__main__":
    unittest.main()
