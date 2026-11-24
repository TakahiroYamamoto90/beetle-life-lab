#include "observation_runtime.h"
#ifdef BLL_EVIDENCE_STORE
#include <Arduino.h>
#include <cstring>

#include "config.h"
#include "evidence_store.h"
#include "observation_core.h"
#include "observation_scheduler.h"
#include "piezo_adc.h"
#include "servo_driver.h"
#include "vision_sscma.h"

namespace {
// No light sensor exists. Only an operator declaration (RAM, reset at boot) can
// mark the scene dark; otherwise the policy records LIGHT_UNKNOWN.
bll::Light operatorLight = bll::Light::Unknown;
// "observe-mode follow|explore|hold" (0.18.1, hold 0.18.2; RAM only, follow after boot).
// follow = 0.18.0 rule (look again where the image AI last saw the larva); explore = the views
// not seen recently, as before; hold = follow, and the arm waits at that view instead of parking.
enum class ObservationMode : uint8_t { Explore, Follow, Hold };
ObservationMode observationMode = ObservationMode::Follow;
const char* observationModeName() {
  return observationMode == ObservationMode::Explore ? "explore" :
         observationMode == ObservationMode::Hold ? "hold" : "follow";
}
bll::PolicyConfig policyConfig() {
  bll::PolicyConfig c;
  c.stayWhereLarvaSeen = observationMode != ObservationMode::Explore;
  c.holdWhereLarvaSeen = observationMode == ObservationMode::Hold;
  return c;
}
bool lightDeclared = false;
unsigned decisionSeq = 0;
// 0.14.0: observation captures run the deployed Vision model on the same frame (INVOKE)
// so the PC can show its raw output for reference. RAM only; "observe-vision sample"
// returns to camera-only frames (e.g. no model loaded). The classes never enter the
// Evidence log or the Policy; presence stays UNKNOWN.
VisionFrameSource frameSource = VisionFrameSource::Invoke;

// Static scratch: the executor below also runs the Vision capture, whose own
// frame parser uses ~2.5 KiB of the loop stack.
bll::TemporalStateView view;
bll::ActionPlan plan;
VisionCaptureResult capture;
char triggerId[bll::kIdLen], decisionId[bll::kActionIdLen], actionId[bll::kActionIdLen];
char decisionEvent[bll::kIdLen], resultEvent[bll::kIdLen];

// Set when a scheduled capture moved the arm; cleared by a park or any operator jog.
bool parkWanted = false;
// PR3c: the Vision camera rides the servo arm. FRONT (0 deg) is home and the former
// fixed-camera view; TOP 90 deg, BACK 180 deg. FIXED is no longer offered.
bll::SchedulerConfig schedulerConfig() {
  bll::SchedulerConfig cfg;
  cfg.viewEnabled[static_cast<unsigned>(bll::Pose::Fixed)] = false;
  for (unsigned i = 1; i < bll::kPoseCount; ++i) cfg.viewEnabled[i] = true;  // gated by calibration below
  // FRONT<->BACK (1,325 us) ramps in 2.7 s. Before 0.14.1 the ramp only advanced while
  // loop() ran, so candidates stored during a park (~4.6 s each) stalled it and the park
  // timed out with the limp arm dropped (2026-11-10 at 4 s, 2026-11-11 at 10 s). Since
  // 0.14.1 the ramp runs on an esp_timer; completion is still detected in the loop, and
  // the "done" check comes before this timeout. No position sensor exists, so this
  // timeout only catches a ramp that never finishes, not a stuck servo.
  cfg.moveTimeoutMs = 10000;
  return cfg;
}
bll::DeviceScheduler scheduler(schedulerConfig());
// Calibrated pulse per pose (index = Pose); 0 = not calibrated, never offered.
uint16_t poseUs[bll::kPoseCount] = {0, config::kServoPoseUs[0], config::kServoPoseUs[1], config::kServoPoseUs[2]};
bool calibrationFromRam = false;
// A follow-up held by the camera cooldown is not persisted; re-run the policy later.
bool recheckArmed = false;
uint32_t recheckAtMs = 0;

bool calibrated(bll::Pose p) { return poseUs[static_cast<unsigned>(p) % bll::kPoseCount] != 0; }

bool selfActuationRecent() {
  return servoMoving() || scheduler.selfActuating() ||
         (servoEverMoved() && millis() - servoLastMotionMs() < config::kSelfActuationGuardMs);
}

const char* phaseName(bll::SchedPhase phase) {
  switch (phase) {
    case bll::SchedPhase::Moving: return "MOVING";
    case bll::SchedPhase::Settling: return "SETTLING";
    case bll::SchedPhase::Capturing: return "CAPTURING";
    case bll::SchedPhase::Recording: return "RECORDING";
    default: return "IDLE";
  }
}

const char* lightName(bll::Light light) {
  return light == bll::Light::Ok ? "OK" : light == bll::Light::Low ? "LOW" : "UNKNOWN";
}

bll::PolicyCapabilities capabilities() {
  bll::PolicyCapabilities caps;
  // Only the fixed Vision AI V2 camera exists; the three-view servo is not built,
  // so FRONT/TOP/BACK are never offered to the policy.
  // Offered only when physically present AND calibrated AND no servo fault is latched.
  for (unsigned i = 0; i < bll::kPoseCount; ++i) {
    const bll::Pose p = static_cast<bll::Pose>(i);
    caps.pose[i] = visionBusReady() && scheduler.poseUsable(p) && (p == bll::Pose::Fixed || calibrated(p));
  }
  return caps;
}

bll::PolicyConstraints constraints() {
  bll::PolicyConstraints k;
  k.liveMode = true;  // this log only admits LIVE events
  k.storageWritable = evidenceStorageReady();
  k.safetyOk = true;  // servo faults are latched in the scheduler and remove the views
  k.deviceBusy = piezoAdcIsRunning() || scheduler.busy() || servoMoving();
  k.selfActuationRecent = selfActuationRecent();
  k.light = operatorLight;
  return k;
}

bll::ClockContext clockNow() {
  bll::ClockContext c;
  bll::copyText(c.bootId, sizeof(c.bootId), evidenceBootId());
  c.nowMs = evidenceMonotonicMs();
  return c;
}

void printReasons(uint32_t reasons) {
  Serial.print('[');
  bool first = true;
  for (unsigned bit = 0; bit < 32; ++bit) {
    if (!((reasons >> bit) & 1u)) continue;
    const char* name = bll::reasonCode(bit);
    Serial.printf("%s\"%s\"", first ? "" : ",", name ? name : "UNKNOWN_REASON");
    first = false;
  }
  Serial.print(']');
}

void printAge(const char* key, const bll::AgeInfo& age, bool comma = true) {
  if (!age.present) Serial.printf("\"%s\":null", key);
  else if (!age.comparable) Serial.printf("\"%s\":\"NOT_COMPARABLE\"", key);
  else Serial.printf("\"%s\":%llu", key, static_cast<unsigned long long>(age.ageMs));
  if (comma) Serial.print(',');
}

void armRecheck();

// followup=armed tells the AD3 bridge that this trigger's episode may continue with
// another view after the camera cooldown; it then waits for that step's DONE too.
void done(const char* status, uint32_t reasons) {
  armRecheck();
  Serial.printf("OBSERVATION_DONE trigger=%s status=%s action=%s followup=%s reasons=",
                triggerId[0] ? triggerId : "none", status, bll::actionName(plan.action),
                recheckArmed ? "armed" : "none");
  printReasons(reasons);
  Serial.println();
}

// Ramp the servo to the pose; report done (ASSUMED: no position sensor) or let the
// scheduler time out. Blocks the loop like the capture does (at most moveTimeoutMs).
bll::SchedulerStep runMove(const char* id, bll::Pose pose) {
  const uint16_t us = poseUs[static_cast<unsigned>(pose) % bll::kPoseCount];
  if (!us || !servoMoveTo(us)) return scheduler.onMove(id, bll::MoveStatus::Failed, evidenceMonotonicMs());
  Serial.printf("OBSERVATION_MOVE_BEGIN action_id=%s pose=%s target_us=%u\n", id, bll::poseName(pose), us);
  for (;;) {
    servoUpdate();
    const uint64_t now = evidenceMonotonicMs();
    if (!servoMoving()) return scheduler.onMove(id, bll::MoveStatus::DoneAssumed, now);
    const bll::SchedulerStep s = scheduler.poll(now);
    if (s.command != bll::Command::None || s.abortMotion) return s;
    delay(1);
  }
}

// Drive one committed intent to a result. The Vision capture is still the blocking
// visionCapture() (non-blocking SSCMA is a later slice).
void runScheduled(bll::SchedulerStep s) {
  for (unsigned guardSteps = 0; s.command != bll::Command::PersistResult; ++guardSteps) {
    const uint64_t now = evidenceMonotonicMs();
    if (s.command == bll::Command::Capture) {
      Serial.printf("OBSERVATION_CAPTURE_BEGIN action_id=%s pose=%s\n", actionId, bll::poseName(s.pose));
      visionCapture(capture, frameSource);
      s = scheduler.onCapture(actionId, capture.ok ? bll::CaptureStatus::Success : bll::CaptureStatus::Failed,
                              capture.reason, evidenceMonotonicMs());
    } else if (s.command == bll::Command::Move) {
      s = runMove(actionId, s.pose);
    } else {
      // Settling: every phase has a deadline, so this ends; the cap is a last resort.
      if (guardSteps > 100000) { s = scheduler.onMove(actionId, bll::MoveStatus::Failed, now); continue; }
      servoUpdate();
      delay(1);
      s = scheduler.poll(evidenceMonotonicMs());
    }
  }
  if (s.abortMotion) servoOff();  // move failed or timed out: stop driving (fail closed)
}

void armRecheck() {
  const bll::PolicyConfig config = policyConfig();
  view = bll::makeView(evidenceTemporalState(), clockNow());
  const uint64_t wait = bll::followUpRecheckInMs(view, config);
  recheckArmed = wait > 0;
  recheckAtMs = millis() + static_cast<uint32_t>(wait);
}

// Every step ends with exactly one OBSERVATION_DONE. A timed follow-up recheck is only
// run after a DONE that announced followup=armed, so the bridge is waiting for it.
void step() {
  const bll::TemporalState& state = evidenceTemporalState();
  const bll::PolicyCapabilities caps = capabilities();
  const bll::PolicyConstraints limits = constraints();
  const bll::PolicyConfig config = policyConfig();
  view = bll::makeView(state, clockNow());
  plan = bll::decide(state, view, caps, limits, config);
  bll::copyText(triggerId, sizeof(triggerId), plan.decide ? plan.triggerId : state.lastPassive.eventId);
  if (!plan.decide) { done("no_decision", plan.reasons); return; }

  ++decisionSeq;
  snprintf(decisionId, sizeof(decisionId), "dec-%s-%u", evidenceBootId(), decisionSeq);
  snprintf(actionId, sizeof(actionId), "obs-%s-%u", evidenceBootId(), decisionSeq);
  const ObservationDecisionRecord record{&plan, decisionId, actionId, &limits, &caps, &config,
                                         lightName(limits.light),
                                         lightDeclared ? "OPERATOR_DECLARED" : "NO_SENSOR"};
  // Intent first: an action whose decision is not durable is never executed.
  if (!evidenceAppendDecision(record, decisionEvent, sizeof(decisionEvent))) {
    done("persist_failed", plan.reasons);
    return;
  }
  Serial.printf("OBSERVATION_DECISION {\"decision_event_id\":\"%s\",\"decision_id\":\"%s\","
                "\"trigger_event_id\":\"%s\",\"action\":\"%s\",\"action_id\":",
                decisionEvent, decisionId, triggerId, bll::actionName(plan.action));
  if (bll::isCapture(plan.action)) Serial.printf("\"%s\",\"target_pose\":\"%s\"", actionId, bll::poseName(plan.pose));
  else Serial.print("null,\"target_pose\":null");
  Serial.printf(",\"policy_version\":\"%s\",\"reason_codes\":", bll::kPolicyVersion);
  printReasons(plan.reasons);
  Serial.println('}');
  if (!bll::isCapture(plan.action)) { done("decided", plan.reasons); return; }

  // SafetyGuard: re-check immediately before the physical action.
  const bll::TemporalState& committed = evidenceTemporalState();
  const char* guard = nullptr;
  if (!committed.intent.open || strcmp(committed.intent.actionId, actionId) != 0) guard = "intent_not_committed";
  else if (!evidenceStorageReady()) guard = "storage_not_ready";
  else if (piezoAdcIsRunning()) guard = "device_busy";
  else if (!capabilities().pose[static_cast<unsigned>(plan.pose)]) guard = "pose_unavailable";

  capture = VisionCaptureResult();
  bll::SchedulerStep first;
  if (!guard && !scheduler.start(actionId, plan.action, true, evidenceMonotonicMs(), first))
    guard = "scheduler_busy";
  if (!guard) runScheduled(first);
  if (!guard && scheduler.result().selfActuated) parkWanted = true;
  const bll::ScheduledResult& ran = scheduler.result();
  const bool captured = !guard && ran.capture == bll::CaptureOutcome::Success;
  // Same Evidence fields as 0.11.0: on success the reason stays the camera's ("none").
  const ObservationResultRecord outcome{
      actionId, decisionEvent,
      guard ? bll::ActionStatus::Failed : ran.status,
      plan.action, plan.pose,
      guard ? bll::CaptureOutcome::NotAttempted : ran.capture,
      guard ? guard : captured ? capture.reason : ran.reason, capture.durationMs,
      captured ? capture.payloadSha256 : "", captured ? capture.payloadBytes : 0,
      static_cast<int16_t>(captured ? capture.topClass : -1), static_cast<int16_t>(captured ? capture.topScore : -1),
      captured && capture.topClass == config::kVisionLarvaClassId && capture.topScore >= config::kVisionLarvaMinScore};
  const bool stored = evidenceAppendActionResult(outcome, resultEvent, sizeof(resultEvent));
  Serial.printf("OBSERVATION_RESULT {\"result_event_id\":%s%s%s,\"action_id\":\"%s\",\"status\":\"%s\","
                "\"capture\":\"%s\",\"reason\":\"%s\",\"payload_sha256\":%s%s%s,\"payload_bytes\":%lu,"
                "\"frame_source\":\"%s\",\"model_output\":%s,\"presence\":\"UNKNOWN\",\"stored\":%s}\n",
                stored ? "\"" : "", stored ? resultEvent : "null", stored ? "\"" : "",
                actionId, captured ? "COMPLETED" : "FAILED",
                guard ? "NOT_ATTEMPTED" : captured ? "SUCCESS" : "FAILED", outcome.captureReason,
                captured ? "\"" : "", captured ? capture.payloadSha256 : "null", captured ? "\"" : "",
                static_cast<unsigned long>(outcome.payloadBytes), visionFrameSourceName(frameSource),
                captured && capture.classesPresent ? "true" : "false", stored ? "true" : "false");
  if (!guard) scheduler.onPersisted(stored);
  // A result that could not be stored leaves the intent open: the policy then
  // waits (ACTION_IN_PROGRESS) and the next boot records it as INTERRUPTED.
  done(stored ? "decided" : "result_persist_failed", plan.reasons);
}

// Back to the home pose (FRONT) once an episode is over: only after the servo has moved
// this boot (never at boot by itself), never while a follow-up is armed or anything runs.
bool parkActive = false;
uint32_t parkStartMs = 0;
void pollPark() {
  if (parkActive) {
    servoUpdate();
    const uint64_t now = evidenceMonotonicMs();
    const uint16_t reachedUs = servoCurrentUs();  // servoOff() below clears it
    bll::SchedulerStep s = !servoMoving()
        ? scheduler.onMove(bll::DeviceScheduler::kParkActionId, bll::MoveStatus::DoneAssumed, now)
        : scheduler.poll(now);
    if (s.abortMotion) {
      servoOff();
      Serial.printf("SERVO_PARK status=failed reason=move_timeout elapsed_ms=%lu reached_us=%u\n",
                    static_cast<unsigned long>(millis() - parkStartMs), reachedUs);
    }
    if (!scheduler.parking()) {
      parkActive = false;
      if (!s.abortMotion)
        Serial.printf("SERVO_PARK status=done pose=FRONT us=%u elapsed_ms=%lu (assumed)\n", reachedUs,
                      static_cast<unsigned long>(millis() - parkStartMs));
    }
    return;
  }
  if (!parkWanted || recheckArmed || scheduler.busy() || piezoAdcIsRunning() || !servoAttached()) return;
  if (!calibrated(bll::Pose::Front)) return;
  // 0.18.2 hold: while the next trigger would look again at the view where the larva was just
  // seen (observation_core.h holdArmAt), the arm waits there. parkWanted stays set, so the arm
  // parks as soon as that no longer applies (2 min, streak limit, or lost there).
  {
    bll::Pose holdPose;
    const bll::PolicyConfig config = policyConfig();
    const bll::TemporalStateView v = bll::makeView(evidenceTemporalState(), clockNow());
    if (bll::holdArmAt(v, capabilities(), config, holdPose) && calibrated(holdPose) &&
        servoCurrentUs() == poseUs[static_cast<unsigned>(holdPose)]) return;
  }
  const uint16_t home = poseUs[static_cast<unsigned>(bll::Pose::Front)];
  if (servoCurrentUs() == home && scheduler.poseConfidence() == bll::PoseConfidence::Assumed) return;
  bll::SchedulerStep s;
  if (!scheduler.startPark(bll::Pose::Front, evidenceMonotonicMs(), s)) return;
  if (!servoMoveTo(home)) { scheduler.onMove(bll::DeviceScheduler::kParkActionId, bll::MoveStatus::Failed, 0); servoOff(); return; }
  parkActive = true;
  parkStartMs = millis();
  parkWanted = false;
  Serial.printf("SERVO_PARK status=begin pose=FRONT target_us=%u\n", home);
}

bool parsePoseArg(const char* text, bll::Pose& out) {
  if (!bll::parsePose(text, out) || out == bll::Pose::Fixed) return false;
  return true;
}

// The scheduler keeps its last pose value even after the pose became unknown (boot, jog,
// servo-off); show UNKNOWN then instead of a stale or default name such as FIXED.
const char* shownPose() {
  return scheduler.poseConfidence() == bll::PoseConfidence::Unknown ? "UNKNOWN"
                                                                     : bll::poseName(scheduler.currentPose());
}

void printServoState(const char* event) {
  Serial.printf("SERVO_STATE {\"event\":\"%s\",\"attached\":%s,\"current_us\":%u,\"target_us\":%u,"
                "\"moving\":%s,\"pose\":\"%s\",\"pose_confidence\":\"%s\",\"servo_fault\":%s,"
                "\"pose_us\":{\"FRONT\":%u,\"TOP\":%u,\"BACK\":%u},\"calibration\":\"%s\","
                "\"self_actuation_recent\":%s,\"parking\":%s}\n",
                event, servoAttached() ? "true" : "false", servoCurrentUs(), servoTargetUs(),
                servoMoving() ? "true" : "false", shownPose(),
                bll::poseConfidenceName(scheduler.poseConfidence()), scheduler.servoFaulted() ? "true" : "false",
                poseUs[1], poseUs[2], poseUs[3], calibrationFromRam ? "RAM_OVERRIDE" : "BUILD",
                selfActuationRecent() ? "true" : "false", parkActive ? "true" : "false");
}
}  // namespace

