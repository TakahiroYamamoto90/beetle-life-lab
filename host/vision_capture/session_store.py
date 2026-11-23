"""Local capture records and human annotations; raw capture metadata stays immutable."""
from datetime import datetime, timezone
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import uuid

LABELS = {"unreviewed", "larva_visible", "background", "uncertain", "exclude"}
VISIBILITY_LABELS = {
    "unreviewed": "UNREVIEWED", "larva_visible": "VISIBLE_LARVA",
    "background": "NO_VISIBLE_LARVA", "uncertain": "AMBIGUOUS",
    "unusable": "UNUSABLE", "exclude": "EXCLUDED",
}
LABELS.add("unusable")
LABEL_VERSION = "human-visible-presence-v1"
# Dataset role is a separate human decision from the visibility label. Its own
# append-only history keeps the annotation table and its history unchanged.
DATASET_ROLES = {"unassigned": "UNASSIGNED", "evaluation_only": "EVALUATION_ONLY"}
# FIXED: the former fixed camera (all images before the servo arm). FRONT/TOP/BACK: arm
# views of the observation loop (imported from AD3 runs). Training still accepts FIXED only.
CAMERA_POSES = ("FIXED", "FRONT", "TOP", "BACK")
# A session's pose. ARM: one observation-loop run whose frames carry their own pose
# (FRONT/TOP/BACK) in the capture metadata.
SESSION_POSES = CAMERA_POSES + ("ARM",)
# A case (physical container) is used either to train or to evaluate, never both, so
# neighbouring frames of one case cannot end up on both sides. The per-image
# evaluation_only role stays as an exception inside a training case.
CASE_ROLES = {"unassigned": "UNASSIGNED", "training": "TRAINING", "evaluation": "EVALUATION"}
# Case corrections (2026-11-17): an image keeps its capture session and the case recorded at
# capture; a later human decision "this image belongs to case X" is appended to its own history.
# The latest one is the image's case everywhere (cases, roles, exports, snapshot).
CASE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_CASE_MOVE = 5000
# Session deletion (2026-11-18) is a recoverable trash: an append-only row hides the session
# and its images from every list, export and snapshot. Rows and image files stay as they were,
# so restoring brings back the labels, roles, case moves and titles unchanged.


