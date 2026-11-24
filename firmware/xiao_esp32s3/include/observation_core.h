#pragma once
// TemporalStateReducer, TemporalStateView and ObservationPolicy (pure core).
//
// No Arduino, I/O, clock reads or heap allocation: the firmware and the host
// tests (test/host/test_observation_core.cpp) compile this same header.
// The policy is an explainable rule set, not a trained model.
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace bll {

constexpr size_t kIdLen = 100;     // event_id incl. NUL; the store limits IDs to 99 chars
constexpr size_t kBootLen = 33;    // 32 hex + NUL
constexpr size_t kActionIdLen = 48;
constexpr size_t kUtcLen = 25;     // 2026-11-01T00:00:00.000Z + NUL
constexpr unsigned kPoseCount = 4;
// v2: per-trigger observation episode (follow-up views, capture budget).
// policy v3: FRONT-first view order (FRONT is the arm's home pose) and a new trigger
// arriving during/just after the device's own servo motion waits (SELF_ACTUATION_RECENT).
constexpr char kReducerVersion[] = "temporal-reducer-v3";   // v3 (0.18.0): last VISIBLE_LARVA view + streak
constexpr char kPolicyVersion[] = "observation-policy-v4";  // v4 (0.18.0): look again where the larva was seen

enum class Origin : uint8_t { Live, Recorded, Synthetic };
enum class EventType : uint8_t {
  Boot, DeviceDiagnostic, PassiveCandidate, TimeSync, PolicyDecision, ActionResult, Unknown
};
// FIXED is the single uncalibrated camera mount used until the servo exists.
enum class Pose : uint8_t { Fixed = 0, Front = 1, Top = 2, Back = 3 };
enum class Action : uint8_t { None, Wait, CaptureFixed, CaptureFront, CaptureTop, CaptureBack };
enum class ActionStatus : uint8_t { Completed, Failed, Interrupted };
enum class CaptureOutcome : uint8_t { NotAttempted, Success, Failed };
enum class Light : uint8_t { Unknown, Ok, Low };

inline void copyText(char* out, size_t size, const char* text) {
  if (!size) return;
  if (!text) text = "";
  size_t n = strlen(text);
  if (n >= size) n = size - 1;
  memcpy(out, text, n);
  out[n] = '\0';
}

inline const char* poseName(Pose pose) {
  switch (pose) {
    case Pose::Front: return "FRONT";
    case Pose::Top: return "TOP";
    case Pose::Back: return "BACK";
    default: return "FIXED";
  }
}

inline const char* actionName(Action action) {
  switch (action) {
    case Action::Wait: return "WAIT";
    case Action::CaptureFixed: return "CAPTURE_FIXED";
    case Action::CaptureFront: return "CAPTURE_FRONT";
    case Action::CaptureTop: return "CAPTURE_TOP";
    case Action::CaptureBack: return "CAPTURE_BACK";
    default: return "NONE";
  }
}

inline bool parseAction(const char* text, Action& out) {
  for (Action a : {Action::Wait, Action::CaptureFixed, Action::CaptureFront,
                   Action::CaptureTop, Action::CaptureBack})
    if (text && strcmp(text, actionName(a)) == 0) { out = a; return true; }
  return false;
}

inline bool parsePose(const char* text, Pose& out) {
  for (Pose p : {Pose::Fixed, Pose::Front, Pose::Top, Pose::Back})
    if (text && strcmp(text, poseName(p)) == 0) { out = p; return true; }
  return false;
}

inline Action captureFor(Pose pose) {
  switch (pose) {
    case Pose::Front: return Action::CaptureFront;
    case Pose::Top: return Action::CaptureTop;
    case Pose::Back: return Action::CaptureBack;
    default: return Action::CaptureFixed;
  }
}

inline bool isCapture(Action action) {
  return action == Action::CaptureFixed || action == Action::CaptureFront ||
         action == Action::CaptureTop || action == Action::CaptureBack;
}

inline Pose poseFor(Action action) {
  switch (action) {
    case Action::CaptureFront: return Pose::Front;
    case Action::CaptureTop: return Pose::Top;
    case Action::CaptureBack: return Pose::Back;
    default: return Pose::Fixed;
  }
}

// ---------------------------------------------------------------- events ---

// A durable event, already validated and committed by the EventStore.
// commitIndex is the 0-based line position in the replayed log.
struct CommittedEvent {
  uint32_t commitIndex = 0;
  EventType type = EventType::Unknown;
  Origin origin = Origin::Live;
  char eventId[kIdLen] = {};
  char bootId[kBootLen] = {};
  uint64_t monotonicMs = 0;
  bool hasUtc = false;
  char utc[kUtcLen] = {};
  // POLICY_DECISION
  char decisionId[kActionIdLen] = {};
  char actionId[kActionIdLen] = {};  // empty for WAIT
  Action action = Action::None;
  char triggerId[kIdLen] = {};       // caused_by_event_ids[0]
  // OBSERVATION_ACTION_RESULT (actionId shared)
  ActionStatus status = ActionStatus::Completed;
  Pose pose = Pose::Fixed;
  CaptureOutcome capture = CaptureOutcome::NotAttempted;
  bool larvaVisible = false;  // 0.17.0: result.vision.larva_visible (model output of the image)
};

struct EvidenceRef {
  bool present = false;
  char eventId[kIdLen] = {};
  char bootId[kBootLen] = {};
  uint64_t monotonicMs = 0;
  bool hasUtc = false;
  char utc[kUtcLen] = {};
  uint32_t commitIndex = 0;
};

inline EvidenceRef refOf(const CommittedEvent& e) {
  EvidenceRef r;
  r.present = true;
  copyText(r.eventId, sizeof(r.eventId), e.eventId);
  copyText(r.bootId, sizeof(r.bootId), e.bootId);
  r.monotonicMs = e.monotonicMs;
  r.hasUtc = e.hasUtc;
  copyText(r.utc, sizeof(r.utc), e.utc);
  r.commitIndex = e.commitIndex;
  return r;
}

// ----------------------------------------------------------------- state ---

struct OpenIntent {
  bool open = false;
  char actionId[kActionIdLen] = {};
  char decisionId[kActionIdLen] = {};
  Action action = Action::None;
  EvidenceRef decision;
  char triggerId[kIdLen] = {};
};

// Facts only. Freshness ("is it recent?") is computed by makeView() with a clock.
struct TemporalState {
  Origin streamOrigin = Origin::Live;
  uint32_t reducedThrough = 0;  // next commitIndex expected
  uint32_t gaps = 0;            // a missing commit makes the state incomplete
  uint32_t foreignOrigin = 0;   // events of another origin are never applied
  uint32_t unknownEvents = 0;
  uint32_t unmatchedResults = 0;
  EvidenceRef lastBoot, lastSync;
  EvidenceRef lastPassive;      // newest candidate of this stream's origin
  uint32_t passiveCount = 0;
  uint32_t coalescedTriggers = 0;  // candidates superseded before any decision
  char lastAnsweredTrigger[kIdLen] = {};
  EvidenceRef lastDecision;
  Action lastDecisionAction = Action::None;
  uint32_t decisions = 0, waits = 0;
  OpenIntent intent;
  EvidenceRef lastAttempt;                       // any pose, for camera cooldown
  EvidenceRef lastAttemptByPose[kPoseCount];
  EvidenceRef lastSuccessByPose[kPoseCount];     // image captured; NOT larva detection
  uint32_t consecutiveCaptureFailures = 0;
  uint32_t completed = 0, failed = 0, interrupted = 0;
  // Only verified vision-model events may fill these. None are produced yet.
  EvidenceRef lastVisualPresence, lastVisualMovement;
  // Observation episode = all decisions answering one trigger (caused_by_event_ids[0]).
  // Counts committed capture decisions, so the budget survives replay and reboot.
  char episodeTrigger[kIdLen] = {};
  uint32_t episodeCaptures = 0;
  bool episodeLastCaptureOk = false;  // newest result of this episode was an image
  bool episodeLarvaVisible = false;   // 0.17.0: an image of this episode was classed VISIBLE_LARVA
  // 0.18.0: the view whose newest image the Vision model classed VISIBLE_LARVA (model output,
  // not a verified detection) and how many images of that view in a row did. Cleared when an
  // image of that view no longer does; the streak restarts at 1 after an image of another view.
  EvidenceRef lastLarvaSeen;
  Pose lastLarvaPose = Pose::Fixed;
  uint32_t larvaSeenStreak = 0;
};

enum class ApplyResult : uint8_t { Applied, AlreadyApplied, Gap, ForeignOrigin };

inline bool pendingTrigger(const TemporalState& s) {
  return s.lastPassive.present && strcmp(s.lastPassive.eventId, s.lastAnsweredTrigger) != 0;
}

// S_next = reduce(S_prev, committed_event). Replaying the same committed log in
// the same order always yields the same state.
inline ApplyResult reduce(TemporalState& s, const CommittedEvent& e) {
  if (e.commitIndex < s.reducedThrough) return ApplyResult::AlreadyApplied;
  if (e.commitIndex > s.reducedThrough) { ++s.gaps; return ApplyResult::Gap; }
  ++s.reducedThrough;
  if (e.origin != s.streamOrigin) { ++s.foreignOrigin; return ApplyResult::ForeignOrigin; }
  const EvidenceRef ref = refOf(e);
  switch (e.type) {
    case EventType::Boot:
      s.lastBoot = ref;
      // A reboot is the operator's "repaired, try again" step (e.g. a reseated camera
      // cable). Without it the stop below never clears: while stopped no capture runs,
      // so no success can reset the count (2026-11-13). Failures still stop again after
      // maxConsecutiveFailures, so an unrepaired camera costs at most that many tries per boot.
      s.consecutiveCaptureFailures = 0;
      break;
    case EventType::TimeSync:
      s.lastSync = ref;
      break;
    case EventType::DeviceDiagnostic:
      break;
    case EventType::PassiveCandidate:
      if (pendingTrigger(s)) ++s.coalescedTriggers;
      s.lastPassive = ref;
      ++s.passiveCount;
      break;
    case EventType::PolicyDecision:
      s.lastDecision = ref;
      s.lastDecisionAction = e.action;
      ++s.decisions;
      if (e.action == Action::Wait) ++s.waits;
      copyText(s.lastAnsweredTrigger, sizeof(s.lastAnsweredTrigger), e.triggerId);
      if (strcmp(s.episodeTrigger, e.triggerId) != 0) {
        copyText(s.episodeTrigger, sizeof(s.episodeTrigger), e.triggerId);
        s.episodeCaptures = 0;
        s.episodeLastCaptureOk = false;
        s.episodeLarvaVisible = false;
      }
      if (isCapture(e.action)) {
        ++s.episodeCaptures;
        s.episodeLastCaptureOk = false;
        s.intent = OpenIntent();
        s.intent.open = true;
        copyText(s.intent.actionId, sizeof(s.intent.actionId), e.actionId);
        copyText(s.intent.decisionId, sizeof(s.intent.decisionId), e.decisionId);
        s.intent.action = e.action;
        s.intent.decision = ref;
        copyText(s.intent.triggerId, sizeof(s.intent.triggerId), e.triggerId);
      }
      break;
    case EventType::ActionResult: {
      if (!s.intent.open || strcmp(s.intent.actionId, e.actionId) != 0) {
        ++s.unmatchedResults;
        break;
      }
      s.intent = OpenIntent();
      s.episodeLastCaptureOk = e.status == ActionStatus::Completed && e.capture == CaptureOutcome::Success;
      if (s.episodeLastCaptureOk && e.larvaVisible) s.episodeLarvaVisible = true;
      if (e.status == ActionStatus::Interrupted) { ++s.interrupted; break; }
      const unsigned p = static_cast<unsigned>(e.pose) % kPoseCount;
      s.lastAttempt = ref;
      s.lastAttemptByPose[p] = ref;
      if (e.capture == CaptureOutcome::Success) {
        s.lastSuccessByPose[p] = ref;
        s.consecutiveCaptureFailures = 0;
        const bool sameView = s.lastLarvaSeen.present && s.lastLarvaPose == e.pose;
        if (e.larvaVisible) {
          s.larvaSeenStreak = sameView ? s.larvaSeenStreak + 1 : 1;
          s.lastLarvaSeen = ref;
          s.lastLarvaPose = e.pose;
        } else if (sameView) {
          s.lastLarvaSeen = EvidenceRef();  // no longer seen there: explore again
          s.larvaSeenStreak = 0;
        } else if (s.lastLarvaSeen.present) {
          // Another view was checked (e.g. after maxLarvaStays): the next trigger may return
          // there for another run of stays, counted from the sighting again.
          s.larvaSeenStreak = 1;
        }
      } else {
        ++s.consecutiveCaptureFailures;
      }
      if (e.status == ActionStatus::Completed) ++s.completed; else ++s.failed;
      break;
    }
    default:
      ++s.unknownEvents;
      break;
  }
  return ApplyResult::Applied;
}

// ------------------------------------------------------------------ view ---

struct ClockContext {
  char bootId[kBootLen] = {};
  uint64_t nowMs = 0;  // monotonic within bootId; never UTC
};

// Elapsed time is only known within the same boot. Other boots are "not comparable".
struct AgeInfo {
  bool present = false;
  bool comparable = false;
  uint64_t ageMs = 0;
};

inline AgeInfo ageOf(const EvidenceRef& r, const ClockContext& c) {
  AgeInfo a;
  if (!r.present) return a;
  a.present = true;
  if (strcmp(r.bootId, c.bootId) != 0 || c.nowMs < r.monotonicMs) return a;
  a.comparable = true;
  a.ageMs = c.nowMs - r.monotonicMs;
  return a;
}

struct TemporalStateView {
  bool complete = true;
  bool pending = false;
  // The newest trigger is answered, its last capture produced an image and no
  // intent is open: the episode may continue with another view (budget permitting).
  bool followUp = false;
  bool episodeLarvaVisible = false;  // copied from the state (0.17.0)
  AgeInfo larvaSeen;                 // 0.18.0: age of the newest VISIBLE_LARVA image (model output)
  Pose larvaPose = Pose::Fixed;
  uint32_t larvaSeenStreak = 0;
  AgeInfo trigger, lastAttempt;
  AgeInfo successByPose[kPoseCount], attemptByPose[kPoseCount];
  AgeInfo visualMovement;
};

inline TemporalStateView makeView(const TemporalState& s, const ClockContext& c) {
  TemporalStateView v;
  v.complete = s.gaps == 0;
  v.pending = pendingTrigger(s);
  v.followUp = !v.pending && s.lastPassive.present && !s.intent.open && s.episodeCaptures > 0 &&
               s.episodeLastCaptureOk && strcmp(s.episodeTrigger, s.lastPassive.eventId) == 0;
  v.episodeLarvaVisible = s.episodeLarvaVisible;
  v.larvaSeen = ageOf(s.lastLarvaSeen, c);
  v.larvaPose = s.lastLarvaPose;
  v.larvaSeenStreak = s.larvaSeenStreak;
  v.trigger = ageOf(s.lastPassive, c);
  v.lastAttempt = ageOf(s.lastAttempt, c);
  for (unsigned i = 0; i < kPoseCount; ++i) {
    v.successByPose[i] = ageOf(s.lastSuccessByPose[i], c);
    v.attemptByPose[i] = ageOf(s.lastAttemptByPose[i], c);
  }
  v.visualMovement = ageOf(s.lastVisualMovement, c);
  return v;
}

// ---------------------------------------------------------------- policy ---

enum Reason : uint32_t {
  kPassiveCandidateNew      = 1u << 0,
  kStateIncomplete          = 1u << 1,
  kRecordedSourceNoActuation= 1u << 2,
  kStorageNotReady          = 1u << 3,
  kSafetyBlocked            = 1u << 4,
  kActionInProgress         = 1u << 5,
  kDeviceBusy               = 1u << 6,
  kTriggerFromPreviousBoot  = 1u << 7,
  kTriggerStale             = 1u << 8,
  kLowLight                 = 1u << 9,
  kLightUnknown             = 1u << 10,
  kCameraCooldownActive     = 1u << 11,
  kVisionRepeatedlyFailed   = 1u << 12,
  kNoCalibratedView         = 1u << 13,
  kAllViewsRecentlyChecked  = 1u << 14,
  kHistoryTimeUncomparable  = 1u << 15,
  kPreviousAttemptFailed    = 1u << 16,
  kRecentlyChecked0         = 1u << 17,  // + pose index: FIXED, FRONT, TOP, BACK
  kNotRecentlyObserved0     = 1u << 21,  // + pose index
  kBaselineEveryCandidate   = 1u << 25,
  kModelUnverified          = 1u << 26,
  kEpisodeFollowUp          = 1u << 27,  // same trigger, another view after a captured image
  kEpisodeBudgetExhausted   = 1u << 28,  // per-trigger capture budget used up
  kSelfActuationRecent      = 1u << 29,  // trigger may be the device's own servo vibration
  kLarvaVisibleStop         = 1u << 30,  // 0.17.0: the image AI saw the larva; no further views
  kLarvaLastSeenHere        = 1u << 31,  // 0.18.0: look again where the image AI last saw the larva
};
constexpr unsigned kReasonBits = 32;

inline uint32_t recentlyChecked(Pose p) { return kRecentlyChecked0 << static_cast<unsigned>(p); }
inline uint32_t notRecentlyObserved(Pose p) { return kNotRecentlyObserved0 << static_cast<unsigned>(p); }

// Stable codes for logs, tests and the dashboard. Order = bit position.
inline const char* reasonCode(unsigned bit) {
  static const char* const names[] = {
    "PASSIVE_CANDIDATE_NEW", "STATE_INCOMPLETE", "RECORDED_SOURCE_NO_ACTUATION",
    "STORAGE_NOT_READY", "SAFETY_BLOCKED", "ACTION_IN_PROGRESS", "DEVICE_BUSY",
    "TRIGGER_FROM_PREVIOUS_BOOT", "TRIGGER_STALE", "LOW_LIGHT", "LIGHT_UNKNOWN",
    "CAMERA_COOLDOWN_ACTIVE", "VISION_REPEATEDLY_FAILED", "NO_CALIBRATED_VIEW",
    "ALL_VIEWS_RECENTLY_CHECKED", "HISTORY_TIME_UNCOMPARABLE", "PREVIOUS_ATTEMPT_FAILED",
    "FIXED_RECENTLY_CHECKED", "FRONT_RECENTLY_CHECKED", "TOP_RECENTLY_CHECKED", "BACK_RECENTLY_CHECKED",
    "FIXED_NOT_RECENTLY_OBSERVED", "FRONT_NOT_RECENTLY_OBSERVED", "TOP_NOT_RECENTLY_OBSERVED",
    "BACK_NOT_RECENTLY_OBSERVED", "BASELINE_EVERY_CANDIDATE", "MODEL_UNVERIFIED",
    "EPISODE_FOLLOW_UP", "EPISODE_BUDGET_EXHAUSTED", "SELF_ACTUATION_RECENT",
    "LARVA_VISIBLE_STOP", "LARVA_LAST_SEEN_HERE",
  };
  return bit < sizeof(names) / sizeof(names[0]) ? names[bit] : nullptr;
}

struct PolicyCapabilities {
  bool pose[kPoseCount] = {false, false, false, false};  // physically present AND calibrated
};

struct PolicyConstraints {
  bool liveMode = true;         // stream may actuate (never true for RECORDED/SYNTHETIC)
  bool storageWritable = false; // a decision that cannot be persisted is never acted on
  bool safetyOk = true;
  bool deviceBusy = false;      // ADC / Vision / servo owned by another task
  // The servo moved (or settled) within the guard window before this trigger arrived, so
  // the candidate may be mechanical self-noise. Only gates NEW triggers, never follow-ups
  // (an episode's own moves would otherwise block it).
  bool selfActuationRecent = false;
  Light light = Light::Unknown;
};

struct PolicyConfig {
  uint32_t viewRecencyMs = 30000;   // a view seen this recently adds little new information
  uint32_t cameraCooldownMs = 5000; // device protection between captures
  uint32_t triggerMaxAgeMs = 60000; // never act on an old candidate
  uint32_t maxConsecutiveFailures = 2;
  uint32_t maxCapturesPerEpisode = 3;  // captures answering one trigger, incl. the first
  // 0.17.0 (user decision 2026-11-23): once an image of the episode is classed VISIBLE_LARVA
  // (score >= 80, see config.h), the remaining views are skipped. false = visit all views.
  bool stopWhenLarvaVisible = true;
  // 0.18.0 (user decision 2026-11-24): a new trigger first looks again at the view where the
  // image AI saw the larva within larvaStayMs, at most maxLarvaStays times in a row; if the larva
  // is not seen there, the episode explores the other views as before. false = always explore.
  bool stayWhereLarvaSeen = true;
  uint32_t larvaStayMs = 120000;
  uint32_t maxLarvaStays = 3;
  // 0.18.2 (user decision 2026-11-24, mode "hold"): after an episode the arm stays at the view the
  // next trigger would look at again (holdArmAt) instead of parking at FRONT; it parks once that no
  // longer applies (larvaStayMs passed, the streak limit reached, or the larva was lost there).
  bool holdWhereLarvaSeen = false;
  // FRONT is the arm's home pose (0 deg), TOP 90 deg, BACK 180 deg: this order visits the
  // views with the least arm travel. FIXED is the legacy fixed mount (same field as FRONT).
  Pose order[kPoseCount] = {Pose::Front, Pose::Top, Pose::Back, Pose::Fixed};
};

struct ActionPlan {
  bool decide = false;       // false: nothing to persist (no trigger / cannot persist)
  Action action = Action::None;
  Pose pose = Pose::Fixed;
  uint32_t reasons = 0;
  char triggerId[kIdLen] = {};
};

inline ActionPlan waitPlan(ActionPlan plan, uint32_t reason) {
  plan.action = Action::Wait;
  plan.reasons |= reason;
  return plan;
}

enum class ViewChoice : uint8_t { Chosen, NoneAvailable, AllRecent };

// Prefer a view never seen this boot, then the one seen longest ago; ties keep
// cfg.order. Always adds explanatory reasons; on Chosen also sets action/pose.
inline ViewChoice chooseView(const TemporalStateView& v, const PolicyCapabilities& caps,
                             const PolicyConfig& cfg, ActionPlan& plan) {
  bool any = false, found = false, foundNever = false;
  Pose target = Pose::Fixed;
  uint64_t targetAge = 0;
  for (Pose p : cfg.order) {
    const unsigned i = static_cast<unsigned>(p);
    if (i >= kPoseCount || !caps.pose[i]) continue;
    any = true;
    const AgeInfo& seen = v.successByPose[i];
    if (seen.present && !seen.comparable) plan.reasons |= kHistoryTimeUncomparable;
    const bool recent = seen.comparable && seen.ageMs < cfg.viewRecencyMs;
    if (recent) { plan.reasons |= recentlyChecked(p); continue; }
    const bool never = !seen.comparable;
    if (!found || (never && !foundNever) || (!never && !foundNever && seen.ageMs > targetAge)) {
      found = true; foundNever = never; target = p; targetAge = seen.ageMs;
    }
  }
  if (!any) return ViewChoice::NoneAvailable;
  if (!found) return ViewChoice::AllRecent;
  const unsigned t = static_cast<unsigned>(target);
  if (v.attemptByPose[t].comparable &&
      (!v.successByPose[t].comparable || v.attemptByPose[t].ageMs < v.successByPose[t].ageMs))
    plan.reasons |= kPreviousAttemptFailed;
  plan.action = captureFor(target);
  plan.pose = target;
  plan.reasons |= notRecentlyObserved(target) | kModelUnverified;
  return ViewChoice::Chosen;
}

// 0.18.0: the first view of a new trigger is the one where the image AI last saw the larva,
// if that was within cfg.larvaStayMs (same boot) and fewer than maxLarvaStays looks in a row
// have already found it there. The streak counts the sighting that started it, so
// larvaSeenStreak <= maxLarvaStays allows maxLarvaStays stays. Ignores view recency on purpose.
inline bool chooseLarvaView(const TemporalStateView& v, const PolicyCapabilities& caps,
                            const PolicyConfig& cfg, ActionPlan& plan) {
  const unsigned p = static_cast<unsigned>(v.larvaPose);
  if (!cfg.stayWhereLarvaSeen || !v.larvaSeen.comparable || v.larvaSeen.ageMs > cfg.larvaStayMs ||
      v.larvaSeenStreak == 0 || v.larvaSeenStreak > cfg.maxLarvaStays || p >= kPoseCount || !caps.pose[p])
    return false;
  plan.action = captureFor(v.larvaPose);
  plan.pose = v.larvaPose;
  plan.reasons |= kLarvaLastSeenHere | kModelUnverified;
  return true;
}

// 0.18.2: where the arm should wait after an episode instead of parking at FRONT (mode "hold"):
// exactly the view chooseLarvaView would pick for the next trigger. false = park as before.
inline bool holdArmAt(const TemporalStateView& v, const PolicyCapabilities& caps,
                      const PolicyConfig& cfg, Pose& pose) {
  if (!cfg.holdWhereLarvaSeen) return false;
  ActionPlan probe;
  if (!chooseLarvaView(v, caps, cfg, probe)) return false;
  pose = probe.pose;
  return true;
}

// Continue an episode with another view. Silent (decide=false) unless a capture is
// chosen, so a single FIXED camera never logs extra decisions; the preview reasons
// still say why the episode stopped. Safety order matches decide().
inline ActionPlan decideFollowUp(const TemporalState& s, const TemporalStateView& v,
                                 const PolicyCapabilities& caps, const PolicyConstraints& k,
                                 const PolicyConfig& cfg) {
  ActionPlan plan;
  plan.reasons = kEpisodeFollowUp;
  if (!k.storageWritable) { plan.reasons |= kStorageNotReady; return plan; }
  if (!v.complete) { plan.reasons |= kStateIncomplete; return plan; }
  if (!k.liveMode) { plan.reasons |= kRecordedSourceNoActuation; return plan; }
  if (!k.safetyOk) { plan.reasons |= kSafetyBlocked; return plan; }
  if (k.deviceBusy) { plan.reasons |= kDeviceBusy; return plan; }
  if (s.episodeCaptures >= cfg.maxCapturesPerEpisode) { plan.reasons |= kEpisodeBudgetExhausted; return plan; }
  if (cfg.stopWhenLarvaVisible && v.episodeLarvaVisible) { plan.reasons |= kLarvaVisibleStop; return plan; }
  if (!v.trigger.comparable) { plan.reasons |= kTriggerFromPreviousBoot; return plan; }
  if (v.trigger.ageMs > cfg.triggerMaxAgeMs) { plan.reasons |= kTriggerStale; return plan; }
  if (k.light == Light::Low) { plan.reasons |= kLowLight; return plan; }
  if (k.light == Light::Unknown) plan.reasons |= kLightUnknown;
  if (s.consecutiveCaptureFailures >= cfg.maxConsecutiveFailures) { plan.reasons |= kVisionRepeatedlyFailed; return plan; }
  // The caller retries after the cooldown; nothing is persisted meanwhile.
  if (v.lastAttempt.comparable && v.lastAttempt.ageMs < cfg.cameraCooldownMs) {
    plan.reasons |= kCameraCooldownActive; return plan;
  }
  ActionPlan chosen = plan;
  const ViewChoice choice = chooseView(v, caps, cfg, chosen);
  if (choice == ViewChoice::NoneAvailable) { plan.reasons |= kNoCalibratedView; return plan; }
  if (choice == ViewChoice::AllRecent) { chosen.reasons |= kAllViewsRecentlyChecked; return chosen; }
  chosen.decide = true;
  copyText(chosen.triggerId, sizeof(chosen.triggerId), s.lastPassive.eventId);
  return chosen;
}

// Deterministic: the same state, view, capabilities, constraints and config
// always return the same plan. Rule order is the safety order of the design.
inline ActionPlan decide(const TemporalState& s, const TemporalStateView& v,
                         const PolicyCapabilities& caps, const PolicyConstraints& k,
                         const PolicyConfig& cfg) {
  ActionPlan plan;
  if (!v.pending) return v.followUp ? decideFollowUp(s, v, caps, k, cfg) : plan;
  if (!k.storageWritable) { plan.reasons = kStorageNotReady; return plan; }
  plan.decide = true;
  copyText(plan.triggerId, sizeof(plan.triggerId), s.lastPassive.eventId);
  plan.reasons = kPassiveCandidateNew;
  if (!v.complete) return waitPlan(plan, kStateIncomplete);
  if (!k.liveMode) return waitPlan(plan, kRecordedSourceNoActuation);
  if (!k.safetyOk) return waitPlan(plan, kSafetyBlocked);
  if (s.intent.open) return waitPlan(plan, kActionInProgress);
  if (k.deviceBusy) return waitPlan(plan, kDeviceBusy);
  if (k.selfActuationRecent) return waitPlan(plan, kSelfActuationRecent);
  if (!v.trigger.comparable) return waitPlan(plan, kTriggerFromPreviousBoot);
  if (v.trigger.ageMs > cfg.triggerMaxAgeMs) return waitPlan(plan, kTriggerStale);
  if (k.light == Light::Low) return waitPlan(plan, kLowLight);
  if (k.light == Light::Unknown) plan.reasons |= kLightUnknown;
  if (s.consecutiveCaptureFailures >= cfg.maxConsecutiveFailures)
    return waitPlan(plan, kVisionRepeatedlyFailed);
  if (v.lastAttempt.comparable && v.lastAttempt.ageMs < cfg.cameraCooldownMs)
    return waitPlan(plan, kCameraCooldownActive);

  if (chooseLarvaView(v, caps, cfg, plan)) return plan;
  const ViewChoice choice = chooseView(v, caps, cfg, plan);
  if (choice == ViewChoice::NoneAvailable) return waitPlan(plan, kNoCalibratedView);
  if (choice == ViewChoice::AllRecent) return waitPlan(plan, kAllViewsRecentlyChecked);
  return plan;
}

// A follow-up held only by the camera cooldown is silent (nothing persisted), so the
// caller must run decide() again later. Returns how long to wait; 0 = no timed recheck
// is needed (no follow-up, or the cooldown is already over). Does not change decisions.
inline uint64_t followUpRecheckInMs(const TemporalStateView& v, const PolicyConfig& cfg) {
  if (!v.followUp || !v.lastAttempt.comparable || v.lastAttempt.ageMs >= cfg.cameraCooldownMs) return 0;
  if (cfg.stopWhenLarvaVisible && v.episodeLarvaVisible) return 0;  // episode over (0.17.0)
  return cfg.cameraCooldownMs - v.lastAttempt.ageMs;
}

// Baseline B: react to every candidate with the same camera, no memory, no checks.
inline ActionPlan decideBaselineB(const TemporalState& s, const TemporalStateView& v, Pose fixed) {
  ActionPlan plan;
  if (!v.pending) return plan;
  plan.decide = true;
  copyText(plan.triggerId, sizeof(plan.triggerId), s.lastPassive.eventId);
  plan.action = captureFor(fixed);
  plan.pose = fixed;
  plan.reasons = kPassiveCandidateNew | kBaselineEveryCandidate;
  return plan;
}

// Baseline A: fixed-period capture, independent of any evidence.
inline bool baselineAPeriodicDue(uint64_t nowMs, bool capturedBefore, uint64_t lastCaptureMs,
                                 uint32_t periodMs) {
  return !capturedBefore || (nowMs >= lastCaptureMs && nowMs - lastCaptureMs >= periodMs);
}

}  // namespace bll
