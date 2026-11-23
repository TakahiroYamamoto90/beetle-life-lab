#include "evidence_store.h"
#include "config.h"
#include "passive_message.h"
#include "evidence_clock.h"
#include "observation_core.h"
#include "observation_runtime.h"
#ifdef BLL_EVIDENCE_STORE
#include <ArduinoJson.h>
#include <LittleFS.h>
#include <esp_partition.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <mbedtls/sha256.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <cstdlib>
#include <cstring>

namespace {
constexpr size_t kMaxLine = 2048;
constexpr size_t kSegmentBytes = 65536;
constexpr size_t kReserve = 262144;
// 16 until 0.14.4. 20 x 64 KiB fills about the space kReserve leaves in the 1.5 MiB
// partition (2026-11-13). Older firmware reads only segments 1-16: one-way change.
constexpr unsigned kSegments = 20;
constexpr unsigned kPageSize = 16;
const char* kDirectory = "/evidence-live";
char deviceId[32], bootId[33];
char buffer[kMaxLine + 1];
// Only the Arduino loop task calls this store, synchronously. No document or
// JsonVariant may outlive a call. Clear before reuse; do not add reentrant calls.
StaticJsonDocument<4096> workDocument;
uint32_t seq = 0, events = 0, boots = 0, diagnostics = 0;
uint32_t restored = 0, damaged = 0, writeFailures = 0;
uint32_t passiveCandidates = 0;
uint32_t timeSyncEvents = 0;
uint32_t decisionEvents = 0, actionResultEvents = 0;
// Pure TemporalStateReducer projection of the committed log (replayed at boot).
bll::TemporalState temporal;
bll::CommittedEvent scratchEvent;  // static: keeps ~0.4 KiB off the loop stack
EvidenceClock activeClock;
char probeNonce[33] = {};
uint64_t probeMonoMs = 0;
bool probeValid = false;
// Set only by evidenceRotate() for the one DEVICE_DIAGNOSTIC that opens the emptied log.
struct RotationNote { bool valid = false; uint32_t events = 0; uint32_t bytes = 0; char sha256[65] = {}; };
RotationNote rotationNote;

uint64_t monotonicMs() { return static_cast<uint64_t>(esp_timer_get_time() / 1000); }
bool clockFresh(uint64_t now) {
  return activeClock.valid && now >= activeClock.monoMs && now - activeClock.monoMs <= kClockMaxAgeMs;
}
unsigned segment = 1;
bool mounted = false, writable = false, readbackOk = false;
const char* storageReason = "NOT_STARTED";

void pathFor(unsigned n, char* path, size_t length) {
  snprintf(path, length, "%s/log-%04u.jsonl", kDirectory, n);
}

// Block reads: one File::read() per byte made every full-log scan cost seconds.
// One reader at a time (loop task only); openLines() resets it for each file.
uint8_t readBlock[512];
size_t readLength = 0, readPosition = 0;
File openLines(const char* path) {
  readLength = readPosition = 0;
  return LittleFS.open(path, FILE_READ);
}

// A line only becomes evidence after its newline. Keep partial/invalid bytes intact.
enum class Line { End, Complete, Invalid };
Line readLine(File& file) {
  size_t used = 0;
  while (true) {
    if (readPosition == readLength) {
      readPosition = 0;
      readLength = file.read(readBlock, sizeof(readBlock));
      if (readLength == 0) {
        // A read that stops before the file end is an error, not the end of the log.
        if (file.position() < file.size()) return Line::Invalid;
        return used ? Line::Invalid : Line::End;
      }
    }
    const char c = static_cast<char>(readBlock[readPosition++]);
    if (c == '\n') { buffer[used] = 0; return used ? Line::Complete : Line::Invalid; }
    if (used >= kMaxLine) return Line::Invalid;
    buffer[used++] = c;
  }
}

// Every committed PASSIVE_VIBRATION_CANDIDATE as (run id, candidate seq), rebuilt by the
// boot replay and extended on each commit. A candidate missing here is new without
// re-reading the log; a hit (a retry) still takes the full scan for the
// duplicate/conflict answer. If the index cannot be kept, every candidate is scanned.
struct CandidateKey { uint8_t run[16]; uint32_t candidate; };
CandidateKey* candidateIndex = nullptr;
size_t candidateCount = 0, candidateCapacity = 0;
bool candidateIndexComplete = true;

bool runBytes(const char* hex, uint8_t* out) {
  if (!hex || strlen(hex) != 32) return false;
  for (size_t i = 0; i < 16; ++i) {
    unsigned value = 0;
    for (size_t j = 0; j < 2; ++j) {
      const char c = hex[2 * i + j];
      value <<= 4;
      if (c >= '0' && c <= '9') value |= c - '0';
      else if (c >= 'a' && c <= 'f') value |= c - 'a' + 10;
      else return false;
    }
    out[i] = static_cast<uint8_t>(value);
  }
  return true;
}

void indexCandidate(const char* run, uint32_t candidate) {
  if (!candidateIndexComplete) return;
  CandidateKey key;
  if (!runBytes(run, key.run)) { candidateIndexComplete = false; return; }
  key.candidate = candidate;
  if (candidateCount == candidateCapacity) {
    const size_t grown = candidateCapacity ? candidateCapacity * 2 : 64;
    auto* moved = static_cast<CandidateKey*>(realloc(candidateIndex, grown * sizeof(CandidateKey)));
    if (!moved) { candidateIndexComplete = false; return; }
    candidateIndex = moved; candidateCapacity = grown;
  }
  candidateIndex[candidateCount++] = key;
}

// true: the candidate is certainly not in the log (the index is complete and has no entry).
bool candidateCertainlyNew(const char* run, uint32_t candidate) {
  uint8_t bytes[16];
  if (!candidateIndexComplete || !runBytes(run, bytes)) return false;
  for (size_t i = 0; i < candidateCount; ++i)
    if (candidateIndex[i].candidate == candidate && memcmp(candidateIndex[i].run, bytes, 16) == 0) return false;
  return true;
}

bool boundedText(JsonVariantConst value, size_t maximum) {
  if (!value.is<const char*>()) return false;
  const size_t length = strlen(value.as<const char*>());
  return length > 0 && length <= maximum;
}

// POLICY_DECISION: a rule-based decision, persisted before any physical action.
bool validDecision(const JsonDocument& doc, JsonObjectConst result) {
  bll::Action action;
  if (strcmp(doc["source"] | "", "XIAO") || strcmp(result["scope"] | "", "OBSERVATION_POLICY") ||
      !boundedText(result["policy_version"], 40) || !boundedText(result["reducer_version"], 40) ||
      !boundedText(result["decision_id"], bll::kActionIdLen - 1) ||
      !bll::parseAction(result["action"] | "", action)) return false;
  const auto causes = result["caused_by_event_ids"].as<JsonArrayConst>();
  const auto reasons = result["reason_codes"].as<JsonArrayConst>();
  if (causes.isNull() || causes.size() != 1 || !boundedText(causes[0], bll::kIdLen - 1) ||
      reasons.isNull() || reasons.size() == 0 || reasons.size() > 16) return false;
  for (JsonVariantConst reason : reasons) if (!boundedText(reason, 40)) return false;
  if (action == bll::Action::Wait)
    return result["action_id"].isNull() && result["target_pose"].isNull() &&
           result.containsKey("action_id") && doc["observation_id"].isNull();
  bll::Pose pose;
  return boundedText(result["action_id"], bll::kActionIdLen - 1) &&
         strcmp(doc["observation_id"] | "", result["action_id"] | "") == 0 &&
         bll::parsePose(result["target_pose"] | "", pose) && pose == bll::poseFor(action);
}

// OBSERVATION_ACTION_RESULT: outcome of an intent. A capture is never a larva detection.
bool validActionResult(const JsonDocument& doc, JsonObjectConst result) {
  bll::Action action;
  bll::Pose pose;
  const char* status = result["status"] | "";
  const auto capture = result["capture"].as<JsonObjectConst>();
  const char* captured = capture["status"] | "";
  if (strcmp(doc["source"] | "", "XIAO") || strcmp(result["scope"] | "", "OBSERVATION_ACTION") ||
      !boundedText(result["action_id"], bll::kActionIdLen - 1) ||
      strcmp(doc["observation_id"] | "", result["action_id"] | "") != 0 ||
      !boundedText(result["decision_event_id"], bll::kIdLen - 1) ||
      !bll::parseAction(result["action"] | "", action) || !bll::isCapture(action) ||
      !bll::parsePose(result["pose"] | "", pose) || pose != bll::poseFor(action) ||
      capture.isNull() || strcmp(capture["presence"] | "", "UNKNOWN") ||
      strcmp(capture["movement"] | "", "UNKNOWN") || !boundedText(capture["reason"], 40))
    return false;
  const bool success = strcmp(captured, "SUCCESS") == 0;
  bool consistent = false;
  if (strcmp(status, "COMPLETED") == 0) consistent = success;
  else if (strcmp(status, "FAILED") == 0)
    consistent = strcmp(captured, "FAILED") == 0 || strcmp(captured, "NOT_ATTEMPTED") == 0;
  else if (strcmp(status, "INTERRUPTED") == 0) consistent = strcmp(captured, "UNKNOWN") == 0;
  if (!consistent) return false;
  // 0.17.0: optional model output of the captured image (older firmware ignores it).
  const auto vision = result["vision"];
  if (!vision.isNull()) {
    if (!success || !vision["top_class_id"].is<int>() || !vision["score"].is<int>() ||
        !vision["larva_visible"].is<bool>() || vision["top_class_id"].as<int>() < 0 ||
        vision["top_class_id"].as<int>() > 255 || vision["score"].as<int>() < 0 ||
        vision["score"].as<int>() > 100) return false;
    const bool visible = vision["top_class_id"].as<int>() == config::kVisionLarvaClassId &&
                         vision["score"].as<int>() >= config::kVisionLarvaMinScore;
    if (vision["larva_visible"].as<bool>() != visible) return false;
  }
  const auto image = doc["image_ref"];
  if (!success) return image.isNull();
  return strcmp(image["store"] | "", "HOST_USB_STREAM") == 0 &&
         strcmp(image["status"] | "", "SENT_TO_HOST_UNCONFIRMED") == 0 &&
         passiveHex(image["payload_sha256"] | "", 64) &&
         image["payload_bytes"].is<uint32_t>() && image["payload_bytes"].as<uint32_t>() > 0;
}

bool validate(const JsonDocument& doc) {
  const char* type = doc["event_type"] | "";
  const bool passive = strcmp(type, "PASSIVE_VIBRATION_CANDIDATE") == 0;
  const bool sync = strcmp(type, "TIME_SYNC") == 0;
  const bool decision = strcmp(type, "POLICY_DECISION") == 0;
  const bool actionResult = strcmp(type, "OBSERVATION_ACTION_RESULT") == 0;
  const auto result = doc["result"].as<JsonObjectConst>();
  if (decision) {
    if (!validDecision(doc, result)) return false;
  } else if (actionResult) {
    if (!validActionResult(doc, result)) return false;
  } else if (sync) {
    if (strcmp(doc["source"] | "", "PC_CLOCK") ||
        strcmp(result["scope"] | "", "CLOCK_REFERENCE_ONLY") ||
        strcmp(result["pc_clock_accuracy"] | "", "UNVERIFIED") ||
        !passiveHex(result["sync_id"] | "", 32) ||
        !result["utc_unix_ms"].is<uint64_t>() || !result["device_monotonic_ms"].is<uint64_t>() ||
        !result["transport_uncertainty_ms"].is<uint32_t>()) return false;
    uint64_t epoch = result["utc_unix_ms"], mono = result["device_monotonic_ms"];
    uint32_t uncertainty = result["transport_uncertainty_ms"];
    if (epoch < kClockMinUtcMs || epoch >= kClockMaxUtcMs || !uncertainty || uncertainty > 501 ||
        mono > doc["monotonic_ms"].as<uint64_t>() || doc["monotonic_ms"].as<uint64_t>() - mono > 5000)
      return false;
  } else if (passive) {
    PassiveDetector detector = PassiveDetector::B2;
    if (strcmp(doc["source"] | "", "PC_AD3") != 0 ||
        strcmp(result["scope"] | "", "PASSIVE_CANDIDATE_ONLY") != 0 ||
        !parsePassiveDetector(result["detector"] | "", detector) ||
        strcmp(result["source_time_quality"] | "", "SAMPLE_CLOCK_ONLY") != 0 ||
        !result["clipping_verified"].is<bool>() || result["clipping_verified"].as<bool>() ||
        !passiveHex(result["source_run_id"] | "", 32) ||
        !passiveHex(result["config_sha256"] | "", 64)) return false;
    for (const char* key : {"candidate_seq", "source_start_ms", "source_end_ms", "peak_rms_uV", "peak_score_milli"})
      if (!result[key].is<uint32_t>()) return false;
    uint32_t start = result["source_start_ms"], end = result["source_end_ms"];
    uint32_t number = result["candidate_seq"], peak = result["peak_rms_uV"], score = result["peak_score_milli"];
    if (!number || number > 1000000 || end <= start || end - start > 15000 ||
        !peak || peak > 50000000 || score < passiveMinScoreMilli(detector) || score > 1000000000) return false;
  } else if (strcmp(doc["source"] | "", "XIAO") != 0 ||
             strcmp(result["scope"] | "", "DEVICE_DIAGNOSTIC_ONLY") != 0) return false;
  const char* boot = doc["boot_id"] | "";
  if (strlen(boot) != 32) return false;
  for (const char* p = boot; *p; ++p)
    if (!((*p >= '0' && *p <= '9') || (*p >= 'a' && *p <= 'f'))) return false;
  char expectedStream[64]; snprintf(expectedStream, sizeof(expectedStream), "live/%s", deviceId);
  for (const char* key : {"case_id", "session_id", "observation_id", "request_id", "image_ref", "model_version"})
    if (!doc.containsKey(key)) return false;
  for (const char* key : {"case_id", "session_id", "request_id", "model_version"})
    if (!doc[key].isNull()) return false;
  // Only observation events may reference an observation or a transferred image.
  if (!decision && !actionResult && !doc["observation_id"].isNull()) return false;
  if (!actionResult && !doc["image_ref"].isNull()) return false;
  const char* clockQuality = doc["time_quality"] | "";
  bool clockOk = strcmp(clockQuality, "UNSYNCED") == 0 && doc.containsKey("timestamp_utc") &&
                 doc["timestamp_utc"].isNull() && !sync && !doc.containsKey("clock_sync_event_id");
  if (strcmp(clockQuality, "ESTIMATED") == 0)
    clockOk = evidenceUtcValid(doc["timestamp_utc"] | "") &&
              doc["clock_sync_event_id"].is<const char*>() &&
              strlen(doc["clock_sync_event_id"]) > 0 && strlen(doc["clock_sync_event_id"]) < 100;
  return strcmp(doc["schema_version"] | "", "bll.evidence.v1") == 0 &&
    strcmp(doc["device_id"] | "", deviceId) == 0 &&
    strcmp(doc["origin"] | "", "LIVE") == 0 &&
    strcmp(doc["stream_id"] | "", expectedStream) == 0 &&
    clockOk &&
    doc["boot_id"].is<const char*>() && strlen(doc["boot_id"]) == 32 &&
    doc["seq"].is<uint32_t>() && doc["seq"].as<uint32_t>() > 0 &&
    doc["monotonic_ms"].is<uint64_t>() &&
    doc["event_id"].is<const char*>() &&
    (passive || sync || decision || actionResult ||
     strcmp(type, "BOOT") == 0 || strcmp(type, "DEVICE_DIAGNOSTIC") == 0) &&
    doc["quality"]["valid"].is<bool>() && doc["quality"]["valid"].as<bool>() &&
    doc["quality"]["reason_codes"].is<JsonArrayConst>() && doc["quality"]["reason_codes"].size() == 0 &&
    doc["producer_version"].is<const char*>();
}

bool decode(JsonDocument& doc) {
  if (deserializeJson(doc, static_cast<const char*>(buffer)) || !validate(doc)) return false;
  char expected[100];
  snprintf(expected, sizeof(expected), "%s/%s/%lu", deviceId,
           doc["boot_id"].as<const char*>(), static_cast<unsigned long>(doc["seq"].as<uint32_t>()));
  return strcmp(expected, doc["event_id"].as<const char*>()) == 0;
}

// Validated JSON -> pure core event. Strings are copied; the document may be reused.
void toCommitted(const JsonDocument& doc, uint32_t index, bll::CommittedEvent& e) {
  e = bll::CommittedEvent();
  e.commitIndex = index;
  e.origin = bll::Origin::Live;  // validate() admits LIVE only
  const char* type = doc["event_type"] | "";
  e.type = strcmp(type, "BOOT") == 0 ? bll::EventType::Boot :
           strcmp(type, "DEVICE_DIAGNOSTIC") == 0 ? bll::EventType::DeviceDiagnostic :
           strcmp(type, "PASSIVE_VIBRATION_CANDIDATE") == 0 ? bll::EventType::PassiveCandidate :
           strcmp(type, "TIME_SYNC") == 0 ? bll::EventType::TimeSync :
           strcmp(type, "POLICY_DECISION") == 0 ? bll::EventType::PolicyDecision :
           strcmp(type, "OBSERVATION_ACTION_RESULT") == 0 ? bll::EventType::ActionResult :
           bll::EventType::Unknown;
  bll::copyText(e.eventId, sizeof(e.eventId), doc["event_id"] | "");
  bll::copyText(e.bootId, sizeof(e.bootId), doc["boot_id"] | "");
  e.monotonicMs = doc["monotonic_ms"].as<uint64_t>();
  e.hasUtc = doc["timestamp_utc"].is<const char*>();
  bll::copyText(e.utc, sizeof(e.utc), doc["timestamp_utc"] | "");
  const auto result = doc["result"].as<JsonObjectConst>();
  if (e.type == bll::EventType::PolicyDecision) {
    bll::copyText(e.decisionId, sizeof(e.decisionId), result["decision_id"] | "");
    bll::copyText(e.actionId, sizeof(e.actionId), result["action_id"] | "");
    bll::parseAction(result["action"] | "", e.action);
    bll::copyText(e.triggerId, sizeof(e.triggerId), result["caused_by_event_ids"][0] | "");
  } else if (e.type == bll::EventType::ActionResult) {
    bll::copyText(e.actionId, sizeof(e.actionId), result["action_id"] | "");
    bll::parsePose(result["pose"] | "", e.pose);
    const char* status = result["status"] | "";
    e.status = strcmp(status, "COMPLETED") == 0 ? bll::ActionStatus::Completed :
               strcmp(status, "INTERRUPTED") == 0 ? bll::ActionStatus::Interrupted :
               bll::ActionStatus::Failed;
    const char* captured = result["capture"]["status"] | "";
    e.larvaVisible = result["vision"]["larva_visible"] | false;
    e.capture = strcmp(captured, "SUCCESS") == 0 ? bll::CaptureOutcome::Success :
                strcmp(captured, "FAILED") == 0 ? bll::CaptureOutcome::Failed :
                bll::CaptureOutcome::NotAttempted;
  }
}

// Called only for committed lines (replay at boot, or after append + readback).
void reduce(const JsonDocument& doc) {
  toCommitted(doc, events, scratchEvent);
  ++events;
  switch (scratchEvent.type) {
    case bll::EventType::Boot: ++boots; break;
    case bll::EventType::DeviceDiagnostic: ++diagnostics; break;
    case bll::EventType::PassiveCandidate:
      ++passiveCandidates;
      indexCandidate(doc["result"]["source_run_id"] | "", doc["result"]["candidate_seq"].as<uint32_t>());
      break;
    case bll::EventType::TimeSync: ++timeSyncEvents; break;
    case bll::EventType::PolicyDecision: ++decisionEvents; break;
    case bll::EventType::ActionResult: ++actionResultEvents; break;
    default: break;
  }
  bll::reduce(temporal, scratchEvent);
  // Diagnostics, candidates, decisions and captures never confirm larva activity.
}

void fail(const char* reason) {
  writable = false; storageReason = reason; ++writeFailures;
}

void recover() {
  events = boots = diagnostics = damaged = passiveCandidates = timeSyncEvents = 0;
  decisionEvents = actionResultEvents = 0;
  candidateCount = 0; candidateIndexComplete = true;
  temporal = bll::TemporalState();
  segment = 1;
  for (unsigned i = 1; i <= kSegments; ++i) {
    char path[64]; pathFor(i, path, sizeof(path));
    if (!LittleFS.exists(path)) continue;
    segment = i;
    File file = openLines(path);
    if (!file || file.size() > kSegmentBytes) { ++damaged; segment = i + 1; continue; }
    bool bad = false;
    while (true) {
      Line result = readLine(file);
      if (result == Line::End) break;
      auto& doc = workDocument;
      doc.clear();
      if (result != Line::Complete || !decode(doc)) { bad = true; break; }
      reduce(doc);
    }
    size_t size = file.size(); file.close();
    if (bad) { ++damaged; segment = i + 1; }
    else if (size + kMaxLine + 1 > kSegmentBytes) segment = i + 1;
  }
  restored = events;
  writable = segment <= kSegments;
  storageReason = !writable ? "SEGMENTS_FULL" : damaged ? "RECOVERED_VALID_PREFIX_CORRUPT_SEGMENT_RETAINED" : "READY";
}

bool append(const char* type, const PassiveMessage* passive = nullptr, const EvidenceClock* clock = nullptr,
            const ObservationDecisionRecord* decision = nullptr,
            const ObservationResultRecord* outcome = nullptr) {
  if (!mounted || !writable) return false;
  if (LittleFS.totalBytes() < LittleFS.usedBytes() + kReserve + kMaxLine + 1) {
    fail("SPACE_RESERVE_REACHED"); return false;
  }
  auto& doc = workDocument;
  doc.clear();
  doc["schema_version"] = "bll.evidence.v1";
  doc["device_id"] = deviceId; doc["boot_id"] = bootId; doc["seq"] = ++seq;
  char id[100]; snprintf(id, sizeof(id), "%s/%s/%lu", deviceId, bootId, static_cast<unsigned long>(seq));
  doc["event_id"] = id;
  char stream[64]; snprintf(stream, sizeof(stream), "live/%s", deviceId);
  doc["stream_id"] = stream; doc["origin"] = "LIVE"; doc["source"] = "XIAO";
  doc["timestamp_utc"] = nullptr; doc["time_quality"] = "UNSYNCED";
  const uint64_t now = monotonicMs();
  doc["monotonic_ms"] = now;
  const EvidenceClock* reference = clock ? clock : clockFresh(now) ? &activeClock : nullptr;
  char utcText[25];
  if (reference && now >= reference->monoMs &&
      evidenceUtcText(reference->utcMs + now - reference->monoMs, utcText, sizeof(utcText))) {
    doc["timestamp_utc"] = utcText;
    doc["time_quality"] = "ESTIMATED";
    doc["clock_sync_event_id"] = clock ? id : reference->eventId;
  }
  doc["event_type"] = type;
  doc["case_id"] = nullptr; doc["session_id"] = nullptr;
  doc["observation_id"] = nullptr; doc["request_id"] = nullptr; doc["image_ref"] = nullptr;
  doc["producer_version"] = config::kFirmwareVersion; doc["model_version"] = nullptr;
  doc["quality"]["valid"] = true; doc["quality"].createNestedArray("reason_codes");
  doc["result"]["scope"] = "DEVICE_DIAGNOSTIC_ONLY";
  doc["result"]["reset_reason"] = static_cast<int>(esp_reset_reason());
  doc["result"]["free_heap_bytes"] = ESP.getFreeHeap();
  if (rotationNote.valid && strcmp(type, "DEVICE_DIAGNOSTIC") == 0) {
    // Extra diagnostic fields only (no new event type): older firmware still reads the log.
    doc["result"]["maintenance"] = "LOG_ROTATED_AFTER_HOST_EXPORT";
    doc["result"]["previous_log_events"] = rotationNote.events;
    doc["result"]["previous_log_bytes"] = rotationNote.bytes;
    doc["result"]["previous_log_sha256"] = rotationNote.sha256;
  }
  if (passive) {
    doc["source"] = "PC_AD3";
    auto result = doc["result"].to<JsonObject>();
    result["scope"] = "PASSIVE_CANDIDATE_ONLY";
    result["detector"] = passiveDetectorName(passive->detector);
    result["source_run_id"] = passive->run;
    result["candidate_seq"] = passive->candidate;
    result["source_start_ms"] = passive->startMs;
    result["source_end_ms"] = passive->endMs;
    result["peak_rms_uV"] = passive->peakUv;
    result["peak_score_milli"] = passive->scoreMilli;
    result["config_sha256"] = passive->configSha;
    result["source_time_quality"] = "SAMPLE_CLOCK_ONLY";
    result["clipping_verified"] = false;
  } else if (clock) {
    doc["source"] = "PC_CLOCK";
    auto result = doc["result"].to<JsonObject>();
    result["scope"] = "CLOCK_REFERENCE_ONLY";
    result["utc_unix_ms"] = clock->utcMs;
    result["device_monotonic_ms"] = clock->monoMs;
    result["transport_uncertainty_ms"] = clock->transportUncertaintyMs;
    result["pc_clock_accuracy"] = "UNVERIFIED";
    result["sync_id"] = clock->syncId;
  } else if (decision) {
    const bll::ActionPlan& plan = *decision->plan;
    const bool capture = bll::isCapture(plan.action);
    auto result = doc["result"].to<JsonObject>();
    result["scope"] = "OBSERVATION_POLICY";
    result["kind"] = "RULE_BASED_NOT_TRAINED";
    result["policy_version"] = bll::kPolicyVersion;
    result["reducer_version"] = bll::kReducerVersion;
    result["decision_id"] = decision->decisionId;
    result["action"] = bll::actionName(plan.action);
    if (capture) {
      result["action_id"] = decision->actionId;
      result["target_pose"] = bll::poseName(plan.pose);
      doc["observation_id"] = decision->actionId;
    } else {
      result["action_id"] = nullptr;
      result["target_pose"] = nullptr;
    }
    result["caused_by_event_ids"].to<JsonArray>().add(plan.triggerId);
    auto reasons = result["reason_codes"].to<JsonArray>();
    for (unsigned bit = 0; bit < 32; ++bit)
      if ((plan.reasons >> bit) & 1u) reasons.add(bll::reasonCode(bit));
    auto limits = result["constraints"].to<JsonObject>();
    limits["live_mode"] = decision->constraints->liveMode;
    limits["storage_writable"] = decision->constraints->storageWritable;
    limits["safety_ok"] = decision->constraints->safetyOk;
    limits["device_busy"] = decision->constraints->deviceBusy;
    limits["light"] = decision->lightName;
    limits["light_source"] = decision->lightSource;
    auto poses = result["capabilities"].to<JsonArray>();
    for (unsigned i = 0; i < bll::kPoseCount; ++i)
      if (decision->capabilities->pose[i]) poses.add(bll::poseName(static_cast<bll::Pose>(i)));
    auto settings = result["config"].to<JsonObject>();
    settings["view_recency_ms"] = decision->config->viewRecencyMs;
    settings["camera_cooldown_ms"] = decision->config->cameraCooldownMs;
    settings["trigger_max_age_ms"] = decision->config->triggerMaxAgeMs;
    settings["max_consecutive_failures"] = decision->config->maxConsecutiveFailures;
    settings["max_captures_per_episode"] = decision->config->maxCapturesPerEpisode;
    settings["stop_when_larva_visible"] = decision->config->stopWhenLarvaVisible;
  } else if (outcome) {
    auto result = doc["result"].to<JsonObject>();
    result["scope"] = "OBSERVATION_ACTION";
    result["action_id"] = outcome->actionId;
    result["decision_event_id"] = outcome->decisionEventId;
    result["status"] = outcome->status == bll::ActionStatus::Completed ? "COMPLETED" :
                       outcome->status == bll::ActionStatus::Interrupted ? "INTERRUPTED" : "FAILED";
    result["action"] = bll::actionName(outcome->action);
    result["pose"] = bll::poseName(outcome->pose);
    auto capture = result["capture"].to<JsonObject>();
    capture["status"] = outcome->status == bll::ActionStatus::Interrupted ? "UNKNOWN" :
                        outcome->capture == bll::CaptureOutcome::Success ? "SUCCESS" :
                        outcome->capture == bll::CaptureOutcome::Failed ? "FAILED" : "NOT_ATTEMPTED";
    capture["reason"] = outcome->captureReason;
    capture["duration_ms"] = outcome->durationMs;
    capture["presence"] = "UNKNOWN";
    capture["movement"] = "UNKNOWN";
    capture["presence_reason"] = "LARVA_MODEL_UNVERIFIED";
    if (outcome->visionTopClass >= 0 && outcome->capture == bll::CaptureOutcome::Success &&
        outcome->status == bll::ActionStatus::Completed) {
      auto vision = result["vision"].to<JsonObject>();
      vision["top_class_id"] = outcome->visionTopClass;
      vision["score"] = outcome->visionTopScore;
      vision["larva_visible"] = outcome->larvaVisible;
      vision["rule"] = "VISIBLE_LARVA_SCORE_GE_80_STOPS_EPISODE";
    }
    doc["observation_id"] = outcome->actionId;
    if (outcome->capture == bll::CaptureOutcome::Success && outcome->status == bll::ActionStatus::Completed) {
      auto image = doc["image_ref"].to<JsonObject>();
      image["store"] = "HOST_USB_STREAM";
      image["status"] = "SENT_TO_HOST_UNCONFIRMED";
      image["payload_sha256"] = outcome->payloadSha256;
      image["payload_bytes"] = outcome->payloadBytes;
    }
  }
  size_t length = measureJson(doc);
  if (doc.overflowed() || length >= kMaxLine || !validate(doc)) { fail("ENCODE_FAILED"); return false; }
  serializeJson(doc, buffer, sizeof(buffer));
  char path[64]; pathFor(segment, path, sizeof(path));
  File existing = LittleFS.open(path, FILE_READ);
  size_t offset = existing ? existing.size() : 0; existing.close();
  if (offset + length + 1 > kSegmentBytes) {
    if (++segment > kSegments) { fail("SEGMENTS_FULL"); return false; }
    pathFor(segment, path, sizeof(path)); offset = 0;
    if (LittleFS.exists(path)) { fail("SEGMENT_COLLISION"); return false; }
  }
  File file = LittleFS.open(path, FILE_APPEND);
  if (!file) { fail("OPEN_FAILED"); return false; }
  size_t written = file.write(reinterpret_cast<const uint8_t*>(buffer), length);
  size_t newline = file.write(static_cast<uint8_t>('\n'));
  file.flush(); file.close();
  if (written != length || newline != 1) { fail("SHORT_WRITE"); return false; }
  // Read back the exact serialized bytes, not merely a success flag.
  File check = LittleFS.open(path, FILE_READ);
  bool ok = check && check.seek(offset);
  for (size_t i = 0; ok && i < length; ++i) ok = check.read() == static_cast<uint8_t>(buffer[i]);
  ok = ok && check.read() == '\n'; check.close();
  if (!ok) { fail("READBACK_FAILED"); return false; }
  readbackOk = true; reduce(doc); return true;
}

void mountAndBoot() {
  mounted = LittleFS.begin(false, "/littlefs", 4, config::kEvidencePartitionLabel);
  if (!mounted) { writable = false; storageReason = "MOUNT_FAILED_NO_AUTOFORMAT"; return; }
  if (!LittleFS.exists(kDirectory) && !LittleFS.mkdir(kDirectory)) { fail("MKDIR_FAILED"); return; }
  recover(); append("BOOT");
  observationRecoverAfterBoot();  // an intent left open by a reset becomes INTERRUPTED
}
} // namespace

