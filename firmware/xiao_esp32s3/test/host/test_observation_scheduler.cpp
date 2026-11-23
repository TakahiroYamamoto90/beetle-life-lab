// Host tests for include/observation_scheduler.h (PR3a DeviceScheduler) together with
// the pure Reducer/Policy, a MOCK servo and a MOCK camera. Nothing here touches
// hardware; mock results are never real sensing. Build/run: scripts/run_core_tests.ps1.
#include <cstdio>
#include <string>
#include <vector>

#include "observation_core.h"
#include "observation_scheduler.h"

using namespace bll;

static int failures = 0, checks = 0;
#define CHECK(cond) do { ++checks; if (!(cond)) { ++failures; \
  std::printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #cond); } } while (0)

static const char* kBootA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
static const char* kBootB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

struct MockServo {
  enum Mode { Ok, Fail, Hang } mode = Ok;
  uint32_t durationMs = 1500;
  bool moving = false;
  uint64_t doneAt = 0;
  std::string actionId;
  unsigned moves = 0, aborts = 0;
  void request(const std::string& id, uint64_t now) {
    moving = true; actionId = id; doneAt = now + durationMs; ++moves;
  }
  MoveStatus poll(uint64_t now) {
    if (mode == Hang || now < doneAt) return MoveStatus::InProgress;
    moving = false;
    return mode == Fail ? MoveStatus::Failed : MoveStatus::DoneAssumed;
  }
  void abort() { moving = false; ++aborts; }
};

struct MockCamera {
  bool hang = false, ok = true;
  uint32_t durationMs = 2000;
  bool active = false;
  uint64_t doneAt = 0, requestedAt = 0;
  std::string actionId;
  unsigned captures = 0;
  void request(const std::string& id, uint64_t now) {
    active = true; actionId = id; requestedAt = now; doneAt = now + durationMs; ++captures;
  }
  CaptureStatus poll(uint64_t now) {
    if (hang || now < doneAt) return CaptureStatus::Pending;
    active = false;
    return ok ? CaptureStatus::Success : CaptureStatus::Failed;
  }
};

// Durable log + reducer + policy + scheduler: the shape the firmware runtime will take (PR3b).
struct Rig {
  std::vector<CommittedEvent> log;
  TemporalState state;
  PolicyConstraints constraints;
  PolicyConfig config;
  SchedulerConfig schedConfig;
  DeviceScheduler scheduler;
  MockServo servo;
  MockCamera camera;
  std::string boot = kBootA;
  uint64_t now = 0;
  unsigned seq = 0;
  bool persistOk = true;
  bool parallelSeen = false;
  std::vector<ScheduledResult> results;

