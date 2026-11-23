"""Host-only checks. Fixture samples are explicitly simulated and never exportable."""
import json
from pathlib import Path
import tempfile
import unittest

from session_store import SessionStore, utc_now, exportable


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = SessionStore(self.temporary.name)

    def fixture_sample(self, purpose="dataset"):
        session = self.store.create_session({"box_id": "fixture-box", "purpose": purpose})
        metadata = {"simulated": True, "source": "unit_test_fixture", "sha256": "fixture"}
        sample_id = "vision-" + "0" * 32
        with self.store.connect() as db:
            db.execute("INSERT INTO samples VALUES(?,?,?,?,?)",
                       (sample_id, session["id"], utc_now(), "fixture.jpg", json.dumps(metadata)))
        return sample_id

    def test_dataset_requires_box_but_setup_does_not(self):
        with self.assertRaises(ValueError):
            self.store.create_session({})
        self.assertEqual(self.store.create_session({"purpose": "setup"})["box_id"], "")

    def test_invalid_measurements_rejected(self):
        for field, value in (("distance_mm", "0"), ("distance_mm", "nan"), ("temperature_c", "200")):
            with self.assertRaises(ValueError):
                self.store.create_session({"box_id": "fixture", field: value})

    def test_annotation_history_and_default_unreviewed(self):
        sample_id = self.fixture_sample()
        self.assertEqual(self.store.sample(sample_id)["label"], "unreviewed")
        self.store.annotate(sample_id, {"label": "uncertain", "reviewer": "unit-test"})
        self.store.annotate(sample_id, {"label": "exclude", "reviewer": "unit-test"})
        self.assertEqual(self.store.sample(sample_id)["label"], "exclude")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM annotations").fetchone()[0], 2)

    def test_reviewer_optional_but_valid_label_required(self):
        sample_id = self.fixture_sample()
        saved = self.store.annotate(sample_id, {"label": "background", "note": "fixture note"})
        self.assertEqual(saved["reviewer"], "")
        self.assertEqual(saved["label"], "background")
        self.assertEqual(saved["note"], "fixture note")
        self.assertIsNone(self.store.classification_snapshot()[0]["labeler"])
        with self.assertRaises(ValueError):
            self.store.annotate(sample_id, {"label": "invalid"})

    def test_dataset_role_is_separate_history_and_excludes_from_training(self):
        sample_id = self.fixture_sample()
        self.assertEqual(self.store.sample(sample_id)["dataset_role"], "unassigned")
        self.store.annotate(sample_id, {"label": "background"})
        saved = self.store.set_dataset_role(sample_id, {"role": "evaluation_only"})
        self.assertEqual(saved["dataset_role"], "evaluation_only")
        self.assertEqual(saved["label"], "background")  # the visibility label is untouched
        row = self.store.classification_snapshot()[0]
        self.assertEqual(row["dataset_role"], "EVALUATION_ONLY")
        self.assertEqual([r["role"] for r in row["dataset_role_history"]], ["EVALUATION_ONLY"])
        self.assertEqual(len(row["annotation_history"]), 1)
        self.store.set_dataset_role(sample_id, {"role": "unassigned"})
        row = self.store.classification_snapshot()[0]
        self.assertEqual(row["dataset_role"], "UNASSIGNED")
        self.assertEqual(len(row["dataset_role_history"]), 2)
        with self.assertRaises(ValueError):
            self.store.set_dataset_role(sample_id, {"role": "train"})
        with self.assertRaises(ValueError):
            self.store.set_dataset_role("vision-missing", {"role": "evaluation_only"})

    def test_readonly_store_without_role_table_reads_as_unassigned(self):
        import sqlite3
        sample_id = self.fixture_sample()
        db = sqlite3.connect(Path(self.temporary.name) / "collection.sqlite3")
        try:
            db.execute("DROP TABLE dataset_roles")
            db.commit()
        finally:
            db.close()  # Windows cannot delete the temp dir while it is open
        readonly = SessionStore(self.temporary.name, readonly=True)
        self.assertEqual(readonly.sample(sample_id)["dataset_role"], "unassigned")
        self.assertEqual(readonly.classification_snapshot()[0]["dataset_role"], "UNASSIGNED")

    def test_simulated_sample_never_exported_even_if_reviewed(self):
        sample_id = self.fixture_sample()
        self.store.annotate(sample_id, {"label": "background", "reviewer": "unit-test"})
        self.assertEqual(self.store.export(), [])

    def test_unknown_ids_rejected(self):
        with self.assertRaises(ValueError):
            self.store.annotate("missing", {"label": "unreviewed"})
        with self.assertRaises(ValueError):
            self.store.session("../outside")

    def test_sample_exposes_absolute_image_path_within_store_root(self):
        from pathlib import Path
        sample_id = self.fixture_sample()
        expected = (Path(self.temporary.name) / "fixture.jpg").resolve()
        self.assertEqual(Path(self.store.sample(sample_id)["image_file_path"]), expected)
        with self.store.connect() as db:
            db.execute("UPDATE samples SET image_path=? WHERE id=?", ("../outside.jpg", sample_id))
        with self.assertRaises(ValueError):
            self.store.sample(sample_id)

    def test_session_survives_store_restart(self):
        session = self.store.create_session({"box_id": "fixture-box"})
        restarted = SessionStore(self.temporary.name)
        self.assertEqual(restarted.session(session["id"])["box_id"], "fixture-box")

    def test_export_policy_for_reviewed_unreviewed_and_setup(self):
        for label in ("larva_visible", "background"):
            self.assertTrue(exportable("dataset", label, False))
            self.assertFalse(exportable("setup", label, False))
        for label in ("unreviewed", "uncertain", "exclude"):
            self.assertFalse(exportable("dataset", label, False))
        self.assertFalse(exportable("dataset", "background", None))

    def test_capture_lock_prevents_overlapping_serial_operations(self):
        from server import App
        app = App(Path(self.temporary.name) / "app")
        with app.serial_lock:
            with self.assertRaises(BlockingIOError):
                app.capture_one({"mode": "preview"})

    def test_classification_snapshot_preserves_legacy_history_and_unknown_time(self):
        sample_id = self.fixture_sample()
        before = self.store.classification_snapshot()[0]
        self.assertEqual(before["label"], "UNREVIEWED")
        self.assertEqual(before["source_kind"], "UNVERIFIED")
        self.assertIsNone(before["device_id"])
        self.assertIsNone(before["captured_at_utc"])
        self.store.annotate(sample_id, {"label": "uncertain", "reviewer": "unit-test"})
        self.store.annotate(sample_id, {"label": "unusable", "reviewer": "unit-test"})
        snapshot = self.store.classification_snapshot()[0]
        self.assertEqual(snapshot["label"], "UNUSABLE")
        self.assertEqual([a["label"] for a in snapshot["annotation_history"]], ["AMBIGUOUS", "UNUSABLE"])
        self.assertEqual(self.store.sample(sample_id)["label"], "unusable")
        self.assertEqual(self.store.export(), [])

    def test_new_collection_conditions_persist_without_schema_migration(self):
        fields = {"box_id": "fixture-box", "device_id": "fixture-device", "camera_pose": "FIXED",
                  "illumination_profile": "fixture-light", "acquisition_protocol_id": "fixture-v1",
                  "continuity_group": "fixture-continuous-run"}
        session = self.store.create_session(fields)
        restarted = SessionStore(self.temporary.name)
        for key, value in fields.items():
            self.assertEqual(restarted.session(session["id"])[key], value)
        with self.assertRaises(ValueError):
            self.store.create_session({**fields, "camera_pose": "TOP_SERVO"})

    def test_readonly_snapshot_cannot_mutate_database(self):
        import sqlite3
        sample_id = self.fixture_sample()
        readonly = SessionStore(self.temporary.name, readonly=True)
        self.assertEqual(readonly.classification_snapshot()[0]["image_id"], sample_id)
        with self.assertRaises(sqlite3.OperationalError):
            readonly.annotate(sample_id, {"label": "exclude", "reviewer": "unit-test"})
        with self.assertRaises(ValueError):
            SessionStore(Path(self.temporary.name) / "missing", readonly=True)

    def test_snapshot_http_keeps_unreviewed_and_legacy_export_excludes_it(self):
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        import threading
        from server import App, make_handler
        sample_id = self.fixture_sample()
        app = App(self.temporary.name)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        httpd.daemon_threads = True
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        connection = HTTPConnection("127.0.0.1", httpd.server_port, timeout=3)
        try:
            connection.request("GET", "/api/classification-snapshot")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("attachment", response.getheader("Content-Disposition"))
            records = [json.loads(line) for line in response.read().splitlines()]
            self.assertEqual(records[0]["image_id"], sample_id)
            self.assertEqual(records[0]["label"], "UNREVIEWED")
            connection.request("GET", "/api/export")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
            connection.request("GET", "/api/classification-snapshot", headers={"Host": "untrusted.invalid"})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            connection.close()
            app.close()
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