void evidenceBegin() {
  snprintf(deviceId, sizeof(deviceId), "%s-%012llx", config::kDevicePrefix, static_cast<unsigned long long>(ESP.getEfuseMac()));
  snprintf(bootId, sizeof(bootId), "%08lx%08lx%08lx%08lx",
           static_cast<unsigned long>(esp_random()), static_cast<unsigned long>(esp_random()),
           static_cast<unsigned long>(esp_random()), static_cast<unsigned long>(esp_random()));
  mountAndBoot(); // Runs without USB, uses no external sensor outputs.
}

void evidenceStatus() {
  auto& doc = workDocument;
  doc.clear();
  doc["schema"] = "bll.evidence_state.v1";
  doc["device_id"] = deviceId; doc["boot_id"] = bootId;
  doc["mounted"] = mounted; doc["writable"] = writable;
  doc["storage_reason"] = storageReason;
  doc["corrupt_segments"] = damaged; doc["write_failures"] = writeFailures;
  doc["restored_events"] = restored; doc["events"] = events;
  doc["boot_events"] = boots; doc["diagnostic_events"] = diagnostics;
  doc["passive_candidates"] = passiveCandidates;
  doc["time_sync_events"] = timeSyncEvents;
  doc["clock_sync_supported"] = true;
  doc["readback_verified_this_boot"] = readbackOk;
  doc["total_bytes"] = mounted ? LittleFS.totalBytes() : 0;
  doc["used_bytes"] = mounted ? LittleFS.usedBytes() : 0;
  doc["current_activity"] = "UNKNOWN"; doc["visual_presence"] = "UNKNOWN";
  doc["last_confirmed_movement_utc"] = nullptr;
  doc["passive_status"] = "EXPERIMENTAL"; doc["active_status"] = "HISTORICAL_ONLY";
  doc["decision_events"] = decisionEvents;
  doc["action_result_events"] = actionResultEvents;
  doc["observation_policy"] = bll::kPolicyVersion;
  doc["reducer_version"] = bll::kReducerVersion;
  auto observation = doc["observation"].to<JsonObject>();
  observation["pending_trigger"] = bll::pendingTrigger(temporal);
  observation["intent_open"] = temporal.intent.open;
  observation["waits"] = temporal.waits;
  observation["completed"] = temporal.completed;
  observation["failed"] = temporal.failed;
  observation["interrupted"] = temporal.interrupted;
  observation["coalesced_triggers"] = temporal.coalescedTriggers;
  observation["reducer_gaps"] = temporal.gaps;
  const uint64_t now = monotonicMs();
  doc["time_quality"] = clockFresh(now) ? "ESTIMATED" : "UNSYNCED";
  if (activeClock.valid) {
    doc["clock_sync_event_id"] = activeClock.eventId;
    doc["clock_age_ms"] = now - activeClock.monoMs;
  }
  Serial.print("EVIDENCE_STATUS "); serializeJson(doc, Serial); Serial.println();
}