  explicit Rig(bool fixed = true, bool front = false, bool top = false, bool back = false) {
    schedConfig.viewEnabled[0] = fixed; schedConfig.viewEnabled[1] = front;
    schedConfig.viewEnabled[2] = top; schedConfig.viewEnabled[3] = back;
    scheduler = DeviceScheduler(schedConfig);
    constraints.storageWritable = true;
    constraints.light = Light::Ok;
    config.order[0] = Pose::Front; config.order[1] = Pose::Back;
    config.order[2] = Pose::Top; config.order[3] = Pose::Fixed;
  }
  CommittedEvent base(EventType type) {
    CommittedEvent e;
    e.commitIndex = static_cast<uint32_t>(log.size());
    e.type = type;
    copyText(e.bootId, sizeof(e.bootId), boot.c_str());
    e.monotonicMs = now;
    std::string id = "xiao-0123456789ab/" + boot + "/" + std::to_string(log.size() + 1);
    copyText(e.eventId, sizeof(e.eventId), id.c_str());
    return e;
  }
  void commit(const CommittedEvent& e) { log.push_back(e); reduce(state, e); }
  void bootEvent(const char* id) { boot = id; now = 0; commit(base(EventType::Boot)); }
  std::string passive() { CommittedEvent e = base(EventType::PassiveCandidate); commit(e); return e.eventId; }
  ClockContext clock() const {
    ClockContext c; copyText(c.bootId, sizeof(c.bootId), boot.c_str()); c.nowMs = now; return c;
  }
  PolicyCapabilities caps() const {
    PolicyCapabilities c;
    for (unsigned i = 0; i < kPoseCount; ++i) c.pose[i] = scheduler.poseUsable(static_cast<Pose>(i));
    return c;
  }
  ActionPlan preview() {
    PolicyConstraints k = constraints;
    k.deviceBusy = scheduler.busy();
    return decide(state, makeView(state, clock()), caps(), k, config);
  }
  // Runtime step: persisted decision first, then hand the committed intent to the scheduler.
  ActionPlan stepPolicy() {
    ActionPlan plan = preview();
    if (!plan.decide) return plan;
    ++seq;
    const std::string actionId = "obs-" + boot + "-" + std::to_string(seq);
    CommittedEvent d = base(EventType::PolicyDecision);
    copyText(d.decisionId, sizeof(d.decisionId), ("dec-" + boot + "-" + std::to_string(seq)).c_str());
    if (isCapture(plan.action)) copyText(d.actionId, sizeof(d.actionId), actionId.c_str());
    d.action = plan.action;
    copyText(d.triggerId, sizeof(d.triggerId), plan.triggerId);
    commit(d);
    if (!isCapture(plan.action)) return plan;
    const bool committed = state.intent.open && actionId == state.intent.actionId;
    SchedulerStep s;
    CHECK(scheduler.start(actionId.c_str(), plan.action, committed, now, s));
    dispatch(s);
    return plan;
  }
  void dispatch(const SchedulerStep& s) {
    const std::string id = scheduler.result().actionId;
    if (s.command == Command::Move) servo.request(id, now);
    if (s.command == Command::Capture) camera.request(id, now);
    if (s.command != Command::PersistResult) return;
    if (s.abortMotion) servo.abort();
    const ScheduledResult& r = scheduler.result();
    results.push_back(r);
    if (persistOk) {
      CommittedEvent e = base(EventType::ActionResult);
      copyText(e.actionId, sizeof(e.actionId), r.actionId);
      e.status = r.status; e.pose = r.pose; e.capture = r.capture;
      commit(e);
    }
    scheduler.onPersisted(persistOk);
  }
  // Cooperative loop: every tick returns immediately (nothing blocks for the capture).
  void advance(uint64_t ms, uint64_t tick = 100) {
    for (uint64_t end = now + ms; now < end;) {
      now += tick;
      if (servo.moving && scheduler.phase() == SchedPhase::Moving)
        dispatch(scheduler.onMove(servo.actionId.c_str(), servo.poll(now), now));
      if (camera.active && scheduler.phase() == SchedPhase::Capturing)
        dispatch(scheduler.onCapture(camera.actionId.c_str(), camera.poll(now), "mock_camera_failed", now));
      dispatch(scheduler.poll(now));
      if (servo.moving && camera.active) parallelSeen = true;
    }
  }
};

static bool has(const ActionPlan& p, uint32_t r) { return (p.reasons & r) == r; }

static void testFixedCameraHasNoMotionAndSameOutcome() {
  // T21: the current FIXED-only hardware: capture at once, no servo, same Evidence.
  Rig r;
  r.bootEvent(kBootA); r.now = 1000; r.passive();
  ActionPlan p = r.stepPolicy();
  CHECK(p.action == Action::CaptureFixed);
  CHECK(r.scheduler.phase() == SchedPhase::Capturing && r.camera.captures == 1);
  CHECK(!r.scheduler.selfActuating());
  r.advance(3000);
  CHECK(r.results.size() == 1 && r.servo.moves == 0);
  if (!r.results.empty()) {
    const ScheduledResult& res = r.results[0];
    CHECK(res.status == ActionStatus::Completed && res.capture == CaptureOutcome::Success);
    CHECK(res.poseConfidence == PoseConfidence::FixedMount && !res.selfActuated);
    CHECK(std::string(res.reason) == "captured");
  }
  CHECK(!r.state.intent.open && r.state.completed == 1);
  // One fixed camera: the episode ends without another logged decision, even after cooldown.
  r.advance(6000);
  CHECK(!r.stepPolicy().decide && r.state.decisions == 1);
}

static void testMoveSettleCaptureAndActuationWindow() {
  // T22 window: motion + settling is self-actuation; the camera starts only after settling.
  Rig r(false, true);
  r.bootEvent(kBootA); r.now = 1000; r.passive();
  CHECK(r.stepPolicy().action == Action::CaptureFront);
  CHECK(r.scheduler.phase() == SchedPhase::Moving && r.servo.moves == 1 && r.camera.captures == 0);
  CHECK(r.scheduler.selfActuating());
  r.advance(500);
  const std::string during = r.passive();  // a candidate inside the window (T22)
  CHECK(r.scheduler.selfActuating());
  r.advance(1000);  // servo done at 2500 -> settling until 3300
  CHECK(r.scheduler.phase() == SchedPhase::Settling && r.scheduler.selfActuating() && r.camera.captures == 0);
  r.advance(900);
  CHECK(r.scheduler.phase() == SchedPhase::Capturing && !r.scheduler.selfActuating());
  CHECK(r.camera.requestedAt == 3300);
  r.advance(2500);
  CHECK(r.results.size() == 1);
  if (r.results.size() == 1) {
    const ScheduledResult& res = r.results[0];
    CHECK(res.status == ActionStatus::Completed && res.poseConfidence == PoseConfidence::Assumed);
    CHECK(res.selfActuated && res.actuationStartMs == 1000 && res.actuationEndMs == 3300);
    const uint64_t t = r.log[2].monotonicMs;  // the candidate committed at 1500
    CHECK(t >= res.actuationStartMs && t <= res.actuationEndMs);
  }
  CHECK(!r.parallelSeen);
  (void)during;
}

