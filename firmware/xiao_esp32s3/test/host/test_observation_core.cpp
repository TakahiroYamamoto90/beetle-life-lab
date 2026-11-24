// Host tests for include/observation_core.h (pure Reducer / View / Policy).
// Build and run: scripts/run_core_tests.ps1. With --scenarios <file> it also writes
// the SYNTHETIC demo comparison used by the AD3 bridge dashboard.
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include "observation_core.h"

using namespace bll;

static int failures = 0, checks = 0;
#define CHECK(cond) do { ++checks; if (!(cond)) { ++failures; \
  std::printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #cond); } } while (0)

static const char* kBootA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
static const char* kBootB = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";

// A durable log plus the runtime loop of the firmware, with a Mock Executor.
struct Harness {
  std::vector<CommittedEvent> log;
  TemporalState state;
  PolicyCapabilities caps;
  PolicyConstraints constraints;
  PolicyConfig config;
  std::string boot = kBootA;
  uint64_t now = 0;
  unsigned decisionSeq = 0;
  CaptureOutcome nextCapture = CaptureOutcome::Success;
  bool nextLarvaVisible = false;  // model output of the next captured image (0.17.0)
  unsigned captures = 0;

  Harness() { caps.pose[0] = true; constraints.storageWritable = true; constraints.light = Light::Ok; }

  CommittedEvent base(EventType type, Origin origin = Origin::Live) {
    CommittedEvent e;
    e.commitIndex = static_cast<uint32_t>(log.size());
    e.type = type;
    e.origin = origin;
    copyText(e.bootId, sizeof(e.bootId), boot.c_str());
    e.monotonicMs = now;
    std::string id = "xiao-0123456789ab/" + boot + "/" + std::to_string(log.size() + 1);
    copyText(e.eventId, sizeof(e.eventId), id.c_str());
    return e;
  }
  ApplyResult commit(const CommittedEvent& e) { log.push_back(e); return reduce(state, e); }
  std::string bootEvent(const char* id) {
    boot = id; now = 0;
    CommittedEvent e = base(EventType::Boot); commit(e); return e.eventId;
  }
  std::string passive(Origin origin = Origin::Live) {
    CommittedEvent e = base(EventType::PassiveCandidate, origin); commit(e); return e.eventId;
  }
  ClockContext clock() const {
    ClockContext c; copyText(c.bootId, sizeof(c.bootId), boot.c_str()); c.nowMs = now; return c;
  }
  ActionPlan preview() const {
    return decide(state, makeView(state, clock()), caps, constraints, config);
  }
  // Runtime step: persist the decision (intent) BEFORE any action, then execute.
  ActionPlan step(bool execute = true) {
    ActionPlan plan = preview();
    if (!plan.decide) return plan;
    CommittedEvent d = base(EventType::PolicyDecision);
    ++decisionSeq;
    std::string actionId = "obs-" + boot + "-" + std::to_string(decisionSeq);
    copyText(d.decisionId, sizeof(d.decisionId), ("dec-" + boot + "-" + std::to_string(decisionSeq)).c_str());
    if (isCapture(plan.action)) copyText(d.actionId, sizeof(d.actionId), actionId.c_str());
    d.action = plan.action;
    copyText(d.triggerId, sizeof(d.triggerId), plan.triggerId);
    commit(d);
    if (!isCapture(plan.action) || !execute) return plan;
    ++captures;
    CommittedEvent r = base(EventType::ActionResult);
    copyText(r.actionId, sizeof(r.actionId), actionId.c_str());
    r.pose = plan.pose;
    r.capture = nextCapture;
    r.status = nextCapture == CaptureOutcome::Success ? ActionStatus::Completed : ActionStatus::Failed;
    r.larvaVisible = nextLarvaVisible && nextCapture == CaptureOutcome::Success;
    commit(r);
    return plan;
  }
};

static bool has(const ActionPlan& p, uint32_t reason) { return (p.reasons & reason) == reason; }

static std::string reasonList(uint32_t reasons) {
  std::string out;
  for (unsigned bit = 0; bit < 32; ++bit)
    if ((reasons >> bit) & 1u) {
      const char* name = reasonCode(bit);
      if (!out.empty()) out += ",";
      out += name ? name : "UNKNOWN_REASON";
    }
  return out;
}

static void testReplayIsDeterministicAndIdempotent() {
  Harness h;
  h.bootEvent(kBootA); h.now = 1000; h.passive(); h.now = 1200; h.step();
  h.now = 40000; h.passive(); h.now = 40100; h.step();
  TemporalState replay;
  for (const CommittedEvent& e : h.log) CHECK(reduce(replay, e) == ApplyResult::Applied);
  CHECK(replay.reducedThrough == h.state.reducedThrough);
  CHECK(replay.decisions == h.state.decisions && replay.completed == h.state.completed);
  CHECK(std::string(replay.lastSuccessByPose[0].eventId) == h.state.lastSuccessByPose[0].eventId);
  // Re-applying committed events (e.g. a USB re-send already deduplicated by the store) is a no-op.
  for (const CommittedEvent& e : h.log) CHECK(reduce(replay, e) == ApplyResult::AlreadyApplied);
  CHECK(replay.decisions == 2);
  CHECK(replay.lastVisualMovement.present == false);  // captures never confirm movement
}