bool evidenceDiagnostic() { return append("DEVICE_DIAGNOSTIC"); }

const bll::TemporalState& evidenceTemporalState() { return temporal; }
bool evidenceStorageReady() { return mounted && writable && !damaged; }
const char* evidenceBootId() { return bootId; }
uint64_t evidenceMonotonicMs() { return monotonicMs(); }

namespace {
bool appendWithId(const char* type, const ObservationDecisionRecord* decision,
                  const ObservationResultRecord* outcome, char* eventId, size_t size) {
  if (!evidenceStorageReady() || !append(type, nullptr, nullptr, decision, outcome)) return false;
  if (eventId) snprintf(eventId, size, "%s/%s/%lu", deviceId, bootId, static_cast<unsigned long>(seq));
  return true;
}
}  // namespace

bool evidenceAppendDecision(const ObservationDecisionRecord& record, char* eventId, size_t size) {
  return appendWithId("POLICY_DECISION", &record, nullptr, eventId, size);
}

bool evidenceAppendActionResult(const ObservationResultRecord& record, char* eventId, size_t size) {
  return appendWithId("OBSERVATION_ACTION_RESULT", nullptr, &record, eventId, size);
}

void evidenceClockProbe(const char* nonce) {
  if (!passiveHex(nonce, 32)) { Serial.println("EVIDENCE_ERROR reason=invalid_clock_nonce"); return; }
  memcpy(probeNonce, nonce, 33);
  probeMonoMs = monotonicMs(); probeValid = true;
  Serial.printf("EVIDENCE_CLOCK_PROBE nonce=%s boot_id=%s monotonic_ms=%llu\n", probeNonce, bootId,
                static_cast<unsigned long long>(probeMonoMs));
}