def exportable(purpose, label, simulated):
    return purpose == "dataset" and simulated is False and label in {"larva_visible", "background"}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def short_text(value, field, limit=1000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{field}: invalid text")
    return value.strip()


class SessionStore:
    def __init__(self, root, *, readonly=False):
        self.root = Path(root).resolve()
        self.readonly = readonly
        self.database = self.root / "collection.sqlite3"
        if readonly:
            if not self.database.is_file():
                raise ValueError("collection database not found")
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, created TEXT NOT NULL, details TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS samples (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    created TEXT NOT NULL, image_path TEXT NOT NULL, metadata TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS annotations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sample_id TEXT NOT NULL REFERENCES samples(id), created TEXT NOT NULL,
                    label TEXT NOT NULL, reviewer TEXT NOT NULL, note TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS dataset_roles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sample_id TEXT NOT NULL REFERENCES samples(id), created TEXT NOT NULL,
                    role TEXT NOT NULL, reviewer TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS case_profiles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL,
                    created TEXT NOT NULL, display_name TEXT NOT NULL, role TEXT NOT NULL,
                    note TEXT NOT NULL, editor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS case_assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sample_id TEXT NOT NULL REFERENCES samples(id), created TEXT NOT NULL,
                    case_id TEXT NOT NULL, reason TEXT NOT NULL, editor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS session_titles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id), created TEXT NOT NULL,
                    title TEXT NOT NULL, editor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS session_deletions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id), created TEXT NOT NULL,
                    deleted INTEGER NOT NULL, reason TEXT NOT NULL, editor TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = (sqlite3.connect(self.database.as_uri() + "?mode=ro", uri=True, timeout=10)
              if self.readonly else sqlite3.connect(self.database, timeout=10))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def has_table(db, name):
        # Read-only stores may predate later tables; treat them as empty.
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    @classmethod
    def has_roles(cls, db):
        return cls.has_table(db, "dataset_roles")

    def _case_history(self, db):
        history = {}
        if self.has_table(db, "case_profiles"):
            for row in db.execute("SELECT * FROM case_profiles ORDER BY id"):
                history.setdefault(row["case_id"], []).append({
                    "profile_id": row["id"], "display_name": row["display_name"],
                    "role": CASE_ROLES[row["role"]], "note": row["note"],
                    "set_by": row["editor"] or None, "set_at_utc": row["created"]})
        return history

    def _titles(self, db):
        titles = {}
        if self.has_table(db, "session_titles"):
            for row in db.execute("SELECT session_id, title FROM session_titles ORDER BY id"):
                titles[row["session_id"]] = row["title"]
        return titles

    def _deletions(self, db):
        """session_id -> its latest delete/restore row, for sessions that were ever deleted."""
        latest = {}
        if self.has_table(db, "session_deletions"):
            for row in db.execute("SELECT * FROM session_deletions ORDER BY id"):
                latest[row["session_id"]] = row
        return latest

    def _deleted(self, db):
        return {session_id for session_id, row in self._deletions(db).items() if row["deleted"]}

    def case_profiles(self):
        """case_id -> latest profile {display_name, role (lower-case key), note, updated}."""
        with self.connect() as db:
            history = self._case_history(db)
        reverse = {value: key for key, value in CASE_ROLES.items()}
        return {case_id: {"display_name": items[-1]["display_name"], "role": reverse[items[-1]["role"]],
                          "note": items[-1]["note"], "updated": items[-1]["set_at_utc"]}
                for case_id, items in history.items()}

    def cases(self):
        """Every case named by a session or a case correction, with its latest profile and
        counts. Images count under their current case (after corrections)."""
        profiles = self.case_profiles()
        cases, members = {}, {}

        def entry(case_id):
            return cases.setdefault(case_id, {"case_id": case_id, "display_name": "", "role": "unassigned",
                                              "note": "", "updated": None, "sessions": 0, "samples": 0})
        for session in self.sessions():
            if session.get("box_id"):
                entry(session["box_id"])
                members.setdefault(session["box_id"], set()).add(session["id"])
        for sample in self.samples():
            if sample["case_id"]:
                entry(sample["case_id"])["samples"] += 1
                members.setdefault(sample["case_id"], set()).add(sample["session_id"])
        for case_id, item in cases.items():
            item["sessions"] = len(members.get(case_id, ()))
            if case_id in profiles:
                item.update(profiles[case_id])
        return sorted(cases.values(), key=lambda item: item["case_id"])

    def set_case_profile(self, case_id, data):
        """Append a display name / role change. The case ID itself never changes."""
        if not isinstance(case_id, str) or not any(c["case_id"] == case_id for c in self.cases()):
            raise ValueError("ケースが見つかりません")
        role = data.get("role", "unassigned")
        if role not in CASE_ROLES:
            raise ValueError("不明なケースの役割です")
        display_name = short_text(data.get("display_name", ""), "display_name", 120)
        note = short_text(data.get("note", ""), "note")
        editor = short_text(data.get("editor", ""), "editor", 120)
        with self.connect() as db:
            db.execute("INSERT INTO case_profiles(case_id,created,display_name,role,note,editor) VALUES(?,?,?,?,?,?)",
                       (case_id, utc_now(), display_name, role, note, editor))
        return next(item for item in self.cases() if item["case_id"] == case_id)

    def set_session_title(self, session_id, data):
        """Append a display title. Session ID and recorded conditions stay unchanged."""
        self.session(session_id)
        title = short_text(data.get("title", ""), "title", 120)
        editor = short_text(data.get("editor", ""), "editor", 120)
        with self.connect() as db:
            db.execute("INSERT INTO session_titles(session_id,created,title,editor) VALUES(?,?,?,?)",
                       (session_id, utc_now(), title, editor))
        return self.session(session_id)

    def create_session(self, data):
        purpose = data.get("purpose", "dataset")
        if purpose not in {"dataset", "setup"}:
            raise ValueError("purpose must be dataset or setup")
        details = {key: short_text(data.get(key, ""), key) for key in (
            "box_id", "larva_id", "substrate", "lighting", "distance_mm", "temperature_c", "notes",
            "device_id", "illumination_profile", "acquisition_protocol_id", "continuity_group",
            "source_run_id", "acquisition")}
        camera_pose = data.get("camera_pose", "FIXED")
        if camera_pose not in SESSION_POSES:
            raise ValueError("camera_pose must be FIXED, FRONT, TOP, BACK or ARM")
        details["camera_pose"] = camera_pose
        if purpose == "dataset" and not details["box_id"]:
            raise ValueError("データ収集ではケースIDを入力してください")
        for field, minimum, maximum in (("distance_mm", 1, 10000), ("temperature_c", -50, 100)):
            if details[field]:
                number = float(details[field])
                if not minimum <= number <= maximum:
                    raise ValueError(f"{field}: outside valid range")
        details.update(id="session-" + uuid.uuid4().hex, created=utc_now(), purpose=purpose)
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                       (details["id"], details["created"], json.dumps(details, ensure_ascii=False)))
        return details

    def sessions(self):
        """Sessions in use; deleted ones (trash) are left out."""
        with self.connect() as db:
            rows = db.execute("SELECT details FROM sessions ORDER BY created DESC").fetchall()
            titles = self._titles(db)
            deleted = self._deleted(db)
        sessions = [json.loads(row["details"]) for row in rows]
        sessions = [session for session in sessions if session["id"] not in deleted]
        for session in sessions:
            session["title"] = titles.get(session["id"], "")
        return sessions

    def session(self, session_id):
        with self.connect() as db:
            row = db.execute("SELECT details FROM sessions WHERE id=?", (session_id,)).fetchone()
            titles = self._titles(db) if row is not None else {}
            deleted = session_id in self._deleted(db) if row is not None else False
        if row is None:
            raise ValueError("セッションが見つかりません")
        if deleted:
            raise ValueError("このセッションは削除済みです（「削除したセッション」から元に戻せます）")
        return dict(json.loads(row["details"]), title=titles.get(session_id, ""))

    def deleted_sessions(self):
        """The trash: deleted sessions with when/why, and how many images they hold."""
        with self.connect() as db:
            latest = self._deletions(db)
            titles = self._titles(db)
            items = []
            for session_id, row in latest.items():
                if not row["deleted"]:
                    continue
                details = json.loads(db.execute("SELECT details FROM sessions WHERE id=?",
                                                (session_id,)).fetchone()["details"])
                count = db.execute("SELECT count(*) FROM samples WHERE session_id=?", (session_id,)).fetchone()[0]
                items.append(dict(details, title=titles.get(session_id, ""), samples=count,
                                  deleted_at=row["created"], delete_reason=row["reason"],
                                  deleted_by=row["editor"]))
        return sorted(items, key=lambda item: item["deleted_at"], reverse=True)

    def set_session_deleted(self, session_id, deleted, data):
        """Move a session to the trash (deleted=True) or bring it back. Appends one row;
        the session, its images, labels and files are not touched."""
        reason = short_text(data.get("reason", ""), "reason")
        editor = short_text(data.get("editor", ""), "editor", 120)
        with self.connect() as db:
            if not isinstance(session_id, str) or db.execute(
                    "SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None:
                raise ValueError("セッションが見つかりません")
            if (session_id in self._deleted(db)) == bool(deleted):
                raise ValueError("このセッションはすでに削除済みです" if deleted else "このセッションは削除されていません")
            count = db.execute("SELECT count(*) FROM samples WHERE session_id=?", (session_id,)).fetchone()[0]
            now = utc_now()
            db.execute("INSERT INTO session_deletions(session_id,created,deleted,reason,editor) VALUES(?,?,?,?,?)",
                       (session_id, now, 1 if deleted else 0, reason, editor))
        return {"session_id": session_id, "deleted": bool(deleted), "samples": count, "at_utc": now}

    def add_capture(self, session_id, image_path):
        self.session(session_id)
        image_path = Path(image_path).resolve()
        relative = image_path.relative_to(self.root).as_posix()
        metadata = json.loads(image_path.with_suffix(".json").read_text(encoding="utf-8"))
        if metadata.get("simulated") is not False:
            raise ValueError("実機画像のみ登録できます")
        if metadata.get("capture_id") != image_path.stem or not image_path.is_file():
            raise ValueError("画像とメタデータの対応が不正です")
        import hashlib
        if hashlib.sha256(image_path.read_bytes()).hexdigest() != metadata.get("sha256"):
            raise ValueError("画像ハッシュが一致しません")
        with self.connect() as db:
            db.execute("INSERT INTO samples VALUES (?, ?, ?, ?, ?)",
                       (image_path.stem, session_id, utc_now(), relative,
                        json.dumps(metadata, ensure_ascii=False)))
        return self.sample(image_path.stem)

    def has_sample(self, sample_id):
        """Whether the image is recorded at all, including in a deleted session (imports skip it)."""
        with self.connect() as db:
            return db.execute("SELECT 1 FROM samples WHERE id=?", (sample_id,)).fetchone() is not None

    def serialize_sample(self, row, recorded_case=None):
        item = dict(row)
        assigned = item.pop("assigned_case", None)
        # recorded_case_id: the case of the capture session; case_id: the current case.
        item["recorded_case_id"] = recorded_case or ""
        item["case_id"] = assigned if assigned is not None else item["recorded_case_id"]
        item["case_corrected"] = item["case_id"] != item["recorded_case_id"]
        item["metadata"] = json.loads(item["metadata"])
        item["image_url"] = "/api/images/" + item["id"]
        image_file = (self.root / item["image_path"]).resolve()
        try:
            image_file.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("画像パスが保存領域の外を指しています") from exc
        item["image_file_path"] = str(image_file)
        item["label"] = item["label"] or "unreviewed"
        item["dataset_role"] = item.get("dataset_role") or "unassigned"
        return item

    def samples(self, session_id=None):
        args = ()
        where = ""
        if session_id:
            where = " WHERE s.session_id=?"
            args = (session_id,)
        with self.connect() as db:
            role = ("""(SELECT role FROM dataset_roles WHERE id=(
                         SELECT max(id) FROM dataset_roles WHERE sample_id=s.id)) AS dataset_role"""
                    if self.has_roles(db) else "NULL AS dataset_role")
            assigned = ("""(SELECT case_id FROM case_assignments WHERE id=(
                            SELECT max(id) FROM case_assignments WHERE sample_id=s.id)) AS assigned_case"""
                        if self.has_table(db, "case_assignments") else "NULL AS assigned_case")
            sql = f"""SELECT s.*, a.label, a.reviewer, a.note, a.created AS reviewed_at, {role}, {assigned}
                      FROM samples s LEFT JOIN annotations a ON a.id=(
                        SELECT max(id) FROM annotations WHERE sample_id=s.id)"""
            rows = db.execute(sql + where + " ORDER BY s.created DESC", args).fetchall()
            recorded = {row["id"]: json.loads(row["details"]).get("box_id") or ""
                        for row in db.execute("SELECT id, details FROM sessions")}
            deleted = self._deleted(db)
        return [self.serialize_sample(row, recorded.get(row["session_id"])) for row in rows
                if row["session_id"] not in deleted]

    def sample(self, sample_id):
        # Keep lookup parameterized even for IDs received from HTTP clients.
        with self.connect() as db:
            row = db.execute("SELECT session_id FROM samples WHERE id=?", (sample_id,)).fetchone()
        if row is None:
            raise ValueError("画像が見つかりません")
        found = [item for item in self.samples(row["session_id"]) if item["id"] == sample_id]
        if not found:
            raise ValueError("この画像のセッションは削除済みです")
        return found[0]

    def annotate(self, sample_id, data):
        self.sample(sample_id)
        label = data.get("label")
        if label not in LABELS:
            raise ValueError("不明なラベルです")
        reviewer = short_text(data.get("reviewer", ""), "reviewer", 120)
        note = short_text(data.get("note", ""), "note")
        with self.connect() as db:
            db.execute("INSERT INTO annotations(sample_id,created,label,reviewer,note) VALUES(?,?,?,?,?)",
                       (sample_id, utc_now(), label, reviewer, note))
        return self.sample(sample_id)

    def set_dataset_role(self, sample_id, data):
        """Append a role change. evaluation_only images are never offered for training."""
        self.sample(sample_id)
        role = data.get("role")
        if role not in DATASET_ROLES:
            raise ValueError("不明な用途です")
        reviewer = short_text(data.get("reviewer", ""), "reviewer", 120)
        with self.connect() as db:
            db.execute("INSERT INTO dataset_roles(sample_id,created,role,reviewer) VALUES(?,?,?,?)",
                       (sample_id, utc_now(), role, reviewer))
        return self.sample(sample_id)

    def assign_case(self, data):
        """Move images to another (or a new) case, or back to the case recorded at capture
        (case_id null). Appends one history row per image that changes; all or nothing.
        The capture session, its recorded case and the image files never change."""
        ids = data.get("sample_ids")
        if (not isinstance(ids, list) or not ids or len(ids) > MAX_CASE_MOVE or
                not all(isinstance(i, str) for i in ids) or len(set(ids)) != len(ids)):
            raise ValueError("移す画像を選んでください")
        target = data.get("case_id")
        if target is not None:
            if not isinstance(target, str) or not CASE_ID_PATTERN.fullmatch(target.strip()):
                raise ValueError("ケースIDは英数字で始まり、英数字・. _ - の64文字以内にしてください（例: box-03）")
            target = target.strip()
        reason = short_text(data.get("reason", ""), "reason")
        editor = short_text(data.get("editor", ""), "editor", 120)
        current = {item["id"]: item for item in self.samples()}
        missing = [i for i in ids if i not in current]
        if missing:
            raise ValueError("画像が見つかりません: " + ", ".join(missing[:3]))
        now, moved, unchanged = utc_now(), [], []
        rows = []
        for sample_id in ids:
            item = current[sample_id]
            wanted = item["recorded_case_id"] if target is None else target
            if not wanted:
                raise ValueError("撮影時のケースがない画像は「戻す」ことができません")
            if wanted == item["case_id"]:
                unchanged.append(sample_id)
                continue
            rows.append((sample_id, now, wanted, reason, editor))
            moved.append(sample_id)
        if rows:
            with self.connect() as db:
                db.executemany("INSERT INTO case_assignments(sample_id,created,case_id,reason,editor) "
                               "VALUES(?,?,?,?,?)", rows)
        return {"moved": moved, "unchanged": unchanged, "case_id": target, "at_utc": now}

    def case_assignment_history(self, db):
        history = {}
        if self.has_table(db, "case_assignments"):
            for row in db.execute("SELECT * FROM case_assignments ORDER BY id"):
                history.setdefault(row["sample_id"], []).append({
                    "assignment_id": row["id"], "case_id": row["case_id"], "reason": row["reason"],
                    "set_by": row["editor"] or None, "set_at_utc": row["created"]})
        return history

    def export(self):
        sessions = {item["id"]: item for item in self.sessions()}
        records = []
        for sample in self.samples():
            session = sessions[sample["session_id"]]
            if not exportable(session["purpose"], sample["label"], sample["metadata"].get("simulated")):
                continue
            records.append({
                "schema": "bll.vision_dataset_row.v1", "capture_id": sample["id"],
                "session_id": session["id"], "box_id": sample["case_id"],
                "larva_id": session["larva_id"] or None,
                "group_id": sample["case_id"], "split": "unassigned",
                "image": sample["image_path"], "sha256": sample["metadata"]["sha256"],
                "label": sample["label"], "annotation_source": "human_image_review",
                "reviewer": sample["reviewer"], "reviewed_at": sample["reviewed_at"],
                "note": sample["note"], "capture": sample["metadata"], "session": session,
                **({"recorded_box_id": sample["recorded_case_id"]} if sample["case_corrected"] else {}),
            })
        return records

    def classification_snapshot(self):
        """Audit export, NOT a training export. Includes excluded/unreviewed records.

        One SQLite read transaction fixes the sessions, samples and annotation history
        to the same snapshot. No migration or mutation of existing captures/labels.
        """
        with self.connect() as db:
            db.execute("BEGIN")
            deleted = self._deleted(db)
            sessions = {row["id"]: json.loads(row["details"]) for row in
                        db.execute("SELECT id, details FROM sessions") if row["id"] not in deleted}
            samples = [row for row in db.execute("SELECT * FROM samples ORDER BY id")
                       if row["session_id"] not in deleted]
            case_history = self._case_history(db)
            moves = self.case_assignment_history(db)
            roles = {}
            if self.has_roles(db):
                for row in db.execute("SELECT * FROM dataset_roles ORDER BY id"):
                    roles.setdefault(row["sample_id"], []).append({
                        "role_id": row["id"], "role": DATASET_ROLES[row["role"]],
                        "set_by": row["reviewer"] or None, "set_at_utc": row["created"]})
            history = {}
            for row in db.execute("SELECT * FROM annotations ORDER BY id"):
                history.setdefault(row["sample_id"], []).append({
                    "annotation_id": row["id"], "label": VISIBILITY_LABELS[row["label"]],
                    "original_label": row["label"], "labeler": row["reviewer"],
                    "reviewed_at_utc": row["created"], "note": row["note"],
                    "label_version": LABEL_VERSION,
                })
        records = []
        for sample in samples:
            session = sessions[sample["session_id"]]
            capture = json.loads(sample["metadata"])
            annotations = history.get(sample["id"], [])
            latest = annotations[-1] if annotations else {}
            # The current case: the latest correction, else the case recorded with the session.
            case_moves = moves.get(sample["id"], [])
            case_id = case_moves[-1]["case_id"] if case_moves else session["box_id"]
            case_profile = case_history.get(case_id, [])
            # ARM sessions (observation-loop runs) keep the pose per frame.
            pose = (capture.get("camera_pose") if session.get("camera_pose") == "ARM"
                    else session.get("camera_pose"))
            real_camera = (capture.get("simulated") is False and
                           capture.get("source") == "grove_vision_ai_v2_via_xiao_i2c_usb")
            records.append({
                "schema": "bll.vision_image_label.v1", "image_id": sample["id"],
                "image": sample["image_path"], "sha256": capture.get("sha256"),
                "capture_session_id": sample["session_id"], "case_id": case_id,
                "larva_id": session.get("larva_id") or None,
                "device_id": session.get("device_id") or None,
                "camera_pose": pose,
                "illumination_profile": session.get("illumination_profile") or None,
                "acquisition_protocol_id": session.get("acquisition_protocol_id") or None,
                "continuity_group": session.get("continuity_group") or None,
                "captured_at_utc": None, "timestamp_quality": "EXPOSURE_UNSYNCED",
                "pc_capture_bracket": {"request": capture.get("request_sent_utc_pc"),
                                       "received": capture.get("received_utc_pc")},
                "source_kind": "REAL_CAMERA" if real_camera else "UNVERIFIED",
                "purpose": session["purpose"],
                "label": latest.get("label", "UNREVIEWED"),
                "labeler": latest.get("labeler") or None, "label_version": LABEL_VERSION,
                "annotation_history": annotations,
                "dataset_role": (roles.get(sample["id"]) or [{"role": "UNASSIGNED"}])[-1]["role"],
                "dataset_role_history": roles.get(sample["id"], []),
                "case_role": case_profile[-1]["role"] if case_profile else "UNASSIGNED",
                "case_role_history": case_profile,
                # Only present when corrected, so unchanged rows keep their frozen digests.
                **({"case_assignment_history": case_moves} if case_moves else {}),
                "capture": capture, "session": session,
            })
        return records
