#pragma once
// Hobby servo on XIAO D2 (DS3218MG 270 deg, external 5 V supply, common GND).
// 50 Hz LEDC pulse train, clamped to hard limits, ramped moves. No position sensor:
// "reached" only means the commanded pulse reached its target (POSE_ASSUMED at best).
// Evidence profile only; drives nothing until the first move command after boot.
#include <cstdint>

#ifdef BLL_EVIDENCE_STORE
void servoBegin();                   // pin LOW, no pulses
bool servoMoveTo(uint16_t us);       // false if outside the hard limits
void servoUpdate();                  // no-op since 0.14.1: the ramp runs on an esp_timer at 50 Hz
void servoOff();                     // stop pulses now (servo goes limp)
bool servoMoving();
bool servoAttached();
uint16_t servoCurrentUs();
uint16_t servoTargetUs();
uint32_t servoLastMotionMs();        // millis() of the last ramp step (0 = never moved)
uint32_t servoMotionStartMs();       // millis() when the current/last motion was commanded
bool servoEverMoved();
#endif