static void testGapMakesStateIncomplete() {
  Harness h;
  h.bootEvent(kBootA);
  CommittedEvent skipped = h.base(EventType::PassiveCandidate);
  skipped.commitIndex = 5;
  CHECK(reduce(h.state, skipped) == ApplyResult::Gap);
  h.passive();
  ActionPlan p = h.preview();
  CHECK(p.decide && p.action == Action::Wait && has(p, kStateIncomplete));
}

static void testSameCandidateDifferentHistory() {
  // Front recently observed, back never: choose BACK. Baseline B re-captures FRONT.
  Harness h;
  h.caps.pose[0] = false; h.caps.pose[1] = true; h.caps.pose[3] = true;
  h.config.order[0] = Pose::Front; h.config.order[1] = Pose::Back;
  h.config.order[2] = Pose::Top; h.config.order[3] = Pose::Fixed;
  h.bootEvent(kBootA);
  h.now = 1000; h.passive();
  ActionPlan first = h.step();
  CHECK(first.action == Action::CaptureFront);
  h.now = 12000; h.passive();
  ActionPlan p = h.preview();
  CHECK(p.action == Action::CaptureBack);
  CHECK(has(p, recentlyChecked(Pose::Front)) && has(p, notRecentlyObserved(Pose::Back)));
  ActionPlan b = decideBaselineB(h.state, makeView(h.state, h.clock()), Pose::Front);
  CHECK(b.action == Action::CaptureFront && has(b, kBaselineEveryCandidate));
  h.step();
  // Back just captured too: no new information expected, WAIT (cooldown elapsed).
  h.now = 20000; h.passive();
  ActionPlan w = h.preview();
  CHECK(w.action == Action::Wait && has(w, kAllViewsRecentlyChecked));
  CHECK(decideBaselineB(h.state, makeView(h.state, h.clock()), Pose::Front).action == Action::CaptureFront);
  // Later both views are stale again: the one seen longest ago is chosen.
  h.step(); h.now = 60000; h.passive();
  CHECK(h.preview().action == Action::CaptureFront);
}

static void testCooldownLowLightAndUnknownLight() {
  Harness h;
  h.bootEvent(kBootA); h.now = 1000; h.passive(); h.step();
  h.now = 3000; h.passive();
  CHECK(has(h.preview(), kCameraCooldownActive));
  h.now = 9000;
  h.constraints.light = Light::Low;
  ActionPlan dark = h.preview();
  CHECK(dark.action == Action::Wait && has(dark, kLowLight));
  h.constraints.light = Light::Unknown;
  h.config.viewRecencyMs = 1000;
  ActionPlan unknown = h.preview();
  CHECK(unknown.action == Action::CaptureFixed && has(unknown, kLightUnknown));
}

static void testDuplicateTriggerHasSingleDecision() {
  Harness h;
  h.bootEvent(kBootA); h.now = 1000; h.passive();
  CHECK(h.step().decide);
  // The store answers a re-sent candidate with "duplicate" and commits nothing.
  CHECK(!h.preview().decide);
  CHECK(h.state.decisions == 1 && h.captures == 1);
}

static void testRecordedAndSyntheticNeverActuate() {
  Harness live;
  live.bootEvent(kBootA); live.now = 1000;
  CHECK(live.commit(live.base(EventType::PassiveCandidate, Origin::Recorded)) == ApplyResult::ForeignOrigin);
  CHECK(!live.preview().decide && live.state.foreignOrigin == 1);

  Harness replay;
  replay.state.streamOrigin = Origin::Recorded;
  replay.constraints.liveMode = false;
  replay.boot = kBootA; replay.now = 1000;
  replay.passive(Origin::Recorded);
  ActionPlan p = replay.step();
  CHECK(p.action == Action::Wait && has(p, kRecordedSourceNoActuation) && replay.captures == 0);
}

static void testFailuresAreNotAbsenceAndAreBounded() {
  Harness h;
  h.bootEvent(kBootA);
  h.nextCapture = CaptureOutcome::Failed;
  h.config.cameraCooldownMs = 0;
  h.now = 1000; h.passive(); h.step();
  CHECK(h.state.failed == 1 && !h.state.lastSuccessByPose[0].present);
  CHECK(!h.state.lastVisualPresence.present);
  h.now = 2000; h.passive();
  ActionPlan retry = h.preview();
  CHECK(retry.action == Action::CaptureFixed && has(retry, kPreviousAttemptFailed));
  h.step();
  h.now = 3000; h.passive();
  ActionPlan stop = h.preview();
  CHECK(stop.action == Action::Wait && has(stop, kVisionRepeatedlyFailed));
  // Waiting never captures, so only a reboot (operator repair) may lift the stop.
  h.step();
  h.now = 4000; h.passive();
  CHECK(has(h.preview(), kVisionRepeatedlyFailed));
  h.bootEvent(kBootB);
  CHECK(h.state.consecutiveCaptureFailures == 0 && h.state.failed == 2);  // history kept
  h.now = 1000; h.passive();
  ActionPlan afterRepair = h.preview();
  CHECK(afterRepair.action == Action::CaptureFixed && !has(afterRepair, kVisionRepeatedlyFailed));
  h.step();  // still broken: the stop returns after the limit
  h.now = 2000; h.passive(); h.step();
  h.now = 3000; h.passive();
  CHECK(has(h.preview(), kVisionRepeatedlyFailed));
}