void observationRecoverAfterBoot() {
  const bll::TemporalState& state = evidenceTemporalState();
  if (!state.intent.open) return;
  // Copy: the reducer clears the intent while the result is being committed.
  bll::copyText(actionId, sizeof(actionId), state.intent.actionId);
  bll::copyText(decisionEvent, sizeof(decisionEvent), state.intent.decision.eventId);
  const bll::Action action = state.intent.action;
  // Never re-run the physical action; record that its outcome is unknown.
  const ObservationResultRecord outcome{actionId, decisionEvent, bll::ActionStatus::Interrupted,
                                        action, bll::poseFor(action), bll::CaptureOutcome::NotAttempted,
                                        "device_restarted_before_result", 0, "", 0, -1, -1, false};
  evidenceAppendActionResult(outcome, resultEvent, sizeof(resultEvent));
}

void observationAfterPassiveStored() { step(); }
void observationRun() { step(); }

void observationPoll() {
  if (!parkActive) servoUpdate();  // operator jog ramps
  pollPark();
  if (!recheckArmed || static_cast<int32_t>(millis() - recheckAtMs) < 0) return;
  if (piezoAdcIsRunning() || scheduler.busy() || servoMoving()) return;  // retried on a later loop
  recheckArmed = false;
  step();
}

void observationBegin() { servoBegin(); }

