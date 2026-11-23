// Display-only orientation of arm-camera images (user report 2026-11-19).
// On the servo arm the camera is turned for FRONT and BACK, so those frames look sideways:
// FRONT is shown rotated 90 degrees left (counter-clockwise), BACK 90 degrees right (clockwise).
// Only the view changes. Stored files, hashes, training exports and the Vision V2 model input
// keep the sensor orientation the model was trained on.
export const POSE_ROTATION_DEG = Object.freeze({FRONT: -90, BACK: 90});

export function poseRotation(pose) { return POSE_ROTATION_DEG[pose] || 0; }

/** Rotate an <img> for its pose (the frames are square 240x240, so the layout box is unchanged). */
export function orient(img, pose) {
  if (!img) return img;
  const d = poseRotation(pose);
  img.style.transform = d ? `rotate(${d}deg)` : '';
  if (img.dataset) { if (pose) img.dataset.pose = pose; else delete img.dataset.pose; }
  return img;
}

/** The arm pose of an observation row (same fields as observation_view.js poseText). */
export function observationPose(o) {
  return o?.target_pose || o?.vision_ai?.pose || /^CAPTURE_(FIXED|FRONT|TOP|BACK)$/.exec(o?.action || '')?.[1] || null;
}

/** drawImage on a canvas with the pose rotation applied around the box centre. */
export function drawOriented(g, im, x, y, w, h, pose) {
  const d = poseRotation(pose);
  if (!d) { g.drawImage(im, x, y, w, h); return; }
  g.save(); g.translate(x + w / 2, y + h / 2); g.rotate(d * Math.PI / 180);
  g.drawImage(im, -h / 2, -w / 2, h, w); g.restore();
}
