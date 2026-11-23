// Host tests for lowfreq_forest_core.h (no Arduino, no hardware).
//   test_lowfreq_forest_core.exe            self-checks
//   test_lowfreq_forest_core.exe --replay   stdin: "t_ms ch1 ch2 arm" per 250 ms window
//                                           stdout: "W p active" per window, then "S t0 t1 peak n"
// The replay mode lets host/ad3_bridge/test_lowfreq_forest.py and
// analysis/ad3_label_review/check_forest_core.py compare the MCU core with the PC reference.
#include <cmath>
#include <cstdio>
#include <cstring>

#include "lowfreq_forest_core.h"

using bll::lff::Result;
using bll::lff::Span;
using bll::lff::Tracker;

static int failures = 0, checks = 0;
#define CHECK(cond)                                                     \
  do {                                                                  \
    ++checks;                                                           \
    if (!(cond)) {                                                      \
      ++failures;                                                       \
      std::printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #cond);        \
    }                                                                   \
  } while (0)

// deterministic small noise around 1e-4 V
static float noise(unsigned i, unsigned salt) {
  unsigned x = i * 2654435761u + salt * 40503u;
  x ^= x >> 13;
  return 1e-4f * (1.0f + 0.1f * static_cast<float>(x % 1000) / 1000.0f);
}

static void selfChecks() {
  {  // no output before 10 s of history
    Tracker tr;
    for (unsigned i = 0; i < 40; ++i) {
      Result r = tr.push(i * 250 + 125, noise(i, 1), noise(i, 2), false);
      CHECK(!r.usable && !r.active && std::isnan(r.p));
    }
    Result r = tr.push(40 * 250 + 125, noise(40, 1), noise(40, 2), false);
    CHECK(r.usable);
  }
  {  // a CH2 burst is marked; the span closes after the 1 s gap
    Tracker tr;
    bool sawActive = false, closed = false;
    Span s;
    for (unsigned i = 0; i < 400; ++i) {
      float ch2 = noise(i, 2) * ((i >= 200 && i < 212) ? 12.0f : 1.0f);
      Result r = tr.push(i * 250 + 125, noise(i, 1), ch2, false);
      if (r.active) { sawActive = true; CHECK(i >= 200 && i < 230); }
      if (r.span_closed) { closed = true; s = r.closed; }
    }
    CHECK(sawActive);
    CHECK(closed);
    CHECK(s.t0_ms >= 50000 && s.t0_ms <= 53000 && s.windows > 0 && s.peak_p >= LFF_THRESHOLD);
    Span rest;
    CHECK(!tr.flush(&rest));
  }
  {  // the arm windows are never marked and never enter the floor
    Tracker tr;
    for (unsigned i = 0; i < 400; ++i) {
      bool arm = i >= 196 && i < 216;
      float ch2 = noise(i, 2) * ((i >= 200 && i < 212) ? 12.0f : 1.0f);
      Result r = tr.push(i * 250 + 125, noise(i, 1), ch2, arm);
      if (arm) CHECK(!r.usable && !r.active);
    }
  }
  {  // an open span is returned by flush at the end
    Tracker tr;
    Span s;
    for (unsigned i = 0; i < 206; ++i) {
      float ch2 = noise(i, 2) * (i >= 200 ? 12.0f : 1.0f);
      tr.push(i * 250 + 125, noise(i, 1), ch2, false);
    }
    CHECK(tr.flush(&s));
    CHECK(s.t1_ms >= 51000);
  }
  {  // no heap, fixed size (fits easily in MCU RAM)
    CHECK(sizeof(Tracker) < 2048);
  }
}

static int replay() {
  Tracker tr;
  unsigned long t;
  float ch1, ch2;
  int arm;
  Span spans[4096];
  unsigned n = 0;
  while (std::scanf("%lu %f %f %d", &t, &ch1, &ch2, &arm) == 4) {
    Result r = tr.push(static_cast<uint32_t>(t), ch1, ch2, arm != 0);
    if (r.span_closed && n < 4096) spans[n++] = r.closed;
    std::printf("W %.6f %d\n", r.usable ? r.p : -1.0f, r.active ? 1 : 0);
  }
  Span last;
  if (tr.flush(&last) && n < 4096) spans[n++] = last;
  for (unsigned i = 0; i < n; ++i)
    std::printf("S %lu %lu %.4f %u\n", static_cast<unsigned long>(spans[i].t0_ms),
                static_cast<unsigned long>(spans[i].t1_ms), spans[i].peak_p, spans[i].windows);
  return 0;
}

int main(int argc, char** argv) {
  if (argc > 1 && std::strcmp(argv[1], "--replay") == 0) return replay();
  selfChecks();
  std::printf("%s: %d checks, %d failures (sizeof Tracker %u bytes)\n", bll::lff::kVersion, checks,
              failures, static_cast<unsigned>(sizeof(Tracker)));
  return failures ? 1 : 0;
}
