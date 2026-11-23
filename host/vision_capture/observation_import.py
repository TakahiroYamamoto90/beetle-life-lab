"""Import AD3 observation-loop images into the collection database for human review.

Usage (from the repository root):
    python host/vision_capture/observation_import.py --run ad3-<run_id> --case-id box-02
    python host/vision_capture/observation_import.py --all --case-id box-02 --dry-run

    python host/vision_capture/observation_import.py --merge-sessions [--dry-run]

Only LIVE runs and frames whose bytes matched the XIAO payload hash are imported. Each
image is copied (the AD3 originals stay untouched) into one collection session per run
(camera_pose ARM); each frame keeps its own pose (FRONT/TOP/BACK) in its metadata. The
session has continuity_group "ad3-<run_id>", so frames of the same run are never split
between training and evaluation. Every sample starts unreviewed; the same-frame model
output is kept in metadata.model_inference and never sets the human label. Re-running
is safe: already imported action IDs are skipped.

--merge-sessions converts sessions made by the first version of this tool (one per run
and pose) into one ARM session per run. Sample IDs, labels and their history are kept;
the image files move to the new session directory.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from session_store import SessionStore, utc_now  # noqa: E402

SOURCE = "grove_vision_ai_v2_via_xiao_i2c_usb"
PROTOCOL = "AD3_OBSERVATION_LOOP_V1"
POSE_FROM_ACTION = {"CAPTURE_FIXED": "FIXED", "CAPTURE_FRONT": "FRONT",
                    "CAPTURE_TOP": "TOP", "CAPTURE_BACK": "BACK"}
RUN_DIR = re.compile(r"ad3-[0-9a-f]{32}")
ACTION_ID = re.compile(r"obs-[0-9a-f]{32}-[1-9][0-9]{0,9}")


def jpeg_size(data):
    """(width, height) from the first SOF marker; raises on a non-JPEG."""
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise ValueError("JPEG markers missing")
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2):
            return int.from_bytes(data[i + 7:i + 9], "big"), int.from_bytes(data[i + 5:i + 7], "big")
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    raise ValueError("JPEG size not found")


def observation_steps(run_dir):
    """Yield (candidate_seq, sent_at_pc, step_index, observation summary) from delivery.jsonl."""
    sent = {}
    path = run_dir / "delivery.jsonl"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        seq = record.get("candidate_seq")
        if record.get("status") == "sending":
            sent[seq] = record.get("received_at_pc")
        elif record.get("status") == "acknowledged":
            steps = [record.get("observation")] + list(record.get("follow_ups") or [])
            for index, step in enumerate(s for s in steps if s):
                yield seq, sent.get(seq), index, step


def plan_run(run_dir):
    """Importable frames of one AD3 run, with reasons for every skipped capture."""
    run_dir = Path(run_dir)
    if not RUN_DIR.fullmatch(run_dir.name):
        raise ValueError("not an AD3 run directory: " + run_dir.name)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("origin") != "LIVE":
        return manifest, [], [("*", "run origin is not LIVE")]
    frames, skipped = [], []
    for seq, sent_at, index, step in observation_steps(run_dir):
        action_id = step.get("action_id")
        result = step.get("result") or {}
        if result.get("capture") != "SUCCESS":
            continue  # waits and failed captures have no frame
        if not action_id or not ACTION_ID.fullmatch(action_id):
            skipped.append((action_id, "invalid action id"))
            continue
        if step.get("image_verified") is not True:
            skipped.append((action_id, "image not verified against the XIAO hash"))
            continue
        image = run_dir / "observations" / f"{action_id}.jpg"
        sidecar = json.loads(image.with_suffix(".json").read_text(encoding="utf-8"))
        data = image.read_bytes()
        if (not sidecar.get("hash_matches_xiao") or not sidecar.get("jpeg_markers_ok")
                or hashlib.sha256(data).hexdigest() != sidecar.get("image_sha256")):
            skipped.append((action_id, "image file does not match its sidecar"))
            continue
        pose = step.get("target_pose") or POSE_FROM_ACTION.get(step.get("action"))
        if pose not in POSE_FROM_ACTION.values():
            skipped.append((action_id, "unknown pose"))
            continue
        frames.append(dict(action_id=action_id, pose=pose, image=image, data=data, sidecar=sidecar,
                           candidate_seq=seq, candidate_sent_at_pc=sent_at, episode_step=index,
                           step=step))
    return manifest, frames, skipped


def find_session(store, run_id):
    for session in store.sessions():
        if (session.get("source_run_id") == run_id and session.get("acquisition") == "OBSERVATION_LOOP"
                and session.get("camera_pose") == "ARM"):
            return session
    return None


def session_notes(run_id, note):
    return (f"AD3観測ループ ad3-{run_id} から取込み（前面・上面・背面は画像ごとに記録）。"
            "撮影のきっかけは振動候補（人工的なタップを含みうる）。" + (" メモ: " + note if note else ""))


def merge_run_sessions(store, *, dry_run=False):
    """One-time conversion: per-pose observation sessions -> one ARM session per run."""
    groups = {}
    for session in store.sessions():
        if session.get("acquisition") == "OBSERVATION_LOOP" and session.get("camera_pose") in POSE_FROM_ACTION.values():
            groups.setdefault(session["source_run_id"], []).append(session)
    report = []
    for run_id, old in sorted(groups.items()):
        old.sort(key=lambda item: item["created"])
        old_ids = [item["id"] for item in old]
        samples = [sample for sample in store.samples() if sample["session_id"] in old_ids]
        report.append(dict(run_id=run_id, sessions=old_ids, samples=len(samples)))
        if dry_run:
            continue
        if find_session(store, run_id):
            raise ValueError(f"ad3-{run_id}: ARM session already exists; merge by hand")
        first = old[0]
        details = {key: value for key, value in first.items() if key != "title"}
        note = first.get("notes", "").split(" メモ: ", 1)
        details.update(id="session-" + uuid.uuid4().hex, camera_pose="ARM",
                       notes=session_notes(run_id, note[1] if len(note) > 1 else "")
                       + f" 旧セッション（視点別）{', '.join(old_ids)} を統合。")
        destination = store.root / "sessions" / details["id"]
        destination.mkdir(parents=True)
        moves = []
        for sample in samples:
            source = store.root / sample["image_path"]
            target = destination / source.name
            for suffix in (".jpg", ".json"):
                shutil.copyfile(source.with_suffix(suffix), target.with_suffix(suffix))
            moves.append((sample["id"], target.relative_to(store.root).as_posix(), source))
        with store.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                       (details["id"], details["created"], json.dumps(details, ensure_ascii=False)))
            for sample_id, relative, _ in moves:
                db.execute("UPDATE samples SET session_id=?, image_path=? WHERE id=?",
                           (details["id"], relative, sample_id))
            marks = ",".join("?" * len(old_ids))
            db.execute(f"DELETE FROM session_titles WHERE session_id IN ({marks})", old_ids)
            db.execute(f"DELETE FROM sessions WHERE id IN ({marks})", old_ids)
        for _, _, source in moves:
            for suffix in (".jpg", ".json"):
                source.with_suffix(suffix).unlink()
        for session_id in old_ids:
            folder = store.root / "sessions" / session_id
            if folder.is_dir() and not any(folder.iterdir()):
                try:
                    folder.rmdir()
                except OSError:  # e.g. held open by Explorer; an empty folder is harmless
                    report[-1].setdefault("left_empty_folders", []).append(session_id)
        report[-1]["new_session"] = details["id"]
    return report


def import_run(store, run_dir, *, case_id="", purpose="dataset", larva_id="",
               illumination_profile="", dry_run=False):
    run_dir = Path(run_dir)
    manifest, frames, skipped = plan_run(run_dir)
    run_id = manifest["run_id"]
    request = manifest.get("request") or {}
    case = case_id or request.get("case_id") or ""
    if frames and purpose == "dataset" and not case:
        raise ValueError(f"{run_dir.name}: ケースIDがありません（--case-id で指定してください）")
    device_id = (manifest.get("xiao_initial_state") or {}).get("device_id") or ""
    imported = []
    for frame in frames:
        # Also skips frames of a deleted session, so a re-import never brings them back.
        if store.has_sample(frame["action_id"]):
            skipped.append((frame["action_id"], "already imported"))
            continue
        if dry_run:
            imported.append(frame["action_id"])
            continue
        session = find_session(store, run_id)
        if session is None:
            session = store.create_session(dict(
                purpose=purpose, box_id=case, larva_id=larva_id, device_id=device_id,
                illumination_profile=illumination_profile, acquisition_protocol_id=PROTOCOL,
                continuity_group="ad3-" + run_id, camera_pose="ARM",
                source_run_id=run_id, acquisition="OBSERVATION_LOOP",
                notes=session_notes(run_id, request.get("note") or "")))
        destination = store.root / "sessions" / session["id"]
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / f"{frame['action_id']}.jpg"
        shutil.copyfile(frame["image"], target)
        width, height = jpeg_size(frame["data"])
        step, sidecar = frame["step"], frame["sidecar"]
        model = sidecar.get("vision_model_output")
        metadata = {
            "schema": "bll.observation_capture.v1", "capture_id": frame["action_id"],
            "source": SOURCE, "simulated": False, "acquisition": "OBSERVATION_LOOP",
            "camera_pose": frame["pose"],
            # The capture happened after the PC sent the candidate and before it received
            # the frame; exposure time itself is not synchronized.
            "request_sent_utc_pc": frame["candidate_sent_at_pc"],
            "received_utc_pc": sidecar.get("received_at_pc"),
            "image_file": target.name, "bytes": len(frame["data"]), "width": width, "height": height,
            "sha256": hashlib.sha256(frame["data"]).hexdigest(), "label": "unknown",
            "time_note": "PC bracket from candidate send to frame receipt; exposure time is not synchronized",
            "provenance": {
                "source_run_id": run_id, "original_image": frame["image"].relative_to(store.root).as_posix(),
                "action_id": frame["action_id"], "device_id": device_id,
                "trigger_event_id": step.get("trigger_event_id"),
                "decision_event_id": step.get("decision_event_id"),
                "result_event_id": (step.get("result") or {}).get("result_event_id"),
                "candidate_seq": frame["candidate_seq"], "episode_step": frame["episode_step"],
                "follow_up": bool(step.get("follow_up")), "reason_codes": step.get("reason_codes"),
                "xiao_payload_sha256": sidecar.get("xiao_payload_sha256"),
                "transport_sha256": sidecar.get("transport_sha256"),
                "trigger_note": "AD3 vibration candidate; may be an artificial tap, not larva activity",
                "imported_at_pc": utc_now(),
            },
        }
        if model:
            # Same shape as capture_vision's model_inference; the human label stays separate.
            metadata["model_inference"] = dict(model, interpretation_note=(
                "Reference model output on the same frame; does not set or replace the human image label."))
        target.with_suffix(".json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
        store.add_capture(session["id"], target)
        imported.append(frame["action_id"])
    return dict(run=run_dir.name, imported=imported, skipped=skipped)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run", action="append", help="ad3-<run_id> directory name (repeatable)")
    target.add_argument("--all", action="store_true", help="every AD3 run with observation images")
    target.add_argument("--merge-sessions", action="store_true",
                        help="convert per-pose observation sessions into one session per run")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "captures")
    parser.add_argument("--case-id", default="", help="case (box) ID when the AD3 run did not record one")
    parser.add_argument("--larva-id", default="")
    parser.add_argument("--illumination", default="", help="illumination profile for the new sessions")
    parser.add_argument("--purpose", choices=("dataset", "setup"), default="dataset")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    store = SessionStore(args.data_dir)
    if args.merge_sessions:
        for item in merge_run_sessions(store, dry_run=args.dry_run):
            print(f"ad3-{item['run_id']}: {len(item['sessions'])} sessions, {item['samples']} images"
                  + (f" -> {item['new_session']}" if "new_session" in item else " (dry run)"))
        return
    ad3 = store.root / "ad3"
    runs = ([ad3 / name for name in args.run] if args.run else
            sorted(p for p in ad3.iterdir() if RUN_DIR.fullmatch(p.name) and (p / "observations").is_dir()))
    total = 0
    for run in runs:
        report = import_run(store, run, case_id=args.case_id, purpose=args.purpose, larva_id=args.larva_id,
                            illumination_profile=args.illumination, dry_run=args.dry_run)
        total += len(report["imported"])
        print(f"{report['run']}: {'would import' if args.dry_run else 'imported'} {len(report['imported'])}")
        for action_id, reason in report["skipped"]:
            print(f"  skip {action_id}: {reason}")
    print(f"TOTAL {total}{' (dry run)' if args.dry_run else ''}")


if __name__ == "__main__":
    main()