static void testNewEventDuringMoveNoParallelNoLoss() {
  // T09: a new candidate while moving is stored and answered WAIT; no second drive/capture.
  Rig r(false, true, false, true);
  r.bootEvent(kBootA); r.now = 1000; r.passive();
  r.stepPolicy();
  r.advance(500);
  r.passive();
  ActionPlan p = r.stepPolicy();
  CHECK(p.decide && p.action == Action::Wait && has(p, kActionInProgress));
  SchedulerStep s;
  CHECK(!r.scheduler.start("obs-other", Action::CaptureBack, true, r.now, s) && s.command == Command::None);
  r.advance(6000);
  CHECK(r.servo.moves == 1 && r.camera.captures == 1 && r.results.size() == 1);
  CHECK(r.state.passiveCount == 2 && !r.parallelSeen);
}

static void testMoveTimeoutAndFailureFailClosed() {
  // T10: no position feedback; a hung or failed move never captures and latches a fault.
  Rig r(false, true);
  r.servo.mode = MockServo::Hang;
  r.bootEvent(kBootA); r.now = 1000; r.passive();
  r.stepPolicy();
  r.advance(4000);
  CHECK(r.results.size() == 1 && r.camera.captures == 0 && r.servo.aborts == 1);
  if (!r.results.empty()) {
    CHECK(r.results[0].status == ActionStatus::Failed && r.results[0].capture == CaptureOutcome::NotAttempted);
    CHECK(std::string(r.results[0].reason) == "move_timeout");
    CHECK(r.results[0].poseConfidence == PoseConfidence::Unknown);
  }
  CHECK(r.scheduler.servoFaulted() && !r.scheduler.poseUsable(Pose::Front));
  CHECK(!r.state.intent.open && r.state.failed == 1);
  // The policy no longer sees a usable view; a direct start is refused without motion.
  r.advance(6000); r.passive();
  ActionPlan p = r.stepPolicy();
  CHECK(p.action == Action::Wait && has(p, kNoCalibratedView));
  SchedulerStep s;
  CHECK(r.scheduler.start("obs-direct", Action::CaptureFront, true, r.now, s));
  CHECK(s.command == Command::PersistResult && std::string(r.scheduler.result().reason) == "servo_fault_latched");
  CHECK(r.servo.moves == 1);
  r.scheduler.onPersisted(true);

  Rig f(true, true);
  f.servo.mode = MockServo::Fail;
  f.bootEvent(kBootA); f.now = 1000; f.passive();
  f.stepPolicy();
  f.advance(2000);
  CHECK(f.results.size() == 1 && f.results.size() == 1 && std::string(f.results[0].reason) == "move_failed");
  CHECK(f.scheduler.poseUsable(Pose::Fixed) && !f.scheduler.poseUsable(Pose::Front));
}

static void testCaptureTimeoutKeepsLoopResponsive() {
  // T11: a camera that never answers ends as FAILED; the loop keeps accepting evidence.
  Rig r;
  r.camera.hang = true;
  r.bootEvent(kBootA); r.now = 1000; r.passive();
  r.stepPolicy();
  r.advance(10000);
  r.passive();  // still stored and reduced while the capture is outstanding
  CHECK(r.stepPolicy().action == Action::Wait);
  r.advance(21000);
  CHECK(r.results.size() == 1);
  if (!r.results.empty()) {
    CHECK(r.results[0].capture == CaptureOutcome::Failed && std::string(r.results[0].reason) == "capture_timeout");
    CHECK(r.results[0].finishedMs == 31000);
  }
  CHECK(!r.scheduler.busy() && r.state.failed == 1 && r.state.consecutiveCaptureFailures == 1);
  const std::string stale = r.camera.actionId;
  SchedulerStep late = r.scheduler.onCapture(stale.c_str(), CaptureStatus::Success, "", r.now);
  CHECK(late.command == Command::None && r.scheduler.ignoredStale() == 1 && r.results.size() == 1);
  CHECK(r.state.passiveCount == 2);
}