static void testIntentPersistedBeforeActionAndInterruptedAfterReboot() {
  Harness h;
  h.bootEvent(kBootA); h.now = 1000; h.passive();
  ActionPlan p = h.step(false);  // decision committed, device reset before the result
  CHECK(isCapture(p.action) && h.state.intent.open);
  h.now = 2000; h.passive();
  CHECK(has(h.preview(), kActionInProgress));
  std::string action = h.state.intent.actionId;
  h.bootEvent(kBootB);
  CHECK(h.state.intent.open);  // recovery must close it explicitly
  CommittedEvent r = h.base(EventType::ActionResult);
  copyText(r.actionId, sizeof(r.actionId), action.c_str());
  r.status = ActionStatus::Interrupted;
  h.commit(r);
  CHECK(!h.state.intent.open && h.state.interrupted == 1 && !h.state.lastAttempt.present);
  // The pending candidate came from the previous boot: its age is not comparable.
  ActionPlan old = h.preview();
  CHECK(old.action == Action::Wait && has(old, kTriggerFromPreviousBoot));
}

static void testStaleTriggerStorageAndSafety() {
  Harness h;
  h.bootEvent(kBootA); h.now = 1000; h.passive();
  h.now = 1000 + 61000;
  CHECK(has(h.preview(), kTriggerStale));
  h.now = 2000;
  h.constraints.storageWritable = false;
  ActionPlan none = h.preview();
  CHECK(!none.decide && has(none, kStorageNotReady));
  h.constraints.storageWritable = true;
  h.constraints.safetyOk = false;
  CHECK(has(h.preview(), kSafetyBlocked));
  h.constraints.safetyOk = true;
  h.constraints.deviceBusy = true;
  CHECK(has(h.preview(), kDeviceBusy));
  h.constraints.deviceBusy = false;
  h.caps.pose[0] = false;
  CHECK(has(h.preview(), kNoCalibratedView));
}

static void testBurstIsCoalescedAndFinite() {
  Harness h;
  h.bootEvent(kBootA);
  for (int i = 0; i < 50; ++i) { h.now = 1000 + i * 10; h.passive(); }
  CHECK(h.state.coalescedTriggers == 49);
  h.step();
  CHECK(!h.preview().decide && h.state.decisions == 1 && h.captures == 1);
}

// Runs one trigger's episode like a scheduler would: execute, wait out the
// camera cooldown, ask again, until the policy has nothing more to persist.
static std::vector<ActionPlan> runEpisode(Harness& h, unsigned maxSteps = 10) {
  std::vector<ActionPlan> plans;
  for (unsigned i = 0; i < maxSteps; ++i) {
    ActionPlan p = h.step();
    if (!p.decide) break;
    plans.push_back(p);
    if (!isCapture(p.action)) break;
    h.now += h.config.cameraCooldownMs;
  }
  return plans;
}

static void testFixedOnlyEpisodeAddsNoDecision() {
  // T21: one camera, one trigger -> exactly the previous single decision and capture.
  Harness h;
  h.bootEvent(kBootA); h.now = 1000; h.passive();
  std::vector<ActionPlan> plans = runEpisode(h);
  CHECK(plans.size() == 1 && plans[0].action == Action::CaptureFixed);
  CHECK(h.state.decisions == 1 && h.captures == 1 && h.state.episodeCaptures == 1);
  ActionPlan after = h.preview();
  CHECK(!after.decide && has(after, kEpisodeFollowUp) && has(after, kAllViewsRecentlyChecked));
}

static void testEpisodeVisitsUnseenViewsWithinBudget() {
  // T03/T17: three views, one trigger -> each unseen view once, then stop.
  Harness h;
  h.caps.pose[0] = false; h.caps.pose[1] = true; h.caps.pose[2] = true; h.caps.pose[3] = true;
  h.config.order[0] = Pose::Front; h.config.order[1] = Pose::Back;
  h.config.order[2] = Pose::Top; h.config.order[3] = Pose::Fixed;
  h.bootEvent(kBootA); h.now = 1000;
  const std::string trigger = h.passive();
  std::vector<ActionPlan> plans = runEpisode(h);
  CHECK(plans.size() == 3);
  if (plans.size() == 3) {
    CHECK(plans[0].action == Action::CaptureFront && has(plans[0], kPassiveCandidateNew));
    CHECK(plans[1].action == Action::CaptureBack && has(plans[1], kEpisodeFollowUp));
    CHECK(!has(plans[1], kPassiveCandidateNew));
    CHECK(plans[2].action == Action::CaptureTop && has(plans[2], kEpisodeFollowUp));
    for (const ActionPlan& p : plans) CHECK(trigger == p.triggerId);
  }
  CHECK(h.captures == 3 && h.state.episodeCaptures == 3);
  ActionPlan after = h.preview();
  CHECK(!after.decide && has(after, kEpisodeBudgetExhausted));
  // A budget of 2 stops one view earlier, without logging a WAIT.
  Harness small;
  small.caps = h.caps; small.config = h.config; small.config.maxCapturesPerEpisode = 2;
  small.bootEvent(kBootA); small.now = 1000; small.passive();
  CHECK(runEpisode(small).size() == 2 && small.state.decisions == 2);
  CHECK(has(small.preview(), kEpisodeBudgetExhausted));
}

