#pragma once
// DeviceScheduler (PR3a): a non-blocking executor state machine for ONE capture
// intent that the Evidence log has already committed.
//
//   IDLE -> [MOVING] -> SETTLING -> CAPTURING -> RECORDING -> IDLE
//
// Pure: no Arduino, I/O, clock reads or heap. The caller (firmware runtime, or the
// host tests with a mock servo/camera) performs each returned command and feeds the
// outcome back with onMove()/onCapture()/poll()/onPersisted().
// It never decides WHAT to observe; observation_core.h does. It only sequences an
// intent and reports its result. Illumination (LED) is PR4 and is not modelled here.
// PR3c: a "park" move (no intent, no capture) returns the arm to its home pose after an
// episode; it never writes Evidence and fails closed like any other move.
#include <cstdint>

#include "observation_core.h"

namespace bll {

enum class SchedPhase : uint8_t { Idle, Moving, Settling, Capturing, Recording };
// No position sensor exists, so a completed servo command is only ASSUMED, never verified.
// UNKNOWN after boot or after any move failure; FIXED_MOUNT is the camera that never moves.
enum class PoseConfidence : uint8_t { Unknown, Assumed, FixedMount };
enum class MoveStatus : uint8_t { InProgress, DoneAssumed, Failed };
enum class CaptureStatus : uint8_t { Pending, Success, Failed };
enum class Command : uint8_t { None, Move, Capture, PersistResult };

inline const char* poseConfidenceName(PoseConfidence c) {
  switch (c) {
    case PoseConfidence::Assumed: return "POSE_ASSUMED";
    case PoseConfidence::FixedMount: return "FIXED_MOUNT";
    default: return "POSE_UNKNOWN";
  }
}

struct SchedulerConfig {
  // A view is usable only when physically present AND calibrated. Defaults keep the
  // current hardware: the fixed camera only.
  bool viewEnabled[kPoseCount] = {true, false, false, false};
  uint32_t moveTimeoutMs = 4000;
  uint32_t settleMs[kPoseCount] = {0, 800, 800, 800};
  uint32_t captureTimeoutMs = 30000;  // above the ~25 s worst case of the current SSCMA capture
};

struct SchedulerStep {
  Command command = Command::None;
  Pose pose = Pose::Fixed;      // for Move / Capture
  bool abortMotion = false;     // stop the servo before persisting (move failure/timeout)
};

struct ScheduledResult {
  char actionId[kActionIdLen] = {};
  Action action = Action::None;
  Pose pose = Pose::Fixed;
  ActionStatus status = ActionStatus::Failed;
  CaptureOutcome capture = CaptureOutcome::NotAttempted;
  char reason[40] = {};
  PoseConfidence poseConfidence = PoseConfidence::Unknown;
  uint64_t startedMs = 0, finishedMs = 0;
  // Self-actuation window (servo motion + settling). Vibration recorded inside it may be
  // mechanical noise and must never be read as larva activity. Empty when nothing moved.
  bool selfActuated = false;
  uint64_t actuationStartMs = 0, actuationEndMs = 0;
};

class DeviceScheduler {
 public:
  explicit DeviceScheduler(const SchedulerConfig& cfg = SchedulerConfig()) : cfg_(cfg) {}

  SchedPhase phase() const { return phase_; }
  bool busy() const { return phase_ != SchedPhase::Idle; }
  bool servoFaulted() const { return servoFault_; }
  bool lastPersistFailed() const { return lastPersistFailed_; }
  uint32_t ignoredStale() const { return ignoredStale_; }
  Pose currentPose() const { return pose_; }
  PoseConfidence poseConfidence() const { return confidence_; }
  bool selfActuating() const {
    return phase_ == SchedPhase::Moving || (phase_ == SchedPhase::Settling && result_.selfActuated);
  }
  // Fail closed: after a servo failure no moving view is offered until reboot/repair.
  bool poseUsable(Pose p) const {
    const unsigned i = static_cast<unsigned>(p);
    if (i >= kPoseCount || !cfg_.viewEnabled[i]) return false;
    return p == Pose::Fixed || !servoFault_;
  }
  const ScheduledResult& result() const { return result_; }
  bool parking() const { return parking_; }