void evidenceClockSync(char* payload) {
  char* values[4]; unsigned count = 0; char* context = nullptr;
  for (char* p = strtok_r(payload, " ", &context); p; p = strtok_r(nullptr, " ", &context)) {
    if (count == 4) { Serial.println("EVIDENCE_ERROR reason=invalid_clock_payload"); return; }
    values[count++] = p;
  }
  if (count != 4 || !passiveHex(values[0], 32) || !passiveHex(values[1], 32)) {
    Serial.println("EVIDENCE_ERROR reason=invalid_clock_payload"); return;
  }
  uint64_t numbers[2];
  for (unsigned i = 0; i < 2; ++i) {
    for (const char* p = values[i + 2]; *p; ++p) if (*p < '0' || *p > '9') {
      Serial.println("EVIDENCE_ERROR reason=invalid_clock_number"); return;
    }
    errno = 0; numbers[i] = strtoull(values[i + 2], nullptr, 10);
    if (errno == ERANGE) { Serial.println("EVIDENCE_ERROR reason=invalid_clock_number"); return; }
  }
  uint64_t now = monotonicMs();
  if (!probeValid || strcmp(values[0], bootId) || strcmp(values[1], probeNonce) ||
      now < probeMonoMs || now - probeMonoMs > 5000) {
    Serial.println("EVIDENCE_ERROR reason=stale_clock_probe"); return;
  }
  if (numbers[0] < kClockMinUtcMs || numbers[0] >= kClockMaxUtcMs || !numbers[1] || numbers[1] > 501) {
    Serial.println("EVIDENCE_ERROR reason=clock_out_of_range"); return;
  }
  if (!mounted || !writable || damaged) {
    Serial.println("EVIDENCE_ERROR reason=clock_storage_unavailable"); return;
  }
  EvidenceClock proposed;
  proposed.utcMs = numbers[0]; proposed.monoMs = probeMonoMs;
  proposed.transportUncertaintyMs = static_cast<uint32_t>(numbers[1]);
  memcpy(proposed.syncId, probeNonce, 33);
  probeValid = false;  // One commit per probe. A retry must acquire a fresh sample.
  if (!append("TIME_SYNC", nullptr, &proposed)) {
    Serial.println("EVIDENCE_ERROR reason=clock_write_failed"); return;
  }
  snprintf(proposed.eventId, sizeof(proposed.eventId), "%s/%s/%lu", deviceId, bootId,
           static_cast<unsigned long>(seq));
  proposed.valid = true; activeClock = proposed;
  Serial.printf("EVIDENCE_TIME_SYNC status=stored event_id=%s time_quality=ESTIMATED\n", activeClock.eventId);
}

