// Servo bench test firmware (env:xiao_servo_test). Standalone: no Evidence store, no
// LittleFS access, no Vision, no ADC. Drives one hobby servo on XIAO D2 with 50 Hz LEDC.
//
// Safety model:
// - No pulses are output until the operator sends a move command (pin held LOW).
// - Every target is clamped to soft limits (default 1000..2000 us, about +/-67 deg around
//   the 1500 us centre of a 270 deg servo); limits can be widened to at most 500..2500 us.
// - Moves are ramped (default 10 us per 20 ms frame, about 67 deg/s) to limit current peaks.
// - "servo-off" (or "stop") stops the pulse train immediately.
#include <Arduino.h>

namespace {

constexpr char kVersion[] = "servo-test-0.1.0";
constexpr uint8_t kServoPin = D2;
constexpr uint8_t kChannel = 0;
constexpr uint32_t kFrameHz = 50;
constexpr uint8_t kResolutionBits = 14;
constexpr uint32_t kPeriodUs = 1000000UL / kFrameHz;
constexpr uint16_t kHardMinUs = 500;
constexpr uint16_t kHardMaxUs = 2500;
constexpr uint16_t kCentreUs = 1500;

uint16_t softMinUs = 1000;
uint16_t softMaxUs = 2000;
uint16_t rampStepUs = 10;   // per frame; 0 = jump
bool attached = false;
uint16_t currentUs = kCentreUs;
uint16_t targetUs = kCentreUs;
uint32_t lastFrameMs = 0;
uint32_t moveStartMs = 0;
bool moving = false;

// Sweep state.
bool sweeping = false;
uint16_t sweepA = 0, sweepB = 0;
uint32_t sweepDwellMs = 0;
uint8_t sweepLeg = 0, sweepLegs = 0;
uint32_t dwellUntilMs = 0;

String line;

uint32_t dutyFor(uint16_t us) {
  return (static_cast<uint32_t>(us) * ((1UL << kResolutionBits) - 1)) / kPeriodUs;
}

// 270 deg servo: 500 us = 0 deg, 2500 us = 270 deg (nominal; not calibrated).
float degFor(uint16_t us) { return (static_cast<float>(us) - kHardMinUs) * 270.0f / (kHardMaxUs - kHardMinUs); }
uint16_t usForDeg(float deg) { return static_cast<uint16_t>(kHardMinUs + deg * (kHardMaxUs - kHardMinUs) / 270.0f + 0.5f); }

void printState(const char* event) {
  Serial.printf("SERVO_STATE {\"event\":\"%s\",\"attached\":%s,\"current_us\":%u,\"target_us\":%u,"
                "\"nominal_deg\":%.1f,\"moving\":%s,\"sweeping\":%s,\"soft_min_us\":%u,\"soft_max_us\":%u,"
                "\"ramp_us_per_frame\":%u,\"uptime_ms\":%lu}\n",
                event, attached ? "true" : "false", currentUs, targetUs, degFor(currentUs),
                moving ? "true" : "false", sweeping ? "true" : "false", softMinUs, softMaxUs, rampStepUs,
                static_cast<unsigned long>(millis()));
}

void detach() {
  if (attached) {
    ledcWrite(kChannel, 0);
    ledcDetachPin(kServoPin);
  }
  pinMode(kServoPin, OUTPUT);
  digitalWrite(kServoPin, LOW);
  attached = false;
  moving = false;
  sweeping = false;
}

void attachIfNeeded() {
  if (attached) return;
  ledcSetup(kChannel, kFrameHz, kResolutionBits);
  ledcAttachPin(kServoPin, kChannel);
  // First pulse goes straight to currentUs (the last commanded position, or centre
  // after boot); the servo may jump there because its real position is unknown.
  ledcWrite(kChannel, dutyFor(currentUs));
  attached = true;
}

uint16_t clampSoft(long us) {
  if (us < softMinUs) return softMinUs;
  if (us > softMaxUs) return softMaxUs;
  return static_cast<uint16_t>(us);
}

void moveTo(long requestedUs, const char* why) {
  const uint16_t us = clampSoft(requestedUs);
  if (us != requestedUs) Serial.printf("SERVO_CLAMPED requested_us=%ld applied_us=%u\n", requestedUs, us);
  attachIfNeeded();
  targetUs = us;
  if (rampStepUs == 0) { currentUs = us; ledcWrite(kChannel, dutyFor(currentUs)); }
  moving = currentUs != targetUs;
  moveStartMs = millis();
  printState(why);
}

void frame() {
  const uint32_t now = millis();
  if (now - lastFrameMs < 1000 / kFrameHz) return;
  lastFrameMs = now;
  if (attached && currentUs != targetUs) {
    const int32_t diff = static_cast<int32_t>(targetUs) - currentUs;
    const int32_t step = rampStepUs == 0 ? diff : (diff > 0 ? min<int32_t>(diff, rampStepUs) : max<int32_t>(diff, -rampStepUs));
    currentUs = static_cast<uint16_t>(currentUs + step);
    ledcWrite(kChannel, dutyFor(currentUs));
    if (currentUs == targetUs) {
      moving = false;
      Serial.printf("SERVO_REACHED_COMMAND us=%u ramp_ms=%lu (command only; real position not sensed)\n",
                    currentUs, static_cast<unsigned long>(now - moveStartMs));
      if (sweeping) dwellUntilMs = now + sweepDwellMs;
    }
  }
  if (sweeping && !moving && static_cast<int32_t>(now - dwellUntilMs) >= 0) {
    if (sweepLeg >= sweepLegs) { sweeping = false; printState("sweep_done"); return; }
    ++sweepLeg;
    moveTo((sweepLeg % 2) ? sweepB : sweepA, "sweep_leg");
  }
}

void help() {
  Serial.println(
      "commands:\n"
      "  info | state\n"
      "  servo-us <us>            move (clamped to soft limits, ramped)\n"
      "  servo-deg <deg>          nominal 0..270 deg (500..2500 us, uncalibrated)\n"
      "  servo-center             1500 us\n"
      "  servo-off | stop         stop pulses now\n"
      "  servo-ramp <us/frame>    0 = jump, default 10 (50 frames/s)\n"
      "  servo-limits <min> <max> soft limits within 500..2500 us\n"
      "  servo-sweep <a> <b> <cycles> <dwell_ms>  a->b->a ... (cycles <= 20)");
}

void handle(String cmd) {
  cmd.trim();
  if (!cmd.length()) return;
  char buf[96];
  cmd.toCharArray(buf, sizeof(buf));
  char* name = strtok(buf, " ");
  char* a1 = strtok(nullptr, " ");
  char* a2 = strtok(nullptr, " ");
  char* a3 = strtok(nullptr, " ");
  char* a4 = strtok(nullptr, " ");
  const String c(name);
  if (c == "help") help();
  else if (c == "info") {
    Serial.printf("SERVO_INFO fw=%s pin=D2 gpio=%u frame_hz=%lu resolution_bits=%u hard_range_us=%u..%u\n",
                  kVersion, kServoPin, static_cast<unsigned long>(kFrameHz), kResolutionBits, kHardMinUs, kHardMaxUs);
    printState("info");
  } else if (c == "state") printState("state");
  else if (c == "servo-off" || c == "stop") { detach(); printState("off"); }
  else if (c == "servo-center") { sweeping = false; moveTo(kCentreUs, "center"); }
  else if (c == "servo-us" && a1) { sweeping = false; moveTo(atol(a1), "move"); }
  else if (c == "servo-deg" && a1) { sweeping = false; moveTo(usForDeg(atof(a1)), "move_deg"); }
  else if (c == "servo-ramp" && a1) { rampStepUs = static_cast<uint16_t>(constrain(atol(a1), 0, 200)); printState("ramp"); }
  else if (c == "servo-limits" && a1 && a2) {
    const long lo = atol(a1), hi = atol(a2);
    if (lo < kHardMinUs || hi > kHardMaxUs || lo >= hi) { Serial.println("SERVO_ERROR limits must satisfy 500 <= min < max <= 2500"); return; }
    softMinUs = static_cast<uint16_t>(lo); softMaxUs = static_cast<uint16_t>(hi);
    printState("limits");
  } else if (c == "servo-sweep" && a1 && a2 && a3 && a4) {
    const long cycles = atol(a3);
    if (cycles < 1 || cycles > 20) { Serial.println("SERVO_ERROR cycles must be 1..20"); return; }
    sweepA = clampSoft(atol(a1)); sweepB = clampSoft(atol(a2));
    sweepDwellMs = static_cast<uint32_t>(constrain(atol(a4), 0, 10000));
    sweepLegs = static_cast<uint8_t>(cycles * 2); sweepLeg = 0;
    sweeping = true;
    moveTo(sweepA, "sweep_start");
  } else Serial.printf("SERVO_ERROR unknown or incomplete command: %s (try help)\n", cmd.c_str());
}

}  // namespace

void setup() {
  pinMode(kServoPin, OUTPUT);
  digitalWrite(kServoPin, LOW);  // no pulses until commanded
  Serial.begin(115200);
  const uint32_t start = millis();
  while (!Serial && millis() - start < 3000) delay(10);
  Serial.printf("SERVO_TEST_READY fw=%s pin=D2 (no pulses until a move command)\n", kVersion);
  help();
}

void loop() {
  while (Serial.available()) {
    const char ch = static_cast<char>(Serial.read());
    if (ch == '\n' || ch == '\r') { handle(line); line = ""; }
    else if (line.length() < 90) line += ch;
  }
  frame();
}