// Operator servo commands (calibration / bench). Refused while anything runs; a jog
// makes the arm pose UNKNOWN to the scheduler so the next capture moves first.
void observationServoCommand(const char* line) {
  char name[24] = {}, a[16] = {}, b[16] = {};
  const int n = sscanf(line, "%23s %15s %15s", name, a, b);
  if (strcmp(name, "servo-status") == 0) { printServoState("status"); return; }
  if (piezoAdcIsRunning() || scheduler.busy() || recheckArmed) {
    Serial.println("SERVO_ERROR reason=busy");
    return;
  }
  parkWanted = false;  // the operator owns the arm until the next scheduled capture
  if (strcmp(name, "servo-off") == 0) {
    servoOff(); scheduler.invalidatePose(); printServoState("off");
  } else if (strcmp(name, "servo-us") == 0 && n == 2) {
    const long us = strtol(a, nullptr, 10);
    if (!servoMoveTo(static_cast<uint16_t>(us < 0 ? 0 : us > 65535 ? 65535 : us))) {
      Serial.printf("SERVO_ERROR reason=out_of_range range_us=%u..%u\n", config::kServoHardMinUs, config::kServoHardMaxUs);
      return;
    }
    scheduler.invalidatePose(); printServoState("jog");
  } else if (strcmp(name, "servo-pose") == 0 && n == 2) {
    bll::Pose p;
    if (!parsePoseArg(a, p)) { Serial.println("SERVO_ERROR reason=pose_must_be_FRONT_TOP_or_BACK"); return; }
    if (!calibrated(p)) { Serial.println("SERVO_ERROR reason=pose_not_calibrated"); return; }
    servoMoveTo(poseUs[static_cast<unsigned>(p)]); scheduler.invalidatePose(); printServoState("jog_pose");
  } else if (strcmp(name, "servo-cal") == 0 && n == 3) {
    bll::Pose p;
    const long us = strtol(b, nullptr, 10);
    if (!parsePoseArg(a, p)) { Serial.println("SERVO_ERROR reason=pose_must_be_FRONT_TOP_or_BACK"); return; }
    if (us != 0 && (us < config::kServoHardMinUs || us > config::kServoHardMaxUs)) {
      Serial.println("SERVO_ERROR reason=out_of_range (0 disables the view)"); return;
    }
    poseUs[static_cast<unsigned>(p)] = static_cast<uint16_t>(us);
    calibrationFromRam = true;
    scheduler.invalidatePose();
    printServoState("calibration_ram");
  } else {
    Serial.println("SERVO_ERROR reason=unknown_command (servo-status | servo-us <us> | servo-pose <FRONT|TOP|BACK> | "
                   "servo-cal <FRONT|TOP|BACK> <us|0> | servo-off)");
  }
}

