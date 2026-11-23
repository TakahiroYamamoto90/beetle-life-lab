#pragma once
#include <Arduino.h>
#include "observation_core.h"

void evidenceBegin();
void evidenceStatus();
void evidenceReadPage(uint32_t afterIndex);
bool evidenceDiagnostic();
void evidenceInitEmpty();
void evidencePassive(char* payload, bool validateOnly);
void evidenceClockProbe(const char* nonce);
void evidenceClockSync(char* payload);
// "evidence-rotate <events> <sha256>" (0.14.6): empties the log only when the PC proves an
// identical saved copy (same event count and SHA-256 of every committed line plus a newline),
// then restarts.
void evidenceRotate(char* payload);

// --- Observation loop access (state is the replayed projection of committed events).
struct ObservationDecisionRecord {
  const bll::ActionPlan* plan;
  const char* decisionId;
  const char* actionId;  // used only for capture actions
  const bll::PolicyConstraints* constraints;
  const bll::PolicyCapabilities* capabilities;
  const bll::PolicyConfig* config;
  const char* lightName;
  const char* lightSource;
};

struct ObservationResultRecord {
  const char* actionId;
  const char* decisionEventId;
  bll::ActionStatus status;
  bll::Action action;
  bll::Pose pose;
  bll::CaptureOutcome capture;
  const char* captureReason;
  uint32_t durationMs;
  const char* payloadSha256;  // decoded image payload sent over USB, when captured
  uint32_t payloadBytes;
  // 0.17.0: model output of the captured image (-1 = none). No default initializers: the
  // record is brace-initialized (aggregate) and the toolchain builds as C++11.
  int16_t visionTopClass;
  int16_t visionTopScore;
  bool larvaVisible;             // top class VISIBLE_LARVA with score >= kVisionLarvaMinScore
};

const bll::TemporalState& evidenceTemporalState();
bool evidenceStorageReady();
const char* evidenceBootId();
uint64_t evidenceMonotonicMs();
// Both return true only after append + readback; eventId receives the new event ID.
bool evidenceAppendDecision(const ObservationDecisionRecord& record, char* eventId, size_t size);
bool evidenceAppendActionResult(const ObservationResultRecord& record, char* eventId, size_t size);