static void testArmDefaultOrderStartsAtHomeFront() {
  // policy v3 default order: FRONT (home, 0 deg) -> TOP (90) -> BACK (180); FIXED disabled.
  Harness h;
  h.caps.pose[0] = false; h.caps.pose[1] = true; h.caps.pose[2] = true; h.caps.pose[3] = true;
  h.bootEvent(kBootA); h.now = 1000; h.passive();
  std::vector<ActionPlan> plans = runEpisode(h);
  CHECK(plans.size() == 3);
  if (plans.size() == 3) {
    CHECK(plans[0].action == Action::CaptureFront);
    CHECK(plans[1].action == Action::CaptureTop);
    CHECK(plans[2].action == Action::CaptureBack);
  }
}

static void testSelfActuationGatesNewTriggersOnly() {
  // A new trigger during/just after the device's own servo motion waits with a reason ...
  Harness h;
  h.caps.pose[0] = false; h.caps.pose[1] = true; h.caps.pose[2] = true;
  h.bootEvent(kBootA); h.now = 1000; h.passive();
  h.constraints.selfActuationRecent = true;
  ActionPlan wait = h.step();
  CHECK(wait.decide && wait.action == Action::Wait && has(wait, kSelfActuationRecent));
  CHECK(h.captures == 0);
  // ... while an episode's follow-up is not blocked by the episode's own move.
  Harness f;
  f.caps = h.caps;
  f.bootEvent(kBootA); f.now = 1000; f.passive();
  ActionPlan first = f.step();
  CHECK(first.action == Action::CaptureFront);
  f.constraints.selfActuationRecent = true;
  f.now += f.config.cameraCooldownMs;
  ActionPlan next = f.step();
  CHECK(next.decide && next.action == Action::CaptureTop && has(next, kEpisodeFollowUp));
  CHECK(!has(next, kSelfActuationRecent));
  // Safety order: device busy is reported before the self-actuation guard.
  Harness b;
  b.bootEvent(kBootA); b.now = 1000; b.passive();
  b.constraints.deviceBusy = true; b.constraints.selfActuationRecent = true;
  ActionPlan busy = b.step();
  CHECK(has(busy, kDeviceBusy) && !has(busy, kSelfActuationRecent));
}

static void testEpisodeStopsOnFailureAndRestartsOnNewTrigger() {
  Harness h;
  h.caps.pose[0] = false; h.caps.pose[1] = true; h.caps.pose[3] = true;
  h.config.order[0] = Pose::Front; h.config.order[1] = Pose::Back;
  h.config.order[2] = Pose::Top; h.config.order[3] = Pose::Fixed;
  h.bootEvent(kBootA); h.now = 1000; h.passive();
  h.nextCapture = CaptureOutcome::Failed;
  CHECK(runEpisode(h).size() == 1);           // a failed image ends the episode
  CHECK(!h.state.episodeLastCaptureOk && !makeView(h.state, h.clock()).followUp);
  // A new trigger opens a new episode with a fresh budget.
  h.nextCapture = CaptureOutcome::Success;
  h.now = 40000; const std::string second = h.passive();
  std::vector<ActionPlan> plans = runEpisode(h);
  CHECK(plans.size() == 2 && h.state.episodeCaptures == 2 && second == h.state.episodeTrigger);
  // Follow-ups never actuate from a RECORDED stream or without durable storage.
  Harness r;
  r.caps = h.caps; r.config = h.config;
  r.bootEvent(kBootA); r.now = 1000; r.passive(); r.step();
  r.now += r.config.cameraCooldownMs;
  r.constraints.storageWritable = false;
  CHECK(!r.preview().decide && has(r.preview(), kStorageNotReady));
  r.constraints.storageWritable = true; r.constraints.liveMode = false;
  CHECK(!r.preview().decide && has(r.preview(), kRecordedSourceNoActuation));
  r.constraints.liveMode = true; r.now = 1000 + 61000;
  CHECK(!r.preview().decide && has(r.preview(), kTriggerStale));
}

static void testEpisodeBudgetSurvivesReplay() {
  Harness h;
  h.caps.pose[0] = false; h.caps.pose[1] = true; h.caps.pose[3] = true;
  h.config.order[0] = Pose::Front; h.config.order[1] = Pose::Back;
  h.config.order[2] = Pose::Top; h.config.order[3] = Pose::Fixed;
  h.bootEvent(kBootA); h.now = 1000; h.passive(); runEpisode(h);
  TemporalState replay;
  for (const CommittedEvent& e : h.log) reduce(replay, e);
  CHECK(replay.episodeCaptures == h.state.episodeCaptures && replay.episodeCaptures == 2);
  CHECK(std::string(replay.episodeTrigger) == h.state.episodeTrigger);
  CHECK(replay.episodeLastCaptureOk == h.state.episodeLastCaptureOk);
}

