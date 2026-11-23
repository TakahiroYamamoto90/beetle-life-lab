"""Deployed Vision V2 larva-visibility model: identity check and raw output reading.

Shared by capture_vision.py (collection tool) and the AD3 bridge. Pure functions, no
serial I/O. Model output is reference only: it is never a larva detection, never an
Evidence or Policy input, and its real-device accuracy is not established.
"""
import base64
import binascii
import json

MODEL_NAME = "larva_visibility_int8_vela"
MODEL_CLASSES = ("NO_VISIBLE_LARVA", "VISIBLE_LARVA")
# INFO version -> deployed Vela artifact SHA256 (bench candidates, not evaluated models).
KNOWN_MODEL_ARTIFACTS = {
    "1.0.0": "ab1b20590d92f7d617861c342a19830b0cc3cf8b9bb42d06d3094da4070ebd7e",  # recipe v7
    "1.1.0": "0358b4fd124af8fb7996e7eca8d0def43f66ede2019a8a3d82b1739efff2066a",  # recipe v9 (RGB)
    "1.2.0": "2e1980fa8f1e3cd5fa36b0f82bbe82ef086c55b6c2ecc7e6f296f28aab2cbd75",  # recipe v11, input 96
    "1.3.0": "77b6d047bea5c50db1c504e7e7e0b90d615dd0171b0544e63755711a9f735573",  # recipe v11, input 192
    "1.4.0": "12bebc7af76a56fb38b81ef0bb6e5c0850b8eca527ae3401e15e71ede6c3c0c3",  # recipe v12, input 192
    "1.5.0": "ae9a10edd01f8b4fcdf56780b1725cfaf07a07c6bf81705763f588b878fd448b",  # recipe v13b, input 96
}
# Human-readable recipe of each INFO version (same source as the comments above).
MODEL_RECIPES = {"1.0.0": "v7", "1.1.0": "v9", "1.2.0": "v11-96", "1.3.0": "v11-192",
                 "1.4.0": "v12-192", "1.5.0": "v13b-96"}
# Up to v12 every training image came from the fixed camera (camera_pose FIXED).
# FRONT is the arm's home pose at about the former fixed view, but not the same mount.
TRAINING_POSES = ("FIXED",)
# v13b (2026-11-19) also trained on the arm views of the observation loop (box-03).
MODEL_TRAINING_POSES = {"1.5.0": ("FIXED", "FRONT", "TOP", "BACK")}


def view_vs_training(pose, version=None):
    """How far a capture pose is from the training images of the model version (display caveat only).
    An unknown or unverified model gets the conservative fixed-camera-only answer."""
    if pose in MODEL_TRAINING_POSES.get(version, TRAINING_POSES):
        return "IN_TRAINING_VIEW"
    if pose == "FRONT":
        return "NEAR_TRAINING_VIEW"  # about the old fixed view, different mount: unverified
    return "OUTSIDE_TRAINING_VIEWS"


def decode_info_reply(info):
    """INFO? reply -> decoded model metadata dict. Raises ValueError when unusable."""
    if not isinstance(info, dict) or info.get("code") != 0 or info.get("type") != 0:
        raise ValueError("Vision V2 INFO query failed")
    encoded = (info.get("data") or {}).get("info")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("Vision V2 model metadata is unavailable")
    try:
        decoded = json.loads(base64.b64decode(encoded, validate=True))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Vision V2 model metadata is not valid base64 JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("Vision V2 model metadata is not an object")
    return decoded


def verify_model(decoded):
    """Return decoded metadata plus artifact_sha256 if it is our known model, else raise."""
    if (decoded.get("name") != MODEL_NAME or decoded.get("classes") != list(MODEL_CLASSES)
            or decoded.get("version") not in KNOWN_MODEL_ARTIFACTS):
        raise ValueError("The expected larva visibility model/classes are not loaded")
    # INFO is metadata written at deployment; the artifact hash is our deployment record.
    return dict(decoded, artifact_sha256=KNOWN_MODEL_ARTIFACTS[decoded["version"]],
                recipe=MODEL_RECIPES.get(decoded["version"]))


def model_from_inspect_lines(lines):
    """'vision' command output lines -> dict(status=VERIFIED, model=...) or UNVERIFIED."""
    replies = {}
    for line in lines:
        if line.startswith("VISION_REPLY "):
            try:
                reply = json.loads(line[len("VISION_REPLY "):])
            except ValueError:
                continue
            if isinstance(reply, dict):
                replies[reply.get("name")] = reply
        elif line.startswith("VISION_UNAVAILABLE"):
            return dict(status="UNVERIFIED", reason="vision_unavailable", model=None)
    try:
        return dict(status="VERIFIED", reason=None, model=verify_model(decode_info_reply(replies.get("INFO?"))))
    except ValueError as exc:
        return dict(status="UNVERIFIED", reason=str(exc), model=None)


def reference_output(model_check, raw, pose):
    """Reference record of one same-frame model output. Never a presence claim.

    model_check: result of model_from_inspect_lines() at link open (or None).
    raw: dict(classes=..., perf=...) from the INVOKE event that carried the saved image.
    """
    out = dict(schema="bll.vision_model_reference.v1", reference_only=True,
               frame_source="INVOKE", same_frame_as_image=True, pose=pose,
               view_vs_training=view_vs_training(pose),
               raw_classes_score_target=raw.get("classes"), perf_ms=raw.get("perf"),
               larva_presence="UNKNOWN", device_accuracy="NOT_ESTABLISHED")
    model = (model_check or {}).get("model") if (model_check or {}).get("status") == "VERIFIED" else None
    if model is None:
        # Class ids cannot be named without a verified model: show nothing interpretive.
        out.update(model=None, prediction=dict(status="MODEL_NOT_VERIFIED",
                                                reason=(model_check or {}).get("reason") or "model_not_checked"))
        return out
    out.update(view_vs_training=view_vs_training(pose, model["version"]),
               model=dict(name=model["name"], version=model["version"], recipe=model.get("recipe"),
                          artifact_sha256=model["artifact_sha256"], classes=model["classes"]),
               prediction=interpret_classes(raw.get("classes"), model["classes"]))
    return out


def interpret_classes(raw_classes, class_names):
    """Raw SSCMA classes [[score 0-100, target_id], ...] -> top class or UNKNOWN."""
    rows = raw_classes if isinstance(raw_classes, list) else []
    candidates = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        score, class_id = row[0], row[1]
        if (isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 100 or
                isinstance(class_id, bool) or not isinstance(class_id, int) or
                not 0 <= class_id < len(class_names)):
            continue
        candidates.append({"class_id": class_id, "class_name": class_names[class_id],
                           "score": score})
    if not candidates:
        return {"status": "UNKNOWN", "reason": "no_valid_class_output", "candidates": []}
    highest = max(item["score"] for item in candidates)
    top = [item for item in candidates if item["score"] == highest]
    if len({item["class_id"] for item in top}) != 1:
        return {"status": "UNKNOWN", "reason": "tied_class_scores", "candidates": candidates}
    return {"status": "MODEL_OUTPUT_UNVERIFIED", "reason": None,
            "top": top[0], "candidates": candidates}