void evidencePassive(char* payload, bool validateOnly) {
  PassiveMessage message;
  if (!parsePassiveMessage(payload, message)) {
    Serial.println("PASSIVE_ERROR reason=invalid_payload"); return;
  }
  if (validateOnly) {
    Serial.printf("PASSIVE_ACK run=%s candidate=%lu status=validated_only\n", message.run,
                  static_cast<unsigned long>(message.candidate)); return;
  }
  if (!mounted || !writable || damaged) {
    Serial.println("PASSIVE_ERROR reason=storage_unavailable_or_corrupt"); return;
  }
  // Durable deduplication, also after reboot. A conflicting retry is rejected.
  // The index holds every committed candidate (boot replay + commits), so a miss
  // means new; only a possible retry pays for the full scan.
  const bool scan = !candidateCertainlyNew(message.run, message.candidate);
  for (unsigned i = 1; scan && i <= kSegments; ++i) {
    char path[64]; pathFor(i, path, sizeof(path));
    if (!LittleFS.exists(path)) continue;
    File file = openLines(path);
    if (!file || file.size() > kSegmentBytes) {
      Serial.println("PASSIVE_ERROR reason=storage_scan_failed"); return;
    }
    while (true) {
      Line result = readLine(file);
      if (result == Line::End) break;
      workDocument.clear();
      if (result != Line::Complete || !decode(workDocument)) {
        Serial.println("PASSIVE_ERROR reason=storage_scan_failed"); return;
      }
      auto stored = workDocument["result"].as<JsonObjectConst>();
      if (strcmp(stored["source_run_id"] | "", message.run) ||
          stored["candidate_seq"].as<uint32_t>() != message.candidate) continue;
      bool same = stored["source_start_ms"].as<uint32_t>() == message.startMs &&
                  stored["source_end_ms"].as<uint32_t>() == message.endMs &&
                  stored["peak_rms_uV"].as<uint32_t>() == message.peakUv &&
                  stored["peak_score_milli"].as<uint32_t>() == message.scoreMilli &&
                  strcmp(stored["detector"] | "", passiveDetectorName(message.detector)) == 0 &&
                  strcmp(stored["config_sha256"] | "", message.configSha) == 0;
      if (!same) Serial.println("PASSIVE_ERROR reason=conflicting_candidate_id");
      else Serial.printf("PASSIVE_ACK run=%s candidate=%lu status=duplicate event_id=%s\n",
                         message.run, static_cast<unsigned long>(message.candidate),
                         workDocument["event_id"].as<const char*>());
      return;
    }
  }
  if (!append("PASSIVE_VIBRATION_CANDIDATE", &message)) {
    Serial.println("PASSIVE_ERROR reason=write_failed"); return;
  }
  Serial.printf("PASSIVE_ACK run=%s candidate=%lu status=stored event_id=%s/%s/%lu\n",
                message.run, static_cast<unsigned long>(message.candidate), deviceId, bootId,
                static_cast<unsigned long>(seq));
  // Committed first, ACK sent; only now may the policy react (ends with OBSERVATION_DONE).
  observationAfterPassiveStored();
}