void observationSetLight(const char* value) {
  if (strcmp(value, "ok") == 0) { operatorLight = bll::Light::Ok; lightDeclared = true; }
  else if (strcmp(value, "low") == 0) { operatorLight = bll::Light::Low; lightDeclared = true; }
  else if (strcmp(value, "unknown") == 0) { operatorLight = bll::Light::Unknown; lightDeclared = false; }
  else { Serial.println("OBSERVATION_ERROR reason=light_must_be_ok_low_or_unknown"); return; }
  Serial.printf("OBSERVATION_LIGHT light=%s source=%s\n", lightName(operatorLight),
                lightDeclared ? "OPERATOR_DECLARED" : "NO_SENSOR");
}

void observationSetMode(const char* value) {
  if (strcmp(value, "follow") == 0) observationMode = ObservationMode::Follow;
  else if (strcmp(value, "explore") == 0) observationMode = ObservationMode::Explore;
  else if (strcmp(value, "hold") == 0) observationMode = ObservationMode::Hold;
  else { Serial.println("OBSERVATION_ERROR reason=mode_must_be_follow_explore_or_hold"); return; }
  Serial.printf("OBSERVATION_MODE mode=%s\n", observationModeName());
}

void observationSetVision(const char* value) {
  if (strcmp(value, "invoke") == 0) frameSource = VisionFrameSource::Invoke;
  else if (strcmp(value, "sample") == 0) frameSource = VisionFrameSource::Sample;
  else { Serial.println("OBSERVATION_ERROR reason=vision_must_be_invoke_or_sample"); return; }
  Serial.printf("OBSERVATION_VISION frame_source=%s\n", visionFrameSourceName(frameSource));
}

