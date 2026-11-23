// Low-frequency forest on the XIAO: serial glue around lowfreq_forest_core.h (see lowfreq_runtime.h).
#ifdef BLL_EVIDENCE_STORE
#include "lowfreq_runtime.h"

#include <Arduino.h>

#include <cctype>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#include "lowfreq_forest_core.h"
#include "observation_scheduler.h"
#include "servo_driver.h"

namespace {
bll::lff::Tracker tracker;  // static (~1 KB), no heap
bool begun = false;
char runTag[9] = "";
int64_t offsetMs = 0;  // XIAO millis = AD3 ms + offsetMs (anchored by the bridge)

// The XIAO's own servo motions (millis), most recent 16, from the servo driver's own start /
// last-step times: a move inside an observation step blocks loop() (runMove), so watching
// servoMoving() from the loop misses it (0.15.0 masked only the park, 2026-11-21). Each motion
// is extended by the scheduler's settle time (the arm still rings while it settles) and the
// PC reference's margin (lowfreq_activity.ARM_MARGIN_S).
constexpr unsigned kMotions = 16;
constexpr int64_t kArmMarginMs = 300;
struct Motion { uint32_t a, b; };
Motion motions[kMotions];
unsigned motionHead = 0, motionCount = 0;
uint32_t seenStart = 0;

int64_t settleMs() {
  const bll::SchedulerConfig cfg;
  uint32_t most = 0;
  for (unsigned i = 0; i < bll::kPoseCount; ++i) most = cfg.settleMs[i] > most ? cfg.settleMs[i] : most;
  return most;
}

bool parseU32(const char*& p, uint32_t* out) {
  while (*p == ' ') ++p;
  if (!std::isdigit(static_cast<unsigned char>(*p))) return false;
  errno = 0;
  char* end = nullptr;
  const unsigned long long v = std::strtoull(p, &end, 10);
  if (errno == ERANGE || v > UINT32_MAX || (*end != ' ' && *end != '\0')) return false;
  *out = static_cast<uint32_t>(v);
  p = end;
  return true;
}

bool atEnd(const char* p) {
  while (*p == ' ') ++p;
  return *p == '\0';
}

bool armAt(uint32_t ad3Ms) {
  const int64_t centre = static_cast<int64_t>(ad3Ms) + offsetMs;
  const int64_t lo = centre - static_cast<int64_t>(bll::lff::kHalfWindowMs) - kArmMarginMs;
  const int64_t hi = centre + static_cast<int64_t>(bll::lff::kHalfWindowMs) + kArmMarginMs;
  const int64_t settle = settleMs();
  for (unsigned i = 0; i < motionCount; ++i)
    if (static_cast<int64_t>(motions[i].b) + settle >= lo && static_cast<int64_t>(motions[i].a) <= hi) return true;
  return false;
}

void printSpan(const bll::lff::Span& s) {
  Serial.printf(" span=%lu,%lu,%.3f,%u", static_cast<unsigned long>(s.t0_ms), static_cast<unsigned long>(s.t1_ms),
                static_cast<double>(s.peak_p), static_cast<unsigned>(s.windows));
}
}  // namespace

void lowfreqPoll() {
  if (!servoEverMoved()) return;
  const uint32_t start = servoMotionStartMs();
  uint32_t end = servoLastMotionMs();
  if (servoMoving()) end = millis();  // still moving: up to now
  if (end < start) end = start;
  if (!motionCount || start != seenStart) {  // a motion not seen before (possibly already over)
    Motion m;
    m.a = start;
    m.b = end;
    motions[motionHead] = m;
    motionHead = (motionHead + 1) % kMotions;
    if (motionCount < kMotions) ++motionCount;
    seenStart = start;
  } else {
    Motion& last = motions[(motionHead + kMotions - 1) % kMotions];
    if (end > last.b) last.b = end;
  }
}

void lowfreqCommand(const char* line) {
  lowfreqPoll();  // a move that blocked loop() is recorded before any window is judged
  if (std::strncmp(line, "lowfreq-rms ", 12) == 0) {
    const char* p = line + 12;
    uint32_t t = 0, ch1 = 0, ch2 = 0;
    if (!parseU32(p, &t) || !parseU32(p, &ch1) || !parseU32(p, &ch2) || !atEnd(p)) {
      Serial.println("LFF_ERROR reason=invalid_args");
      return;
    }
    if (!begun) {
      Serial.println("LFF_ERROR reason=not_begun");
      return;
    }
    const bool arm = armAt(t);
    const bll::lff::Result r = tracker.push(t, ch1 * 1e-9f, ch2 * 1e-9f, arm);
    Serial.printf("LFF t=%lu p=%.4f a=%d arm=%d", static_cast<unsigned long>(t),
                  r.usable ? static_cast<double>(r.p) : -1.0, r.active ? 1 : 0, arm ? 1 : 0);
    if (r.span_closed) printSpan(r.closed);
    Serial.println();
  } else if (std::strncmp(line, "lowfreq-begin ", 14) == 0) {
    const char* p = line + 14;
    while (*p == ' ') ++p;
    size_t n = 0;
    char tag[9] = "";
    while (n < 8 && std::isxdigit(static_cast<unsigned char>(p[n]))) { tag[n] = p[n]; ++n; }
    p += n;
    uint32_t now = 0;
    if (n != 8 || *p != ' ' || !parseU32(p, &now) || !atEnd(p)) {
      Serial.println("LFF_ERROR reason=invalid_args");
      return;
    }
    tracker.reset();
    std::memcpy(runTag, tag, sizeof(runTag));
    offsetMs = static_cast<int64_t>(millis()) - now;
    begun = true;
    Serial.printf("LFF_BEGIN run=%s core=%s trees=%d threshold=%.4f\n", runTag, bll::lff::kVersion,
                  LFF_N_TREES, static_cast<double>(LFF_THRESHOLD));
  } else if (std::strncmp(line, "lowfreq-anchor ", 15) == 0) {
    const char* p = line + 15;
    uint32_t now = 0;
    if (!parseU32(p, &now) || !atEnd(p)) {
      Serial.println("LFF_ERROR reason=invalid_args");
      return;
    }
    const int64_t next = static_cast<int64_t>(millis()) - now;
    Serial.printf("LFF_ANCHOR shift_ms=%ld\n", static_cast<long>(next - offsetMs));
    offsetMs = next;
  } else if (std::strcmp(line, "lowfreq-end") == 0) {
    bll::lff::Span s;
    Serial.printf("LFF_END run=%s", runTag);
    if (begun && tracker.flush(&s)) printSpan(s);
    Serial.println();
    begun = false;
  } else {
    Serial.println("LFF_ERROR reason=unknown_command");
  }
}
#endif