// Log rotation (0.14.6, 2026-11-15). The 1.25 MiB log fills within weeks (SPACE_RESERVE_REACHED).
// The PC first saves a full export and sends its event count and the SHA-256 of the exact
// lines it received (each line + '\n', in log order). The XIAO re-reads every committed line,
// and removes the log only if both match, nothing is running and no segment is damaged.
// Segments are removed newest first, so an interruption leaves a valid older prefix.
// The emptied log starts with one DEVICE_DIAGNOSTIC naming the previous log's count and hash
// (UNSYNCED: the TIME_SYNC it would refer to is gone), then the XIAO restarts so the new log
// gets a BOOT and the reducer, candidate index and RAM state start clean. Lost on the device
// by design: duplicate detection for candidates of earlier runs and lifetime counters.
void evidenceRotate(char* payload) {
  char* context = nullptr;
  char* countText = strtok_r(payload, " ", &context);
  char* hashText = strtok_r(nullptr, " ", &context);
  if (!countText || !hashText || strtok_r(nullptr, " ", &context) || !passiveHex(hashText, 64)) {
    Serial.println("EVIDENCE_ERROR reason=invalid_rotate_payload"); return;
  }
  for (const char* p = countText; *p; ++p) if (*p < '0' || *p > '9') {
    Serial.println("EVIDENCE_ERROR reason=invalid_rotate_payload"); return;
  }
  errno = 0; const unsigned long long expected = strtoull(countText, nullptr, 10);
  if (errno == ERANGE || expected == 0 || expected > UINT32_MAX) {
    Serial.println("EVIDENCE_ERROR reason=invalid_rotate_payload"); return;
  }
  if (!mounted || damaged) { Serial.println("EVIDENCE_ERROR reason=rotate_storage_unavailable_or_corrupt"); return; }
  if (const char* busy = observationBusyReason()) {
    Serial.printf("EVIDENCE_ERROR reason=rotate_observation_busy detail=%s\n", busy); return;
  }
  mbedtls_sha256_context sha;
  mbedtls_sha256_init(&sha);
  bool ok = mbedtls_sha256_starts_ret(&sha, 0) == 0;
  uint32_t count = 0, bytes = 0;
  for (unsigned i = 1; ok && i <= kSegments; ++i) {
    char path[64]; pathFor(i, path, sizeof(path));
    if (!LittleFS.exists(path)) continue;
    File file = openLines(path);
    if (!file || file.size() > kSegmentBytes) { ok = false; break; }
    while (ok) {
      Line result = readLine(file);
      if (result == Line::End) break;
      workDocument.clear();
      if (result != Line::Complete || !decode(workDocument)) { ok = false; break; }
      const size_t length = strlen(buffer);
      ok = mbedtls_sha256_update_ret(&sha, reinterpret_cast<const uint8_t*>(buffer), length) == 0 &&
           mbedtls_sha256_update_ret(&sha, reinterpret_cast<const uint8_t*>("\n"), 1) == 0;
      ++count; bytes += length + 1;
    }
    file.close();
  }
  uint8_t digest[32];
  ok = ok && mbedtls_sha256_finish_ret(&sha, digest) == 0;
  mbedtls_sha256_free(&sha);
  if (!ok) { Serial.println("EVIDENCE_ERROR reason=rotate_scan_failed"); return; }
  char hex[65];
  for (unsigned i = 0; i < 32; ++i) snprintf(hex + 2 * i, 3, "%02x", digest[i]);
  if (count != expected || count != events || strcmp(hex, hashText) != 0) {
    Serial.printf("EVIDENCE_ERROR reason=rotate_mismatch device_events=%lu device_sha256=%s\n",
                  static_cast<unsigned long>(count), hex);
    return;
  }
  Serial.printf("EVIDENCE_ROTATE stage=verified events=%lu bytes=%lu sha256=%s\n",
                static_cast<unsigned long>(count), static_cast<unsigned long>(bytes), hex);
  for (unsigned i = kSegments; i >= 1; --i) {
    char path[64]; pathFor(i, path, sizeof(path));
    if (LittleFS.exists(path) && !LittleFS.remove(path)) {
      fail("ROTATE_REMOVE_FAILED");
      Serial.printf("EVIDENCE_ERROR reason=rotate_remove_failed segment=%u\n", i); return;
    }
  }
  recover();                  // empty log: counters, reducer and candidate index start again
  activeClock.valid = false;  // its TIME_SYNC was removed with the log
  rotationNote.valid = true; rotationNote.events = count; rotationNote.bytes = bytes;
  memcpy(rotationNote.sha256, hex, sizeof(hex));
  const bool noted = append("DEVICE_DIAGNOSTIC");
  rotationNote.valid = false;
  Serial.printf("EVIDENCE_ROTATE stage=done removed_events=%lu diagnostic=%s restart=1\n",
                static_cast<unsigned long>(count), noted ? "stored" : "not_stored");
  Serial.flush(); delay(300);
  ESP.restart();
}

