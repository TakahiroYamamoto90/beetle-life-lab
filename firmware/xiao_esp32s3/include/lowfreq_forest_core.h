#pragma once
// Low-frequency activity-likeness by a small random forest (pure core, reference only).
//
// Input: one 250 ms window at a time of the CH1/CH2 1-40 Hz RMS that the PC bridge computes from
// the AD3 (host/ad3_bridge/lowfreq_activity.py LowTracker): measured values, not decisions. This
// core keeps the trailing history, builds the six features of host/ad3_bridge/lowfreq_forest.py,
// runs the generated forest (lowfreq_forest_model.h) and merges active windows into spans.
//
// No Arduino, I/O, clock reads or heap allocation: the firmware and the host tests
// (test/host/test_lowfreq_forest_core.cpp) compile this same header. Not wired into the firmware
// yet (slice 1, 2026-11-21). The output is a provisional reference: it is not a larva detection,
// not B2, and the Observation Policy does not use it.
//
// Windows must arrive in order, one per 250 ms; `arm` = the device's own arm was moving in or
// near the window (ARM_MARGIN 0.3 s, as lowfreq_activity._arm_mask): such windows are neither
// used for the floor nor marked.
#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>

#include "lowfreq_forest_model.h"

namespace bll {
namespace lff {

constexpr char kVersion[] = "lowfreq-forest-core-v1";
constexpr unsigned kFloorN = 120;           // 30 s of 250 ms windows (lowfreq_activity.FLOOR_S)
constexpr unsigned kFloorMin = 40;          // 10 s of usable history (MIN_FLOOR_S)
constexpr float kFloorQ = 0.20f;            // 20th percentile, linear (numpy default)
constexpr unsigned kMax1 = 4, kMax3 = 12;   // windows before the current one (1 s, 3 s)
constexpr uint32_t kHalfWindowMs = 125;
constexpr uint32_t kMergeGapMs = 1000;      // lowfreq_activity.MERGE_GAP_S

struct Span {
  uint32_t t0_ms = 0, t1_ms = 0;
  float peak_p = 0.0f;
  uint16_t windows = 0;
};

struct Result {
  bool usable = false;          // floor ready, not arm, all features finite
  bool active = false;          // usable && p >= LFF_THRESHOLD
  float p = NAN;                // forest mean leaf value (NaN when not usable)
  float x[LFF_N_FEATURES] = {}; // features (for logging / tests)
  bool span_closed = false;     // a span ended before this window (see closed)
  Span closed;
};

class Tracker {
 public:
  Result push(uint32_t t_ms, float ch1, float ch2, bool arm) {
    Result r;
    const float f2 = floorOf(hist2_), f1 = floorOf(hist1_);
    const float r2 = std::log(ch2 / f2), r1 = std::log(ch1 / f1);
    // the floors use the windows before this one; then this window joins the history
    remember(hist2_, arm ? NAN : ch2);
    remember(hist1_, arm ? NAN : ch1);
    remember(r2hist_, r2);
    r.x[0] = r2;
    r.x[1] = r1;
    r.x[2] = std::log(ch2 / ch1);
    r.x[3] = recent(kMax1, true);
    r.x[4] = recent(kMax3, true);
    r.x[5] = recent(kMax3, false);
    r.usable = !arm;
    for (float v : r.x) r.usable = r.usable && std::isfinite(v);
    if (r.usable) {
      r.p = lff_predict(r.x);
      r.active = r.p >= LFF_THRESHOLD;
    }
    // spans: [t - 125 ms, t + 125 ms] of active windows, merged across gaps <= 1 s
    const uint32_t a = t_ms > kHalfWindowMs ? t_ms - kHalfWindowMs : 0, b = t_ms + kHalfWindowMs;
    if (open_ && a > span_.t1_ms + kMergeGapMs) {
      r.span_closed = true;
      r.closed = span_;
      open_ = false;
    }
    if (r.active) {
      if (!open_) {
        span_ = Span();
        span_.t0_ms = a;
        span_.peak_p = r.p;
        open_ = true;
      }
      span_.t1_ms = b;
      span_.peak_p = std::max(span_.peak_p, r.p);
      ++span_.windows;
    }
    return r;
  }

  // The open span (if any), e.g. at the end of a run.
  bool flush(Span* out) {
    if (!open_) return false;
    *out = span_;
    open_ = false;
    return true;
  }

  void reset() { *this = Tracker(); }

 private:
  template <unsigned N>
  struct Ring {
    float v[N];
    unsigned head = 0, count = 0;
    Ring() { std::fill(v, v + N, NAN); }
  };

  template <unsigned N>
  static void remember(Ring<N>& ring, float value) {
    ring.v[ring.head] = value;
    ring.head = (ring.head + 1) % N;
    if (ring.count < N) ++ring.count;
  }

  // 20th percentile of the finite values among the last kFloorN windows; NaN with < kFloorMin.
  static float floorOf(const Ring<kFloorN>& ring) {
    float s[kFloorN];
    unsigned k = 0;
    for (unsigned i = 0; i < ring.count; ++i)
      if (std::isfinite(ring.v[i])) s[k++] = ring.v[i];
    if (k < kFloorMin) return NAN;
    std::sort(s, s + k);
    const float pos = (k - 1) * kFloorQ;
    const unsigned lo = static_cast<unsigned>(pos), hi = std::min(lo + 1, k - 1);
    return s[lo] + (s[hi] - s[lo]) * (pos - lo);
  }

  // max (or mean) of the finite r2 values of this window and the n before it; NaN if none.
  float recent(unsigned n, bool takeMax) const {
    float acc = takeMax ? -INFINITY : 0.0f;
    unsigned used = 0;
    for (unsigned j = 0; j <= n && j < r2hist_.count; ++j) {
      const float v = r2hist_.v[(r2hist_.head + kR2N - 1 - j) % kR2N];
      if (!std::isfinite(v)) continue;
      acc = takeMax ? std::max(acc, v) : acc + v;
      ++used;
    }
    if (!used) return NAN;
    return takeMax ? acc : acc / used;
  }

  static constexpr unsigned kR2N = kMax3 + 1;
  Ring<kFloorN> hist1_, hist2_;
  Ring<kR2N> r2hist_;
  Span span_;
  bool open_ = false;
};

}  // namespace lff
}  // namespace bll