static void testUtcIsNeverRequired() {
  Harness h;
  h.bootEvent(kBootA); h.now = 1000;
  CommittedEvent e = h.base(EventType::PassiveCandidate);
  e.hasUtc = false;
  h.commit(e);
  CHECK(h.step().action == Action::CaptureFixed);  // monotonic time is enough
  CHECK(!h.state.lastPassive.hasUtc);
}

// ------------------------------------------------------- demo scenarios ---

struct Row { std::string label, proposed, proposedReasons, baseline; };

static void scenarioJson(FILE* f, const char* id, const char* title, const char* note,
                         const std::vector<Row>& rows, bool last) {
  std::fprintf(f, "    {\"id\":\"%s\",\"title\":\"%s\",\"note\":\"%s\",\"steps\":[\n", id, title, note);
  for (size_t i = 0; i < rows.size(); ++i)
    std::fprintf(f, "      {\"input\":\"%s\",\"proposed\":\"%s\",\"reasons\":[%s],\"baseline_b\":\"%s\"}%s\n",
                 rows[i].label.c_str(), rows[i].proposed.c_str(), rows[i].proposedReasons.c_str(),
                 rows[i].baseline.c_str(), i + 1 < rows.size() ? "," : "");
  std::fprintf(f, "    ]}%s\n", last ? "" : ",");
}

static std::string quoted(uint32_t reasons) {
  std::string list = reasonList(reasons), out;
  size_t start = 0;
  while (start < list.size()) {
    size_t end = list.find(',', start);
    if (end == std::string::npos) end = list.size();
    if (!out.empty()) out += ",";
    out += "\"" + list.substr(start, end - start) + "\"";
    start = end + 1;
  }
  return out;
}

static Row record(Harness& h, const std::string& label, Pose baselinePose) {
  h.passive();
  ActionPlan b = decideBaselineB(h.state, makeView(h.state, h.clock()), baselinePose);
  ActionPlan p = h.step();
  return Row{label, actionName(p.action), quoted(p.reasons), actionName(b.action)};
}