void evidenceReadPage(uint32_t afterIndex) {
  if (!mounted) { Serial.println("EVIDENCE_ERROR reason=storage_unavailable"); return; }
  Serial.printf("EVIDENCE_BEGIN after_index=%lu\n", static_cast<unsigned long>(afterIndex));
  uint32_t index = 0; unsigned sent = 0;
  for (unsigned i = 1; i <= kSegments && sent < kPageSize; ++i) {
    char path[64]; pathFor(i, path, sizeof(path));
    if (!LittleFS.exists(path)) continue;
    File file = openLines(path);
    if (!file || file.size() > kSegmentBytes) continue;
    while (sent < kPageSize) {
      Line result = readLine(file);
      if (result == Line::End) break;
      auto& doc = workDocument;
      doc.clear();
      if (result != Line::Complete || !decode(doc)) break;
      ++index;
      if (index <= afterIndex) continue;
      Serial.print("EVIDENCE_EVENT "); Serial.println(buffer); ++sent;
    }
    file.close();
  }
  uint32_t next = sent ? index : afterIndex;
  Serial.printf("EVIDENCE_END next_index=%lu has_more=%u\n", static_cast<unsigned long>(next), next < events ? 1 : 0);
}

void evidenceInitEmpty() {
  if (mounted) { Serial.println("EVIDENCE_ERROR reason=already_mounted_no_format"); return; }
  const esp_partition_t* part = esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, config::kEvidencePartitionLabel);
  if (!part || part->size != config::kEvidencePartitionSize || part->address != config::kEvidencePartitionAddress) {
    Serial.println("EVIDENCE_ERROR reason=unexpected_partition_no_format"); return;
  }
  // Reuse the existing static line buffer. A local scan array otherwise remains
  // on the stack throughout mountAndBoot() and its filesystem/BOOT write calls.
  constexpr size_t kScanBytes = 1024;
  for (size_t offset = 0; offset < part->size; offset += kScanBytes) {
    if (esp_partition_read(part, offset, buffer, kScanBytes) != ESP_OK) {
      Serial.println("EVIDENCE_ERROR reason=partition_read_failed_no_format"); return;
    }
    for (size_t i = 0; i < kScanBytes; ++i) if (static_cast<uint8_t>(buffer[i]) != 0xff) {
      Serial.println("EVIDENCE_ERROR reason=partition_not_empty_no_format"); return;
    }
  }
  // Explicit command only, and only an entirely erased, expected FS partition.
  Serial.printf("EVIDENCE_INIT stage=empty_verified loop_stack_min_free_bytes=%u\n",
                static_cast<unsigned>(uxTaskGetStackHighWaterMark(nullptr)));
  if (!LittleFS.format()) { Serial.println("EVIDENCE_ERROR reason=empty_partition_format_failed"); return; }
  Serial.printf("EVIDENCE_INIT stage=formatted loop_stack_min_free_bytes=%u\n",
                static_cast<unsigned>(uxTaskGetStackHighWaterMark(nullptr)));
  mountAndBoot(); evidenceStatus();
}
#endif