static void testStorageFailureNeverActuates() {
  // T13: no durable intent -> no motion; an unstored result keeps the intent open.
  Rig r(false, true);
  r.constraints.storageWritable = false;
  r.bootEvent(kBootA); r.now = 1000; r.passive();
  ActionPlan p = r.stepPolicy();
  CHECK(!p.decide && has(p, kStorageNotReady) && r.servo.moves == 0);
  SchedulerStep s;
  CHECK(!r.scheduler.start("obs-uncommitted", Action::CaptureFront, false, r.now, s) && s.command == Command::None);

  r.constraints.storageWritable = true;
  r.persistOk = false;
  r.stepPolicy();
  r.advance(6000);
  CHECK(r.results.size() == 1 && r.scheduler.lastPersistFailed() && !r.scheduler.busy());
  CHECK(r.state.intent.open);
  r.persistOk = true;
  r.passive();
  ActionPlan next = r.stepPolicy();
  CHECK(next.action == Action::Wait && has(next, kActionInProgress) && r.servo.moves == 1);
}

static void testRebootNeverResumesMotion() {
  // T14: a fresh scheduler issues nothing by itself and knows no servo position.
  Rig r(false, true);
  r.bootEvent(kBootA); r.now = 1000; r.passive();
  r.stepPolicy();
  r.advance(500);
  CHECK(r.state.intent.open && r.scheduler.phase() == SchedPhase::Moving);
  // Reboot: RAM is gone. The runtime records the open intent as INTERRUPTED (existing code).
  r.scheduler = DeviceScheduler(r.schedConfig);
  r.servo.moving = false;
  CHECK(r.scheduler.poll(999999).command == Command::None);
  CHECK(r.scheduler.poseConfidence() == PoseConfidence::Unknown);
  const std::string openId = r.state.intent.actionId;
  r.bootEvent(kBootB);
  CommittedEvent interrupted = r.base(EventType::ActionResult);
  copyText(interrupted.actionId, sizeof(interrupted.actionId), openId.c_str());
  interrupted.status = ActionStatus::Interrupted;
  interrupted.pose = Pose::Front;
  r.commit(interrupted);
  CHECK(!r.state.intent.open && r.state.interrupted == 1 && r.servo.moves == 1);
  // The next capture on the same view must move again: the position is unknown.
  r.now = 1000; r.passive();
  CHECK(r.stepPolicy().action == Action::CaptureFront && r.servo.moves == 2);
}

static void testDisabledViewNeverChosen() {
  // T18: BACK is not calibrated; the episode visits FRONT and TOP only, then stops.
  Rig r(false, true, true, false);
  r.bootEvent(kBootA); r.now = 1000; r.passive();
  std::vector<Pose> visited;
  for (int i = 0; i < 6; ++i) {
    ActionPlan p = r.stepPolicy();
    if (p.decide && isCapture(p.action)) visited.push_back(p.pose);
    r.advance(6000);
  }
  CHECK(visited.size() == 2);
  if (visited.size() == 2) CHECK(visited[0] == Pose::Front && visited[1] == Pose::Top);
  for (const ScheduledResult& res : r.results) CHECK(res.pose != Pose::Back);
  CHECK(r.servo.moves == 2 && r.state.decisions == 2);
}

static void testFollowUpWaitsForCooldownThenRuns() {
  Rig r(false, true, false, true);
  r.bootEvent(kBootA); r.now = 1000; r.passive();
  r.stepPolicy();
  r.advance(4500);  // move 1500 + settle 800 + capture 2000 -> result at 5300
  CHECK(r.results.size() == 1 && !r.scheduler.busy());
  CHECK(!r.preview().decide && has(r.preview(), kCameraCooldownActive));
  const uint64_t wait = followUpRecheckInMs(makeView(r.state, r.clock()), r.config);
  CHECK(r.results[0].finishedMs == 5300 && r.now == 5500);
  CHECK(wait == 5000 - (r.now - r.results[0].finishedMs));
  r.now += wait - 1;
  CHECK(!r.stepPolicy().decide);
  r.now += 1;
  CHECK(followUpRecheckInMs(makeView(r.state, r.clock()), r.config) == 0);
  ActionPlan p = r.stepPolicy();
  CHECK(p.action == Action::CaptureBack && has(p, kEpisodeFollowUp));
  // No follow-up -> no timed recheck.
  Rig fixed;
  fixed.bootEvent(kBootA); fixed.now = 1000;
  CHECK(followUpRecheckInMs(makeView(fixed.state, fixed.clock()), fixed.config) == 0);
}

