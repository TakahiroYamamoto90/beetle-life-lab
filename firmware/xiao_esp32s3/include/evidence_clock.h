#pragma once
#include <cstdint>
#include <cstdio>
#include <cstring>

constexpr uint64_t kClockMinUtcMs = 1577836800000ULL;  // 2020-01-01
constexpr uint64_t kClockMaxUtcMs = 4102444800000ULL;  // 2100-01-01 exclusive
constexpr uint64_t kClockMaxAgeMs = 3600000;

struct EvidenceClock {
  uint64_t utcMs = 0, monoMs = 0;
  uint32_t transportUncertaintyMs = 0;
  char syncId[33] = {}, eventId[100] = {};
  bool valid = false;
};

inline bool evidenceUtcText(uint64_t ms, char* output, size_t size) {
  if (ms < kClockMinUtcMs || ms >= kClockMaxUtcMs || size < 25) return false;
  // The pinned Arduino core uses 32-bit time_t. Format the bounded 2020..2099
  // range directly, without changing libc ABI or overflowing at 2038.
  auto leap = [](unsigned y) { return y % 4 == 0 && (y % 100 != 0 || y % 400 == 0); };
  unsigned days = static_cast<unsigned>((ms - kClockMinUtcMs) / 86400000);
  unsigned year = 2020, month = 1;
  while (days >= 365U + leap(year)) { days -= 365U + leap(year); ++year; }
  const unsigned months[] = {31,28,31,30,31,30,31,31,30,31,30,31};
  while (days >= months[month - 1] + (month == 2 && leap(year))) {
    days -= months[month - 1] + (month == 2 && leap(year)); ++month;
  }
  unsigned seconds = static_cast<unsigned>((ms / 1000) % 86400);
  return snprintf(output, size, "%04u-%02u-%02uT%02u:%02u:%02u.%03uZ", year, month, days + 1,
                  seconds / 3600, seconds / 60 % 60, seconds % 60, static_cast<unsigned>(ms % 1000)) == 24;
}

inline bool evidenceUtcValid(const char* text) {
  if (!text || strlen(text) != 24) return false;
  for (unsigned i = 0; i < 24; ++i) {
    char separator = i == 4 || i == 7 ? '-' : i == 10 ? 'T' :
                     i == 13 || i == 16 ? ':' : i == 19 ? '.' : i == 23 ? 'Z' : 0;
    if (separator ? text[i] != separator : text[i] < '0' || text[i] > '9') return false;
  }
  int y, m, d, h, minute, s, ms;
  if (sscanf(text, "%4d-%2d-%2dT%2d:%2d:%2d.%3dZ", &y, &m, &d, &h, &minute, &s, &ms) != 7)
    return false;
  const int days[] = {31,28,31,30,31,30,31,31,30,31,30,31};
  if (y < 2020 || y >= 2100 || m < 1 || m > 12 || d < 1 || h > 23 || minute > 59 || s > 59)
    return false;
  return d <= days[m - 1] + (m == 2 && y % 4 == 0 && (y % 100 != 0 || y % 400 == 0));
}
