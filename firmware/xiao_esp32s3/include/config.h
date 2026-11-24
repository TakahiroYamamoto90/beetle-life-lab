#pragma once

#include <Arduino.h>

namespace config {
#ifdef BLL_EVIDENCE_STORE
constexpr char kFirmwareVersion[] = "0.18.2-observation-hold";  // 0.18.2 (2026-11-24, user decision): "observe-mode hold" = follow, and after an episode the arm waits at the view the next trigger would look at again (observation_core.h holdArmAt) instead of parking at FRONT; it parks once that no longer applies (120 s, 3 in a row, or lost there). The decision config adds hold_where_larva_seen. Not one-way. 0.18.1 (2026-11-24, user decision): "observe-mode follow|explore" switches the 0.18.0 rule per acquisition (RAM only, follow after boot; the bridge sends it when forwarding); the decision config records stay_where_larva_seen; observe-status shows mode. Not one-way. 0.18.0 (2026-11-24, user decision): a new trigger first looks again at the view where the Vision model last classed the larva VISIBLE_LARVA (within 120 s, at most 3 times in a row; reason LARVA_LAST_SEEN_HERE); if it is not seen there the episode explores the other views. Reducer temporal-reducer-v3, policy observation-policy-v4; the decision config adds stay_where_larva_seen, larva_stay_ms, max_larva_stays. No new event type; not one-way (reason codes and config are free-form for older firmware and the PC). 0.17.0 (2026-11-23, user decision): an episode stops after a capture whose image the Vision model classes as VISIBLE_LARVA with score >= 80 (OBSERVATION_ACTION_RESULT result.vision; old firmware ignores the field), and passive-candidate may name its detector LOWFREQ_FOREST_RATIO (PC low-frequency forest or 1-40 Hz ratio >= 3). ONE-WAY: firmware before 0.17.0 rejects such PASSIVE_VIBRATION_CANDIDATE events. 0.16.0: also builds for the Arduino Nano ESP32 (env nano_evidence; device_id "nano-..." on that board, Evidence schema widened to accept it). XIAO behaviour unchanged; not one-way. 0.15.1: the low-frequency forest also masks moves inside an observation step (servo start/last-step times + settle; 0.15.0 masked only parks). 0.15.0: lowfreq-* commands run the low-frequency forest on the RMS windows the PC sends (reference only; no Evidence write, policy unchanged; not one-way). 0.14.6: evidence-rotate (empty the log after a hash-verified PC export, then restart; not one-way). 0.14.5: 0.14.4 + Evidence log 20 segments (was 16; firmware before 0.14.5 ignores segments 17-20); 0.14.4: BOOT clears the consecutive capture-failure stop
#elif defined(BLL_PASSIVE_ADC_FRONTEND_VERIFIED)
constexpr char kFirmwareVersion[] = "0.6.3-piezo-streaming-8khz-vision-classify";
#elif defined(BLL_VISION_BRINGUP)
constexpr char kFirmwareVersion[] = "0.3.0-vision-capture";
#else
constexpr char kFirmwareVersion[] = "0.1.0-bringup";
#endif
// ---- board (2026-11-23): XIAO ESP32S3 or Arduino Nano ESP32, same sources ----
#ifdef BLL_BOARD_NANO_ESP32
#ifndef BOARD_USES_HW_GPIO_NUMBERS
#error "Nano ESP32 builds need -DBOARD_USES_HW_GPIO_NUMBERS (see platformio.ini)"
#endif
// Pin numbers from the core's variant arduino_nano_nora/pins_arduino.h (GPIO numbering).
static_assert(D2 == 5 && A4 == 11 && A5 == 12 && A0 == 1, "unexpected Nano ESP32 pin map");
constexpr char kBoardName[] = "arduino_nano_esp32";
constexpr char kDevicePrefix[] = "nano";      // Evidence device_id nano-<efuse mac>
constexpr uint8_t kI2cSdaPin = A4;            // GPIO11, Grove Vision AI V2 SDA (white)
constexpr uint8_t kI2cSclPin = A5;            // GPIO12, Grove Vision AI V2 SCL (yellow)
constexpr bool kLedActiveLow = false;         // D13 / GPIO48, LED on at HIGH
// Stock partition table app3M_fat9M_fact512k_16MB.csv: data partition "ffat" (FAT subtype,
// mounted as LittleFS here; never formatted automatically, see evidenceInitEmpty()).
constexpr char kEvidencePartitionLabel[] = "ffat";
constexpr uint32_t kEvidencePartitionAddress = 0x610000;
constexpr uint32_t kEvidencePartitionSize = 0x960000;
#else
constexpr char kBoardName[] = "seeed_xiao_esp32s3";
constexpr char kDevicePrefix[] = "xiao";
constexpr uint8_t kI2cSdaPin = D4;            // GPIO5 via the Grove Shield
constexpr uint8_t kI2cSclPin = D5;            // GPIO6
constexpr bool kLedActiveLow = true;          // XIAO user LED is active low
// default_8MB.csv: "spiffs" 0x670000 / 0x180000 (back up with read_flash 0x670000 0x180000).
constexpr char kEvidencePartitionLabel[] = "spiffs";
constexpr uint32_t kEvidencePartitionAddress = 0x670000;
constexpr uint32_t kEvidencePartitionSize = 0x180000;
#endif
constexpr uint32_t kSerialBaud = 115200;
constexpr uint32_t kLedIntervalMs = 500;
constexpr uint32_t kHeartbeatIntervalMs = 2000;
#ifdef BLL_EVIDENCE_STORE
constexpr size_t kMaxLineLength = 256;
#else
constexpr size_t kMaxLineLength = 128;
#endif
#ifdef BLL_PASSIVE_ADC_FRONTEND_VERIFIED
// ADC1_CH0 = GPIO1 (piezo_adc.cpp): XIAO D0/A0, Nano ESP32 A0. Protection must be verified first.
#ifdef BLL_BOARD_NANO_ESP32
constexpr uint8_t kPassiveAdcPin = A0;
#else
constexpr uint8_t kPassiveAdcPin = D0;
#endif
#endif
#ifdef BLL_EVIDENCE_STORE
// Camera arm: DS3218MG 270 deg on D2 (XIAO GPIO3 / Nano GPIO5), external 5 V / 3 A, GND common (2026-11-08).
constexpr uint8_t kServoPin = D2;
constexpr uint16_t kServoHardMinUs = 500;   // nominal 0 deg of the servo
constexpr uint16_t kServoHardMaxUs = 2500;  // nominal 270 deg of the servo
constexpr uint16_t kServoRampUsPerFrame = 10;  // 50 frames/s -> about 67 deg/s
// Arm poses: FRONT = 0 deg (home, the former fixed-camera view), TOP = 90 deg, BACK = 180 deg.
// Pulse widths must be measured on the real arm (no 0/90/180 -> PWM shortcut).
// 0 = not calibrated: that view is never offered to the policy. Index: FRONT, TOP, BACK.
// "servo-cal <FRONT|TOP|BACK> <us>" overrides these in RAM until reboot.
// Measured 2026-11-10 (arm remounted, servo lying flat, phone protractor; no position
// sensor): 890 us = 0 deg, 1535 us = 88-89 deg, 2215 us = 181-182 deg, held 30 s at BACK
// without hunting. Re-measure after any change to the arm spline or mounting.
constexpr uint16_t kServoPoseUs[3] = {890, 1535, 2215};
// A new trigger arriving within this time after the last servo motion may be the arm's
// own vibration and waits (SELF_ACTUATION_RECENT). Provisional; AD3 detection latency is
// not measured.
constexpr uint32_t kSelfActuationGuardMs = 10000;
// Image AI stop (0.17.0, user decision 2026-11-23): the deployed model's classes are
// NO_VISIBLE_LARVA (0) and VISIBLE_LARVA (1); the PC checks the model name and classes per run.
// A capture whose top class is VISIBLE_LARVA with score >= 80 (0-100) ends the episode: the
// remaining views (FRONT -> TOP -> BACK) are not visited. bll::PolicyConfig::stopWhenLarvaVisible
// turns the rule off. This is the model's judgement of one image, not a verified detection.
constexpr int16_t kVisionLarvaClassId = 1;
constexpr int16_t kVisionLarvaMinScore = 80;
#endif
}  // namespace config