static bool writeScenarios(const char* path) {
  FILE* f = std::fopen(path, "w");
  if (!f) return false;
  std::fprintf(f, "{\n  \"schema\":\"bll.observation_demo_scenarios.v1\",\n  \"origin\":\"SYNTHETIC\",\n"
                  "  \"generator\":\"firmware/xiao_esp32s3/test/host/test_observation_core.cpp\",\n"
                  "  \"policy_version\":\"%s\",\n  \"reducer_version\":\"%s\",\n"
                  "  \"claim\":\"Rule-based policy on synthetic inputs. Not a measured detection or larva result.\",\n"
                  "  \"scenarios\":[\n", kPolicyVersion, kReducerVersion);
  {
    Harness h;
    h.caps.pose[0] = false; h.caps.pose[1] = true; h.caps.pose[3] = true;
    h.config.order[0] = Pose::Front; h.config.order[1] = Pose::Back;
    h.config.order[2] = Pose::Top; h.config.order[3] = Pose::Fixed;
    h.bootEvent(kBootA);
    std::vector<Row> rows;
    h.now = 1000; rows.push_back(record(h, "振動候補（前面も背面も未観測）", Pose::Front));
    h.now = 12000; rows.push_back(record(h, "同じ振動候補（前面を11秒前に確認済み）", Pose::Front));
    h.now = 20000; rows.push_back(record(h, "同じ振動候補（前面・背面とも30秒以内に確認済み）", Pose::Front));
    scenarioJson(f, "history_changes_action", "同じ振動入力でも履歴で行動が変わる（前面・背面の2視点を仮定）",
                 "三視点機構は未製作。視点の可用性は仮定。", rows, false);
  }
  {
    Harness h;
    h.bootEvent(kBootA);
    std::vector<Row> rows;
    const int strikes[] = {18000, 36000, 54000, 72000};
    for (int t : strikes) { h.now = static_cast<uint64_t>(t); rows.push_back(record(h, "人工掘削の振動候補 t=" + std::to_string(t / 1000) + "s", Pose::Fixed)); }
    scenarioJson(f, "fixed_camera_live_equivalent", "固定カメラ1台（実機と同じ構成）で18秒ごとの振動候補",
                 "LIVE閉ループと同じ規則。直近30秒に撮影済みなら待機。", rows, false);
  }
  {
    Harness h;
    h.bootEvent(kBootA);
    h.constraints.light = Light::Low;
    std::vector<Row> rows;
    h.now = 1000; rows.push_back(record(h, "振動候補（暗所と申告）", Pose::Fixed));
    h.constraints.light = Light::Ok;
    h.nextCapture = CaptureOutcome::Failed;
    h.config.cameraCooldownMs = 0;
    h.now = 2000; rows.push_back(record(h, "振動候補（撮影失敗）", Pose::Fixed));
    h.now = 3000; rows.push_back(record(h, "振動候補（再度撮影失敗）", Pose::Fixed));
    h.now = 4000; rows.push_back(record(h, "振動候補（失敗が続いた後）", Pose::Fixed));
    scenarioJson(f, "dark_and_failures", "暗所と撮影失敗：観測不能を不在にしない",
                 "撮影失敗は『見えない』ではなく観測できなかった事実として記録。", rows, false);
  }
  {
    Harness h;
    h.caps.pose[0] = false; h.caps.pose[1] = true; h.caps.pose[2] = true; h.caps.pose[3] = true;
    h.config.order[0] = Pose::Front; h.config.order[1] = Pose::Back;
    h.config.order[2] = Pose::Top; h.config.order[3] = Pose::Fixed;
    h.config.maxCapturesPerEpisode = 2;
    h.bootEvent(kBootA);
    std::vector<Row> rows;
    h.now = 1000; rows.push_back(record(h, "振動候補（3視点とも未観測）", Pose::Front));
    h.now += h.config.cameraCooldownMs;
    ActionPlan follow = h.step();
    rows.push_back(Row{"同じ候補の続き（前面は撮影済み）", actionName(follow.action), quoted(follow.reasons), "—"});
    h.now += h.config.cameraCooldownMs;
    ActionPlan stop = h.preview();
    rows.push_back(Row{"同じ候補の続き（撮影予算2回を使い切り・記録しない）", stop.decide ? actionName(stop.action) : "NONE",
                       quoted(stop.reasons), "—"});
    scenarioJson(f, "episode_follow_up", "1つの振動候補で未観測の視点を順に撮り、予算で止まる（3視点を仮定）",
                 "三視点機構は未製作。視点の可用性は仮定。予算到達後は判断を記録せず終了。", rows, false);
  }
  {
    // 0.18.0: look again where the image AI saw the larva; explore once it is no longer seen there.
    Harness h;
    h.caps.pose[0] = false; h.caps.pose[1] = true; h.caps.pose[2] = true; h.caps.pose[3] = true;
    h.config.order[0] = Pose::Back; h.config.order[1] = Pose::Front;
    h.config.order[2] = Pose::Top; h.config.order[3] = Pose::Fixed;
    h.bootEvent(kBootA);
    std::vector<Row> rows;
    h.nextLarvaVisible = true;
    h.now = 1000; rows.push_back(record(h, "振動候補（後ろで画像AIが『見えた』と判定）", Pose::Front));
    h.now = 21000; rows.push_back(record(h, "次の振動候補（20秒後・後ろでまた『見えた』）", Pose::Front));
    h.nextLarvaVisible = false;
    h.now = 41000; rows.push_back(record(h, "次の振動候補（40秒後・後ろでは『見えない』）", Pose::Front));
    h.now += h.config.cameraCooldownMs;
    ActionPlan follow = h.step();
    rows.push_back(Row{"同じ候補の続き（見えなくなったので別の方向へ）", actionName(follow.action), quoted(follow.reasons), "—"});
    scenarioJson(f, "larva_stay", "幼虫が見えた方向をもう一度撮り、見えなくなったら別の方向を探す（3視点を仮定）",
                 "『見えた』は画像AIの判定（未検証）で、幼虫の検出ではない。同じ方向は2分以内・連続3回まで。", rows, true);
  }
  std::fprintf(f, "  ]\n}\n");
  return std::fclose(f) == 0;
}

// 0.17.0: an image classed VISIBLE_LARVA ends the episode (FRONT seen -> no TOP/BACK; TOP seen ->
// no BACK); the next trigger starts a full episode again; the rule can be turned off; replay agrees.
static void testLarvaVisibleStopsTheEpisode() {
  Harness h;
  h.caps.pose[0] = false; h.caps.pose[1] = true; h.caps.pose[2] = true; h.caps.pose[3] = true;
  h.bootEvent(kBootA); h.now = 1000; h.passive();
  h.nextLarvaVisible = true;
  std::vector<ActionPlan> plans = runEpisode(h);
  CHECK(plans.size() == 1 && plans[0].action == Action::CaptureFront);
  CHECK(h.state.episodeLarvaVisible && h.state.episodeCaptures == 1);
  ActionPlan stop = decideFollowUp(h.state, makeView(h.state, h.clock()), h.caps, h.constraints, h.config);
  CHECK(!stop.decide && has(stop, kLarvaVisibleStop));
  CHECK(followUpRecheckInMs(makeView(h.state, h.clock()), h.config) == 0);
  TemporalState replay;
  for (const CommittedEvent& e : h.log) reduce(replay, e);
  CHECK(replay.episodeLarvaVisible && replay.episodeCaptures == 1);
  // TOP sees it: FRONT and TOP only.
  Harness t;
  t.caps = h.caps; t.bootEvent(kBootA); t.now = 1000; t.passive();
  ActionPlan first = t.step(); t.now += t.config.cameraCooldownMs;
  t.nextLarvaVisible = true; ActionPlan second = t.step(); t.now += t.config.cameraCooldownMs;
  ActionPlan third = t.step();
  CHECK(first.action == Action::CaptureFront && second.action == Action::CaptureTop && !third.decide);
  CHECK(has(third, kLarvaVisibleStop) && t.captures == 2);
  // A new trigger is a new episode: 0.18.0 looks at TOP again first; not seen there, so the
  // other views follow (a full episode again).
  t.now += 60000; t.nextLarvaVisible = false; t.passive();
  std::vector<ActionPlan> again = runEpisode(t);
  CHECK(again.size() == 3 && !t.state.episodeLarvaVisible);
  CHECK(again[0].action == Action::CaptureTop && has(again[0], kLarvaLastSeenHere));
  // Rule off: all three views even when the larva is visible.
  Harness off;
  off.caps = h.caps; off.config.stopWhenLarvaVisible = false;
  off.bootEvent(kBootA); off.now = 1000; off.passive(); off.nextLarvaVisible = true;
  CHECK(runEpisode(off).size() == 3);
}