static std::string trace(Rig& r) {
  std::string out;
  for (const CommittedEvent& e : r.log)
    out += std::to_string(static_cast<int>(e.type)) + ":" + actionName(e.action) + ":" +
           std::to_string(e.monotonicMs) + ";";
  for (const ScheduledResult& res : r.results) out += std::string(res.reason) + ";";
  return out;
}

static void testParkReturnsHomeWithoutEvidence() {
  SchedulerConfig cfg;
  cfg.viewEnabled[0] = false; cfg.viewEnabled[1] = true; cfg.viewEnabled[2] = true;
  DeviceScheduler s(cfg);
  SchedulerStep out;
  // Capture at TOP first, so the arm is away from home.
  CHECK(s.start("obs-1", Action::CaptureTop, true, 0, out) && out.command == Command::Move);
  out = s.onMove("obs-1", MoveStatus::DoneAssumed, 1000);
  out = s.poll(1800);
  CHECK(out.command == Command::Capture);
  out = s.onCapture("obs-1", CaptureStatus::Success, "none", 3000);
  s.onPersisted(true);
  CHECK(!s.busy() && s.currentPose() == Pose::Top && s.poseConfidence() == PoseConfidence::Assumed);
  // Park: a Move with the reserved id, no capture, back to Idle and ASSUMED at FRONT.
  CHECK(s.startPark(Pose::Front, 4000, out) && out.command == Command::Move && out.pose == Pose::Front);
  CHECK(s.busy() && s.parking() && s.selfActuating());
  CHECK(s.onMove("obs-1", MoveStatus::DoneAssumed, 4100).command == Command::None);  // stale id
  CHECK(s.parking());
  out = s.onMove(DeviceScheduler::kParkActionId, MoveStatus::DoneAssumed, 5000);
  CHECK(out.command == Command::None && !s.busy() && !s.parking());
  CHECK(s.currentPose() == Pose::Front && s.poseConfidence() == PoseConfidence::Assumed);
  CHECK(std::string(s.result().actionId) == "obs-1");  // the capture result is untouched
  // Already home: nothing to do. FIXED or a disabled view is never a park target.
  CHECK(!s.startPark(Pose::Front, 6000, out));
  CHECK(!s.startPark(Pose::Fixed, 6000, out));
  CHECK(!s.startPark(Pose::Back, 6000, out));
  // A park that times out latches the servo fault and offers no moving view afterwards.
  s.invalidatePose();
  CHECK(s.poseConfidence() == PoseConfidence::Unknown);
  CHECK(s.startPark(Pose::Front, 7000, out));
  out = s.poll(7000 + cfg.moveTimeoutMs);
  CHECK(out.abortMotion && out.command == Command::None && !s.busy());
  CHECK(s.servoFaulted() && !s.poseUsable(Pose::Front) && !s.poseUsable(Pose::Top));
  // While busy with a capture, a park is refused.
  DeviceScheduler b(cfg);
  CHECK(b.start("obs-2", Action::CaptureTop, true, 0, out));
  CHECK(!b.startPark(Pose::Front, 10, out));
}

static void testDeterministic() {
  std::string a, b;
  for (std::string* out : {&a, &b}) {
    Rig r(true, true, true, true);
    r.bootEvent(kBootA); r.now = 1000; r.passive();
    for (int i = 0; i < 5; ++i) { r.stepPolicy(); r.advance(6000); }
    *out = trace(r);
  }
  CHECK(!a.empty() && a == b);
}

int main() {
  testFixedCameraHasNoMotionAndSameOutcome();
  testMoveSettleCaptureAndActuationWindow();
  testNewEventDuringMoveNoParallelNoLoss();
  testMoveTimeoutAndFailureFailClosed();
  testCaptureTimeoutKeepsLoopResponsive();
  testStorageFailureNeverActuates();
  testRebootNeverResumesMotion();
  testDisabledViewNeverChosen();
  testFollowUpWaitsForCooldownThenRuns();
  testParkReturnsHomeWithoutEvidence();
  testDeterministic();
  CHECK(std::string(poseConfidenceName(PoseConfidence::Assumed)) == "POSE_ASSUMED");
  std::printf("%s: scheduler %d checks, %d failures\n", failures ? "FAILED" : "OK", checks, failures);
  return failures ? 1 : 0;
}