  // The servo was moved outside the scheduler (operator jog / calibration): its pose is
  // no longer known, so the next capture moves first.
  void invalidatePose() {
    if (busy()) return;
    confidence_ = PoseConfidence::Unknown;
  }

  // Return the arm to `home` while idle. Only for a usable moving view not already
  // assumed there. Answer the returned Move with onMove(kParkActionId, ...).
  static constexpr const char* kParkActionId = "park";
  bool startPark(Pose home, uint64_t nowMs, SchedulerStep& out) {
    out = SchedulerStep();
    if (busy() || home == Pose::Fixed || !poseUsable(home)) return false;
    if (pose_ == home && confidence_ == PoseConfidence::Assumed) return false;
    phase_ = SchedPhase::Moving;
    parking_ = true;
    pose_ = home;
    confidence_ = PoseConfidence::Unknown;
    deadline_ = nowMs + cfg_.moveTimeoutMs;
    out.command = Command::Move;
    out.pose = home;
    return true;
  }

  // Accept an intent only if it is committed and nothing else is running (no parallel
  // drive or capture). Returns false with no command otherwise.
  bool start(const char* actionId, Action action, bool intentCommitted, uint64_t nowMs,
             SchedulerStep& out) {
    out = SchedulerStep();
    if (busy() || !intentCommitted || !isCapture(action) || !actionId || !actionId[0]) return false;
    result_ = ScheduledResult();
    copyText(result_.actionId, sizeof(result_.actionId), actionId);
    result_.action = action;
    result_.pose = poseFor(action);
    result_.startedMs = nowMs;
    const Pose target = result_.pose;
    if (!poseUsable(target)) {
      out = finish(ActionStatus::Failed, CaptureOutcome::NotAttempted,
                   servoFault_ && target != Pose::Fixed ? "servo_fault_latched" : "pose_unavailable", nowMs);
      return true;
    }
    if (target == Pose::Fixed) {
      confidence_ = PoseConfidence::FixedMount;
      pose_ = Pose::Fixed;
      out = enterSettling(nowMs);
      return true;
    }
    if (pose_ == target && confidence_ == PoseConfidence::Assumed) {
      out = enterSettling(nowMs);
      return true;
    }
    phase_ = SchedPhase::Moving;
    pose_ = target;  // requested; confidence stays UNKNOWN until the move reports done
    confidence_ = PoseConfidence::Unknown;
    deadline_ = nowMs + cfg_.moveTimeoutMs;
    result_.selfActuated = true;
    result_.actuationStartMs = nowMs;
    out.command = Command::Move;
    out.pose = target;
    return true;
  }

  SchedulerStep onMove(const char* actionId, MoveStatus status, uint64_t nowMs) {
    if (parking_) return onParkMove(actionId, status, nowMs);
    if (phase_ != SchedPhase::Moving || !matches(actionId)) { ++ignoredStale_; return SchedulerStep(); }
    if (status == MoveStatus::InProgress) return poll(nowMs);
    if (status == MoveStatus::Failed) return moveFailed("move_failed", nowMs);
    confidence_ = PoseConfidence::Assumed;
    return enterSettling(nowMs);
  }

  SchedulerStep onCapture(const char* actionId, CaptureStatus status, const char* reason, uint64_t nowMs) {
    if (phase_ != SchedPhase::Capturing || !matches(actionId)) { ++ignoredStale_; return SchedulerStep(); }
    if (status == CaptureStatus::Pending) return poll(nowMs);
    if (status == CaptureStatus::Success)
      return finish(ActionStatus::Completed, CaptureOutcome::Success, "captured", nowMs);
    return finish(ActionStatus::Failed, CaptureOutcome::Failed, reason && reason[0] ? reason : "capture_failed", nowMs);
  }