// 0.18.0: a new trigger looks again where the image AI last saw the larva (within 120 s), at most
// 3 times in a row; then it explores, and may come back afterwards. Not seen there -> explore at
// once. Older sightings, another boot or the rule off -> explore. Replay rebuilds the same state.
static void testLookAgainWhereLarvaWasSeen() {
  Harness h;
  h.caps.pose[0] = false; h.caps.pose[1] = true; h.caps.pose[2] = true; h.caps.pose[3] = true;
  h.bootEvent(kBootA); h.now = 1000; h.passive();
  ActionPlan first = h.step(); h.now += h.config.cameraCooldownMs;   // FRONT: not seen
  h.nextLarvaVisible = true; ActionPlan seen = h.step();               // TOP: seen -> episode ends
  CHECK(first.action == Action::CaptureFront && seen.action == Action::CaptureTop);
  CHECK(h.state.lastLarvaSeen.present && h.state.lastLarvaPose == Pose::Top && h.state.larvaSeenStreak == 1);
  // triggers 15 s apart incl. the 5 s cooldown (inside the 30 s view recency). Three stays in a row, each seen again (TOP is "recently checked" but chosen on purpose)
  for (int i = 0; i < 3; ++i) {
    h.now += 10000; h.passive();
    std::vector<ActionPlan> ep = runEpisode(h);
    CHECK(ep.size() == 1 && ep[0].action == Action::CaptureTop && has(ep[0], kLarvaLastSeenHere));
    CHECK(has(ep[0], kPassiveCandidateNew) && h.state.episodeLarvaVisible);
  }
  CHECK(h.state.larvaSeenStreak == 4);
  // the fourth trigger explores instead (BACK never seen, then FRONT; TOP recent)
  h.now += 10000; h.nextLarvaVisible = false; h.passive();
  std::vector<ActionPlan> explore = runEpisode(h);
  CHECK(!explore.empty() && explore[0].action == Action::CaptureBack && !has(explore[0], kLarvaLastSeenHere));
  for (const ActionPlan& p : explore) CHECK(p.action != Action::CaptureTop);
  CHECK(h.state.lastLarvaSeen.present && h.state.larvaSeenStreak == 1);
  // within 120 s of the last sighting (about 65 s): back to TOP; not seen there -> the episode
  // explores the views not checked within 30 s
  h.now += 40000; h.passive();
  std::vector<ActionPlan> back = runEpisode(h);
  CHECK(back.size() >= 2 && back[0].action == Action::CaptureTop && has(back[0], kLarvaLastSeenHere));
  CHECK(back[1].action != Action::CaptureTop && has(back[1], kEpisodeFollowUp));
  CHECK(!h.state.lastLarvaSeen.present && h.state.larvaSeenStreak == 0);
  h.now += 10000; h.passive();
  CHECK(!has(h.preview(), kLarvaLastSeenHere));  // lost: no stay
  // replay of the committed log gives the same state
  TemporalState replay;
  for (const CommittedEvent& e : h.log) reduce(replay, e);
  CHECK(replay.lastLarvaSeen.present == h.state.lastLarvaSeen.present && replay.larvaSeenStreak == h.state.larvaSeenStreak);

  // older than 120 s, another boot, rule off: explore as before
  auto sighted = [&](Harness& x) {
    x.caps = h.caps; x.bootEvent(kBootA); x.now = 1000; x.passive(); x.nextLarvaVisible = true; x.step();
    CHECK(x.state.lastLarvaPose == Pose::Front && x.state.larvaSeenStreak == 1);
  };
  Harness edge; sighted(edge);
  edge.now = 1000 + edge.config.larvaStayMs; edge.passive();
  ActionPlan atLimit = edge.preview();
  CHECK(has(atLimit, kLarvaLastSeenHere) && atLimit.action == Action::CaptureFront);  // exactly 120 s still counts
  Harness old; sighted(old);
  old.now = 1000 + old.config.larvaStayMs + 1; old.passive();
  CHECK(!has(old.preview(), kLarvaLastSeenHere));
  Harness reboot; sighted(reboot);
  reboot.bootEvent(kBootB); reboot.now = 5000; reboot.passive();
  CHECK(!has(reboot.preview(), kLarvaLastSeenHere));
  Harness off; off.config.stayWhereLarvaSeen = false; sighted(off);
  off.now += 20000; off.passive();
  ActionPlan o = off.preview();
  CHECK(o.action != Action::CaptureFront && !has(o, kLarvaLastSeenHere));
  // the safety gates still come first (self-actuation, cooldown)
  Harness gate; sighted(gate);
  gate.now += 20000; gate.passive(); gate.constraints.selfActuationRecent = true;
  ActionPlan g = gate.preview();
  CHECK(g.action == Action::Wait && has(g, kSelfActuationRecent) && !has(g, kLarvaLastSeenHere));
}