const char* observationBusyReason() {
  if (evidenceTemporalState().intent.open) return "intent_open";
  if (scheduler.busy()) return "scheduler_busy";
  if (servoMoving() || parkActive) return "servo_moving";
  if (recheckArmed) return "follow_up_armed";
  if (parkWanted && calibrated(bll::Pose::Front)) return "park_pending";
  return nullptr;
}

void observationStatus() {
  const bll::TemporalState& state = evidenceTemporalState();
  const bll::PolicyCapabilities caps = capabilities();
  const bll::PolicyConstraints limits = constraints();
  const bll::PolicyConfig config = policyConfig();
  view = bll::makeView(state, clockNow());
  const bll::ActionPlan preview = bll::decide(state, view, caps, limits, config);
  Serial.printf("OBSERVATION_STATE {\"policy_version\":\"%s\",\"reducer_version\":\"%s\","
                "\"reduced_through\":%lu,\"complete\":%s,\"pending_trigger\":%s,\"intent_open\":%s,"
                "\"decisions\":%lu,\"waits\":%lu,\"completed\":%lu,\"failed\":%lu,\"interrupted\":%lu,"
                "\"coalesced_triggers\":%lu,\"light\":\"%s\",\"fixed_camera\":%s,\"device_busy\":%s,"
                "\"storage_ready\":%s,\"mode\":\"%s\",\"ages_ms\":{",
                bll::kPolicyVersion, bll::kReducerVersion,
                static_cast<unsigned long>(state.reducedThrough), view.complete ? "true" : "false",
                view.pending ? "true" : "false", state.intent.open ? "true" : "false",
                static_cast<unsigned long>(state.decisions), static_cast<unsigned long>(state.waits),
                static_cast<unsigned long>(state.completed), static_cast<unsigned long>(state.failed),
                static_cast<unsigned long>(state.interrupted),
                static_cast<unsigned long>(state.coalescedTriggers), lightName(limits.light),
                caps.pose[0] ? "true" : "false", limits.deviceBusy ? "true" : "false",
                limits.storageWritable ? "true" : "false", observationModeName());
  printAge("last_passive_candidate", view.trigger);
  printAge("last_capture_attempt", view.lastAttempt);
  printAge("last_fixed_capture_success", view.successByPose[0]);
  printAge("last_visual_movement", view.visualMovement, false);
  Serial.printf("},\"activity\":\"UNKNOWN\",\"preview\":{\"decide\":%s,\"action\":\"%s\",\"reason_codes\":",
                preview.decide ? "true" : "false", bll::actionName(preview.action));
  printReasons(preview.reasons);
  Serial.printf("},\"scheduler\":{\"phase\":\"%s\",\"pose_confidence\":\"%s\",\"servo_fault\":%s,"
                "\"recheck_armed\":%s,\"pose\":\"%s\",\"views\":{\"FRONT\":%s,\"TOP\":%s,\"BACK\":%s},"
                "\"self_actuation_recent\":%s,\"parking\":%s},\"vision_frame_source\":\"%s\"}\n",
                phaseName(scheduler.phase()), bll::poseConfidenceName(scheduler.poseConfidence()),
                scheduler.servoFaulted() ? "true" : "false", recheckArmed ? "true" : "false",
                shownPose(), caps.pose[1] ? "true" : "false",
                caps.pose[2] ? "true" : "false", caps.pose[3] ? "true" : "false",
                limits.selfActuationRecent ? "true" : "false", parkActive ? "true" : "false",
                visionFrameSourceName(frameSource));
}
#endif
