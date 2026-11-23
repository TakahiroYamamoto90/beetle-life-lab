#pragma once
#include <cstdint>
#include <cstddef>
#include <cstring>
#include <cstdlib>
#include <cerrno>

// Compact line protocol; deliberately independent of Arduino for host tests.
// 0.17.0: an optional 8th token names the detector. Without it the candidate is B2 (as before).
enum class PassiveDetector : uint8_t { B2 = 0, LowFreq = 1 };
inline const char* passiveDetectorName(PassiveDetector d) {
  return d == PassiveDetector::LowFreq ? "LOWFREQ_FOREST_RATIO" : "B2_guarded_mad";
}
inline bool parsePassiveDetector(const char* text, PassiveDetector& out) {
  if (!text) return false;
  if (std::strcmp(text, "B2_guarded_mad") == 0) { out = PassiveDetector::B2; return true; }
  if (std::strcmp(text, "LOWFREQ_FOREST_RATIO") == 0) { out = PassiveDetector::LowFreq; return true; }
  return false;
}
// B2 scores start at its threshold 4.0; the low-frequency trigger sends its 1-40 Hz ratio.
inline uint32_t passiveMinScoreMilli(PassiveDetector d) { return d == PassiveDetector::LowFreq ? 1 : 4000; }

struct PassiveMessage {
  char run[33];
  uint32_t candidate, startMs, endMs, peakUv, scoreMilli;
  char configSha[65];
  PassiveDetector detector = PassiveDetector::B2;
};

inline bool passiveHex(const char* text, size_t length) {
  if (!text || std::strlen(text) != length) return false;
  for (size_t i = 0; i < length; ++i)
    if (!((text[i] >= '0' && text[i] <= '9') || (text[i] >= 'a' && text[i] <= 'f'))) return false;
  return true;
}

inline bool parsePassiveMessage(char* text, PassiveMessage& out) {
  char* tokens[8] = {};
  char* context = nullptr;
  unsigned count = 0;
  for (char* p = strtok_r(text, " ", &context); p; p = strtok_r(nullptr, " ", &context)) {
    if (count == 8) return false;
    tokens[count++] = p;
  }
  if ((count != 7 && count != 8) || !passiveHex(tokens[0], 32) || !passiveHex(tokens[6], 64)) return false;
  PassiveDetector detector = PassiveDetector::B2;
  if (count == 8 && !parsePassiveDetector(tokens[7], detector)) return false;
  uint32_t values[5];
  for (unsigned i = 0; i < 5; ++i) {
    const char* value = tokens[i + 1];
    if (!*value) return false;
    for (const char* p = value; *p; ++p) if (*p < '0' || *p > '9') return false;
    errno = 0;
    unsigned long long number = std::strtoull(value, nullptr, 10);
    if (errno == ERANGE || number > UINT32_MAX) return false;
    values[i] = static_cast<uint32_t>(number);
  }
  if (!values[0] || values[0] > 1000000 || values[2] <= values[1] ||
      values[2] - values[1] > 15000 || !values[3] || values[3] > 50000000 ||
      values[4] < passiveMinScoreMilli(detector) || values[4] > 1000000000) return false;
  std::memcpy(out.run, tokens[0], 33);
  out.candidate = values[0]; out.startMs = values[1]; out.endMs = values[2];
  out.peakUv = values[3]; out.scoreMilli = values[4];
  std::memcpy(out.configSha, tokens[6], 65);
  out.detector = detector;
  return true;
}