// 0.18.2 (mode hold): after an episode the arm waits at the view the next trigger would look at
// again; it parks once the sighting is older than 120 s, the streak limit is reached, or lost.
static void testHoldArmWhereLarvaWasSeen() {
  Harness h;
  h.caps.pose[0] = false; h.caps.pose[1] = true; h.caps.pose[2] = true; h.caps.pose[3] = true;
  h.config.holdWhereLarvaSeen = true;
  Pose at = Pose::Fixed;
  h.bootEvent(kBootA); h.now = 1000;
  CHECK(!holdArmAt(makeView(h.state, h.clock()), h.caps, h.config, at));  // nothing seen yet: park
  Harness t; t.caps = h.caps; t.config.holdWhereLarvaSeen = true;
  t.bootEvent(kBootA); t.now = 1000; t.passive();
  t.step(); t.now += t.config.cameraCooldownMs; t.nextLarvaVisible = true; t.step();  // FRONT no, TOP seen
  CHECK(holdArmAt(makeView(t.state, t.clock()), t.caps, t.config, at) && at == Pose::Top);
  t.now += t.config.larvaStayMs;  // still exactly 120 s after the sighting
  CHECK(holdArmAt(makeView(t.state, t.clock()), t.caps, t.config, at));
  t.now += 1;
  CHECK(!holdArmAt(makeView(t.state, t.clock()), t.caps, t.config, at));  // older: park
  // streak limit: after 3 stays seen again, the next trigger explores -> park
  Harness s; s.caps = h.caps; s.config.holdWhereLarvaSeen = true;
  s.bootEvent(kBootA); s.now = 1000; s.passive(); s.nextLarvaVisible = true; s.step();
  for (int i = 0; i < 3; ++i) { s.now += 10000; s.passive(); runEpisode(s); }
  CHECK(s.state.larvaSeenStreak == 4 && !holdArmAt(makeView(s.state, s.clock()), s.caps, s.config, at));
  // lost there -> park; follow mode (hold off) never holds
  Harness l; l.caps = h.caps; l.config.holdWhereLarvaSeen = true;
  l.bootEvent(kBootA); l.now = 1000; l.passive(); l.nextLarvaVisible = true; l.step();
  l.now += 20000; l.nextLarvaVisible = false; l.passive(); l.step();
  CHECK(!holdArmAt(makeView(l.state, l.clock()), l.caps, l.config, at));
  Harness f; f.caps = h.caps;
  f.bootEvent(kBootA); f.now = 1000; f.passive(); f.nextLarvaVisible = true; f.step();
  CHECK(!holdArmAt(makeView(f.state, f.clock()), f.caps, f.config, at));
}

int main(int argc, char** argv) {
  testReplayIsDeterministicAndIdempotent();
  testGapMakesStateIncomplete();
  testSameCandidateDifferentHistory();
  testCooldownLowLightAndUnknownLight();
  testDuplicateTriggerHasSingleDecision();
  testRecordedAndSyntheticNeverActuate();
  testFailuresAreNotAbsenceAndAreBounded();
  testIntentPersistedBeforeActionAndInterruptedAfterReboot();
  testStaleTriggerStorageAndSafety();
  testBurstIsCoalescedAndFinite();
  testUtcIsNeverRequired();
  testFixedOnlyEpisodeAddsNoDecision();
  testEpisodeVisitsUnseenViewsWithinBudget();
  testEpisodeStopsOnFailureAndRestartsOnNewTrigger();
  testEpisodeBudgetSurvivesReplay();
  testArmDefaultOrderStartsAtHomeFront();
  testSelfActuationGatesNewTriggersOnly();
  testLarvaVisibleStopsTheEpisode();
  testLookAgainWhereLarvaWasSeen();
  testHoldArmWhereLarvaWasSeen();
  for (unsigned bit = 0; bit < kReasonBits; ++bit) CHECK(reasonCode(bit) != nullptr);
  CHECK(reasonCode(kReasonBits) == nullptr);
  if (argc == 3 && std::string(argv[1]) == "--scenarios" && !writeScenarios(argv[2])) {
    std::printf("FAIL could not write %s\n", argv[2]);
    return 2;
  }
  std::printf("%s: %d checks, %d failures\n", failures ? "FAILED" : "OK", checks, failures);
  return failures ? 1 : 0;
}
