#include "servo_driver.h"
#ifdef BLL_EVIDENCE_STORE
#include <Arduino.h>
#include <esp_timer.h>

#include "config.h"

namespace {
constexpr uint8_t kChannel = 0;
constexpr uint32_t kFrameHz = 50;
constexpr uint8_t kResolutionBits = 14;
constexpr uint32_t kPeriodUs = 1000000UL / kFrameHz;

// The ramp runs on an esp_timer (esp_timer task, not loopTask), one step per 20 ms
// frame. Until 0.14.0 it advanced only when loop() ran, so a candidate being stored
// (duplicate scan + commits, ~4.6 s each on 2026-11-11) froze the arm mid-park and the
// park timed out with the arm limp at ~135 deg. State shared with the timer callback
// is guarded by this spinlock; LEDC writes happen inside it so servoOff() cannot race.
portMUX_TYPE lock = portMUX_INITIALIZER_UNLOCKED;
esp_timer_handle_t rampTimer = nullptr;
volatile bool attached = false;
bool everMoved = false;
volatile uint16_t currentUs = 0;  // 0 = unknown (nothing commanded this boot)
volatile uint16_t targetUs = 0;
volatile uint32_t lastMotionMs = 0;
volatile uint32_t motionStartMs = 0;  // millis() when the current/last motion began

uint32_t dutyFor(uint16_t us) {
  return (static_cast<uint32_t>(us) * ((1UL << kResolutionBits) - 1)) / kPeriodUs;
}

void rampTick(void*) {
  portENTER_CRITICAL(&lock);
  if (attached && currentUs != targetUs) {
    const int32_t diff = static_cast<int32_t>(targetUs) - currentUs;
    const int32_t stepUs = config::kServoRampUsPerFrame;
    const int32_t step = stepUs == 0 ? diff : (diff > 0 ? min<int32_t>(diff, stepUs) : max<int32_t>(diff, -stepUs));
    currentUs = static_cast<uint16_t>(currentUs + step);
    ledcWrite(kChannel, dutyFor(currentUs));
    lastMotionMs = millis();
  }
  portEXIT_CRITICAL(&lock);
}
}  // namespace

void servoBegin() {
  pinMode(config::kServoPin, OUTPUT);
  digitalWrite(config::kServoPin, LOW);
  if (!rampTimer) {
    esp_timer_create_args_t args = {};
    args.callback = rampTick;
    args.dispatch_method = ESP_TIMER_TASK;
    args.name = "servo_ramp";
    if (esp_timer_create(&args, &rampTimer) == ESP_OK) esp_timer_start_periodic(rampTimer, kPeriodUs);
    else rampTimer = nullptr;
  }
}

bool servoMoveTo(uint16_t us) {
  if (us < config::kServoHardMinUs || us > config::kServoHardMaxUs) return false;
  if (!rampTimer) return false;  // no ramp available: never drive the servo unramped
  if (!attached) {
    ledcSetup(kChannel, kFrameHz, kResolutionBits);
    ledcAttachPin(config::kServoPin, kChannel);
    // The real position is unknown after boot or servoOff(): the first pulse goes straight
    // to the target, so the servo moves at its own speed (cannot be ramped).
    portENTER_CRITICAL(&lock);
    if (currentUs == 0) currentUs = us;
    ledcWrite(kChannel, dutyFor(currentUs));
    attached = true;
    portEXIT_CRITICAL(&lock);
  }
  portENTER_CRITICAL(&lock);
  if (currentUs == targetUs || targetUs == 0) motionStartMs = millis();  // a new motion (not a retarget)
  targetUs = us;
  lastMotionMs = millis();
  portEXIT_CRITICAL(&lock);
  everMoved = true;
  return true;
}

// Kept for callers; the ramp now advances on its own timer.
void servoUpdate() {}

void servoOff() {
  portENTER_CRITICAL(&lock);
  const bool was = attached;
  attached = false;  // the timer stops writing from here on
  currentUs = 0;     // a limp servo may be pushed anywhere
  targetUs = 0;
  if (was) ledcWrite(kChannel, 0);
  portEXIT_CRITICAL(&lock);
  if (was) ledcDetachPin(config::kServoPin);
  pinMode(config::kServoPin, OUTPUT);
  digitalWrite(config::kServoPin, LOW);
}

bool servoMoving() {
  portENTER_CRITICAL(&lock);
  const bool moving = attached && currentUs != targetUs;
  portEXIT_CRITICAL(&lock);
  return moving;
}
bool servoAttached() { return attached; }
uint16_t servoCurrentUs() { return currentUs; }
uint16_t servoTargetUs() { return targetUs; }
uint32_t servoLastMotionMs() { return lastMotionMs; }
uint32_t servoMotionStartMs() { return motionStartMs; }
bool servoEverMoved() { return everMoved; }
#endif