  // Time-driven transitions: settle completion and timeouts. Never re-issues a command.
  SchedulerStep poll(uint64_t nowMs) {
    switch (phase_) {
      case SchedPhase::Moving:
        if (nowMs >= deadline_) {
          if (parking_) return parkFailed();
          return moveFailed("move_timeout", nowMs);
        }
        break;
      case SchedPhase::Settling:
        if (nowMs >= deadline_) {
          if (result_.selfActuated) result_.actuationEndMs = nowMs;
          phase_ = SchedPhase::Capturing;
          deadline_ = nowMs + cfg_.captureTimeoutMs;
          SchedulerStep s;
          s.command = Command::Capture;
          s.pose = result_.pose;
          return s;
        }
        break;
      case SchedPhase::Capturing:
        // The camera cannot be aborted; a late reply carries this actionId and is ignored.
        if (nowMs >= deadline_)
          return finish(ActionStatus::Failed, CaptureOutcome::Failed, "capture_timeout", nowMs);
        break;
      default:
        break;
    }
    return SchedulerStep();
  }

  // The runtime appended (or failed to append) the result. A result that was not
  // stored leaves the intent open in the log, so the policy keeps waiting.
  void onPersisted(bool stored) {
    if (phase_ != SchedPhase::Recording) return;
    lastPersistFailed_ = !stored;
    phase_ = SchedPhase::Idle;
  }

 private:
  SchedulerStep onParkMove(const char* actionId, MoveStatus status, uint64_t nowMs) {
    if (phase_ != SchedPhase::Moving || !actionId || strcmp(actionId, kParkActionId) != 0) {
      ++ignoredStale_; return SchedulerStep();
    }
    if (status == MoveStatus::InProgress) return poll(nowMs);
    if (status == MoveStatus::Failed) return parkFailed();
    confidence_ = PoseConfidence::Assumed;
    parking_ = false;
    phase_ = SchedPhase::Idle;
    return SchedulerStep();
  }
  SchedulerStep parkFailed() {
    servoFault_ = true;
    confidence_ = PoseConfidence::Unknown;
    parking_ = false;
    phase_ = SchedPhase::Idle;
    SchedulerStep s;
    s.abortMotion = true;  // nothing to persist: a park has no intent
    return s;
  }
  bool matches(const char* actionId) const {
    return actionId && strcmp(actionId, result_.actionId) == 0;
  }
  SchedulerStep enterSettling(uint64_t nowMs) {
    phase_ = SchedPhase::Settling;
    // Also settles when the servo is assumed to be there already (FIXED defaults to 0 ms).
    deadline_ = nowMs + cfg_.settleMs[static_cast<unsigned>(result_.pose) % kPoseCount];
    return poll(nowMs);
  }
  SchedulerStep moveFailed(const char* reason, uint64_t nowMs) {
    servoFault_ = true;
    confidence_ = PoseConfidence::Unknown;
    result_.actuationEndMs = nowMs;
    SchedulerStep s = finish(ActionStatus::Failed, CaptureOutcome::NotAttempted, reason, nowMs);
    s.abortMotion = true;
    return s;
  }
  SchedulerStep finish(ActionStatus status, CaptureOutcome capture, const char* reason, uint64_t nowMs) {
    phase_ = SchedPhase::Recording;
    result_.status = status;
    result_.capture = capture;
    copyText(result_.reason, sizeof(result_.reason), reason);
    result_.poseConfidence = confidence_;
    result_.finishedMs = nowMs;
    if (result_.selfActuated && !result_.actuationEndMs) result_.actuationEndMs = nowMs;
    SchedulerStep s;
    s.command = Command::PersistResult;
    s.pose = result_.pose;
    return s;
  }

  SchedulerConfig cfg_;
  SchedPhase phase_ = SchedPhase::Idle;
  Pose pose_ = Pose::Fixed;
  PoseConfidence confidence_ = PoseConfidence::Unknown;  // nothing is known after boot
  bool servoFault_ = false;
  bool parking_ = false;
  bool lastPersistFailed_ = false;
  uint32_t ignoredStale_ = 0;
  uint64_t deadline_ = 0;
  ScheduledResult result_;
};

}  // namespace bll
