#include <Arduino.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <cstring>
#include <cstdlib>
#include <cctype>
#include <cerrno>

#include "config.h"
#include "vision_sscma.h"
#include "piezo_adc.h"
#include "evidence_store.h"
#include "observation_runtime.h"
#include "lowfreq_runtime.h"

#ifdef BLL_EVIDENCE_STORE
// The observation step runs candidate storage, two Flash commits and a Vision
// capture on loopTask. 0.10.0 measured only 2,540 bytes free of the default 8 KiB
// after boot replay, so give loopTask 16 KiB (PSRAM board, ample internal RAM).
SET_LOOP_TASK_STACK_SIZE(16 * 1024);
#endif

namespace {
char line[config::kMaxLineLength + 1];
size_t lineLength = 0;
bool discarding = false;
bool ledOn = false;
bool wasConnected = false;
uint32_t lastLedMs = 0;
uint32_t lastHeartbeatMs = 0;
uint32_t lastLineActivityMs = 0;

#ifdef BLL_BOARD_NANO_ESP32
// Nano ESP32: Serial is TinyUSB CDC, whose operator bool needs DTR and RTS. The host tools
// open the port with DTR on and RTS off (on the XIAO's USB Serial/JTAG, RTS resets the chip),
// so a host counts as connected on DTR alone, tracked from the line-state events. USB writes
// and reads themselves need DTR only (2026-11-23, found on the first Nano bring-up).
volatile bool cdcDtr = false;
void onCdcEvent(void*, esp_event_base_t, int32_t id, void* data) {
  if (id == ARDUINO_USB_CDC_LINE_STATE_EVENT)
    cdcDtr = static_cast<arduino_usb_cdc_event_data_t*>(data)->line_state.dtr;
}
bool hostConnected() { return cdcDtr; }
#else
bool hostConnected() { return static_cast<bool>(Serial); }
#endif

void printStatus() {
  Serial.printf("STATUS fw=%s chip=%s revision=%u cpu_mhz=%u flash_bytes=%u "
                "psram_bytes=%u free_heap_bytes=%u uptime_ms=%lu reset_reason=%d "
                "loop_stack_min_free_bytes=%u board=%s\n",
                config::kFirmwareVersion, ESP.getChipModel(), ESP.getChipRevision(),
                ESP.getCpuFreqMHz(), ESP.getFlashChipSize(), ESP.getPsramSize(),
                ESP.getFreeHeap(), static_cast<unsigned long>(millis()),
                static_cast<int>(esp_reset_reason()),
                static_cast<unsigned>(uxTaskGetStackHighWaterMark(nullptr)), config::kBoardName);
}

#ifdef BLL_VISION_BRINGUP
bool isVisionCommand(const char* text) {
  return std::strcmp(text, "i2c") == 0 || std::strcmp(text, "vision") == 0 ||
         std::strcmp(text, "vision-invoke") == 0 || std::strcmp(text, "vision-sample") == 0 ||
         std::strcmp(text, "vision-classify-capture") == 0 ||
         std::strncmp(text, "vision-sensor", 13) == 0;
}
#endif

void handleLine() {
  line[lineLength] = '\0';
  if (std::strcmp(line, "ping") == 0) {
    Serial.println("pong");
  } else if (std::strcmp(line, "info") == 0) {
    // Read-only: frequent host status checks must not consume evidence storage.
    printStatus();
#ifdef BLL_EVIDENCE_STORE
  } else if (std::strncmp(line, "passive-candidate ", 18) == 0 ||
             std::strncmp(line, "passive-check ", 14) == 0) {
    if (piezoAdcIsRunning()) Serial.println("PASSIVE_ERROR reason=adc_busy");
    else if (std::strncmp(line, "passive-check ", 14) == 0) evidencePassive(line + 14, true);
    else evidencePassive(line + 18, false);
  } else if (std::strncmp(line, "evidence-", 9) == 0) {
    if (piezoAdcIsRunning()) {
      Serial.println("EVIDENCE_ERROR reason=adc_busy");
    } else if (std::strcmp(line, "evidence-status") == 0) {
      evidenceStatus();
    } else if (std::strncmp(line, "evidence-clock-probe ", 21) == 0) {
      evidenceClockProbe(line + 21);
    } else if (std::strncmp(line, "evidence-time-sync ", 19) == 0) {
      evidenceClockSync(line + 19);
    } else if (std::strcmp(line, "evidence-diag") == 0) {
      Serial.printf("EVIDENCE_DIAG status=%s\n", evidenceDiagnostic() ? "stored" : "not_stored");
      evidenceStatus();
    } else if (std::strncmp(line, "evidence-rotate ", 16) == 0) {
      evidenceRotate(line + 16);
    } else if (std::strcmp(line, "evidence-init-empty") == 0) {
      evidenceInitEmpty();
    } else if (std::strncmp(line, "evidence-read ", 14) == 0) {
      const char* value = line + 14;
      bool digits = *value != '\0';
      for (const char* p = value; *p; ++p) if (!std::isdigit(static_cast<unsigned char>(*p))) digits = false;
      errno = 0; char* end = nullptr;
      unsigned long long index = std::strtoull(value, &end, 10);
      if (!digits || errno == ERANGE || *end || index > UINT32_MAX)
        Serial.println("EVIDENCE_ERROR reason=invalid_index");
      else evidenceReadPage(static_cast<uint32_t>(index));
    } else {
      Serial.println("EVIDENCE_ERROR reason=unknown_command");
    }
  } else if (std::strncmp(line, "servo-", 6) == 0) {
    observationServoCommand(line);
  } else if (std::strncmp(line, "lowfreq-", 8) == 0) {
    lowfreqCommand(line);  // reference only: no Evidence write, not used by the policy
  } else if (std::strcmp(line, "observe-status") == 0) {
    observationStatus();  // read-only, allowed during ADC streaming
  } else if (std::strncmp(line, "observe-", 8) == 0) {
    if (piezoAdcIsRunning()) Serial.println("OBSERVATION_ERROR reason=adc_busy");
    else if (std::strcmp(line, "observe-run") == 0) observationRun();
    else if (std::strncmp(line, "observe-light ", 14) == 0) observationSetLight(line + 14);
    else if (std::strncmp(line, "observe-vision ", 15) == 0) observationSetVision(line + 15);
    else if (std::strncmp(line, "observe-mode ", 13) == 0) observationSetMode(line + 13);
    else Serial.println("OBSERVATION_ERROR reason=unknown_command");
#endif
#ifdef BLL_VISION_BRINGUP
  } else if (isVisionCommand(line) && piezoAdcIsRunning()) {
    // Vision requests block for seconds and would overrun the ADC DMA buffer.
    Serial.println("VISION_BUSY reason=adc_running label=unknown");
  } else if (std::strcmp(line, "i2c") == 0) {
    visionScan();
  } else if (std::strcmp(line, "vision") == 0) {
    visionInspect();
  } else if (std::strcmp(line, "vision-invoke") == 0) {
    visionInvokeOnce();
  } else if (std::strcmp(line, "vision-classify-capture") == 0) {
    visionClassifyCaptureOnce();
  } else if (std::strcmp(line, "vision-sample") == 0) {
    visionSampleOnce();
  } else if (std::strcmp(line, "vision-sensor") == 0) {
    visionSensorQuery();
  } else if (std::strncmp(line, "vision-sensor-set ", 18) == 0) {
    unsigned id = 0, opt = 0;
    char extra = 0;
    if (std::sscanf(line + 18, "%u %u %c", &id, &opt, &extra) == 2 && id >= 1 && id <= 8 && opt <= 15)
      visionSensorSet(id, opt);
    else
      Serial.println("VISION_ERROR reason=usage_vision-sensor-set_<id 1-8>_<opt 0-15>");
#endif
  } else if (std::strcmp(line, "adc-start") == 0) {
    piezoAdcStart(0);
  } else if (std::strncmp(line, "adc-start ", 10) == 0) {
    const char* value = line + 10;
    while (*value == ' ') ++value;
    if (*value == '\0') {
      Serial.println("ERROR adc-start_seconds_must_be_positive");
    } else {
      bool digitsOnly = true;
      for (const char* digit = value; *digit; ++digit) {
        if (!std::isdigit(static_cast<unsigned char>(*digit))) digitsOnly = false;
      }
      char* end = nullptr;
      errno = 0;
      const unsigned long long seconds = std::strtoull(value, &end, 10);
      if (!digitsOnly || end == value || *end != '\0' || errno == ERANGE ||
          seconds == 0 || seconds > UINT32_MAX) {
        Serial.println("ERROR adc-start_seconds_range=1..4294967295");
      } else {
        piezoAdcStart(static_cast<uint32_t>(seconds));
      }
    }
  } else if (std::strcmp(line, "adc-stop") == 0) {
    piezoAdcStop();
  } else if (lineLength != 0) {
    Serial.print("ECHO ");
    Serial.println(line);
  }
  lineLength = 0;
}

bool isCompleteCommand() {
  line[lineLength] = '\0';
  if (std::strcmp(line, "ping") == 0 || std::strcmp(line, "info") == 0 ||
      std::strcmp(line, "adc-stop") == 0) {
    return true;
  }
#ifdef BLL_VISION_BRINGUP
  // "vision" is a prefix of other commands, so it waits for a terminator or idle gap.
  return std::strcmp(line, "i2c") == 0 || std::strcmp(line, "vision-invoke") == 0 ||
         std::strcmp(line, "vision-sample") == 0 ||
         std::strcmp(line, "vision-classify-capture") == 0;
#else
  return false;
#endif
}

void pollSerial() {
  // Bound processing so continuous input cannot starve the LED/heartbeat.
  for (size_t count = 0; count < 64 && Serial.available(); ++count) {
    const char ch = static_cast<char>(Serial.read());
    // Serial monitors commonly send LF, CRLF, or CR on Enter.
    if (ch == '\n' || ch == '\r') {
      if (!discarding) {
        handleLine();
      }
      lineLength = 0;
      discarding = false;
    } else if (!discarding) {
      if (lineLength < config::kMaxLineLength) {
        line[lineLength++] = ch;
        lastLineActivityMs = millis();
        // Some serial consoles send the entered text without CR/LF. Execute
        // exact known commands as soon as the full token has arrived.
        if (isCompleteCommand()) handleLine();
      } else {
        Serial.printf("ERROR line_too_long max=%u\n", static_cast<unsigned>(config::kMaxLineLength));
        lineLength = 0;
        discarding = true;
      }
    }
  }
}

void executeAfterIdle() {
  // Retain no-newline Serial Monitor support for commands that are prefixes of
  // longer commands ("adc-start" / "adc-start <seconds>", "vision" / "vision-*").
  if (discarding || lineLength == 0 || millis() - lastLineActivityMs < 100) return;
  line[lineLength] = '\0';
#ifdef BLL_VISION_BRINGUP
  if (std::strcmp(line, "vision") == 0) { handleLine(); return; }
#endif
  if (lineLength < 9) return;
#ifdef BLL_EVIDENCE_STORE
  if (std::strncmp(line, "evidence-", 9) == 0 || std::strncmp(line, "observe-", 8) == 0 ||
      std::strncmp(line, "servo-", 6) == 0) {
    handleLine();
    return;
  }
#endif
  if (std::strncmp(line, "adc-start", 9) == 0 &&
      (lineLength == 9 || line[9] == ' ')) {
    handleLine();
  }
}
}  // namespace

void setup() {
  // The basic profile drives only the onboard LED; the Vision profile adds I2C.
  digitalWrite(LED_BUILTIN, config::kLedActiveLow ? HIGH : LOW);  // off
  pinMode(LED_BUILTIN, OUTPUT);
  Serial.begin(config::kSerialBaud);
#ifdef BLL_BOARD_NANO_ESP32
  Serial.onEvent(ARDUINO_USB_CDC_LINE_STATE_EVENT, onCdcEvent);
#endif
#ifdef BLL_EVIDENCE_STORE
  observationBegin();  // servo signal LOW before anything else
  evidenceBegin();
#endif
#ifdef BLL_VISION_BRINGUP
  visionBusBegin();
#endif
  // Do not wait for a host: blinking must work with no serial monitor open.
}

void loop() {
  const uint32_t now = millis();
  if (now - lastLedMs >= config::kLedIntervalMs) {
    lastLedMs = now;
    ledOn = !ledOn;
    digitalWrite(LED_BUILTIN, ledOn == config::kLedActiveLow ? LOW : HIGH);
  }

  const bool connected = hostConnected();
  if (connected && !wasConnected) {
    Serial.printf("READY Beetle Life Lab / %s bring-up\n", config::kBoardName);
    Serial.printf("COMMANDS ping | info | any text (LF or CRLF, max %u bytes)\n",
                  static_cast<unsigned>(config::kMaxLineLength));
#ifdef BLL_VISION_BRINGUP
    Serial.println("COMMANDS i2c | vision | vision-invoke | vision-sample | vision-classify-capture | vision-sensor | vision-sensor-set <id> <opt>");
#endif
    Serial.println("COMMANDS adc-start [seconds] | adc-stop (no seconds = unlimited)");
#ifdef BLL_EVIDENCE_STORE
    Serial.println("COMMANDS evidence-status | evidence-diag | evidence-read <after_index> | evidence-init-empty");
    Serial.println("COMMANDS observe-status | observe-run | observe-light <ok|low|unknown> | observe-vision <invoke|sample> | observe-mode <follow|explore|hold>");
    Serial.println("COMMANDS servo-status | servo-us <us> | servo-pose <FRONT|TOP|BACK> | servo-cal <FRONT|TOP|BACK> <us|0> | servo-off");
    Serial.println("COMMANDS passive-candidate <run> <seq> <start_ms> <end_ms> <peak_uV> <score_milli> <config_sha256> | passive-check <same fields>");
    Serial.println("COMMANDS lowfreq-begin <run8> <ad3_ms> | lowfreq-anchor <ad3_ms> | lowfreq-rms <t_ms> <ch1_nV> <ch2_nV> | lowfreq-end (reference only)");
#endif
    printStatus();
  }
  if (!connected && wasConnected) {
    lineLength = 0;
    discarding = false;
    // Polling stops while disconnected; do not leave DMA running in Capturing.
    piezoAdcAbort("usb_disconnected");
  }
  wasConnected = connected;

  if (connected) {
    pollSerial();
    executeAfterIdle();
    piezoAdcPoll();
    if (!piezoAdcIsRunning() && now - lastHeartbeatMs >= config::kHeartbeatIntervalMs) {
      lastHeartbeatMs = now;
      Serial.printf("HEARTBEAT uptime_ms=%lu led=%s\n",
                    static_cast<unsigned long>(now), ledOn ? "on" : "off");
    }
  }
  // Also while USB is disconnected, so a servo ramp or park never freezes half-way.
  observationPoll();
  lowfreqPoll();  // the XIAO's own arm motions, for the low-frequency forest's arm mask
  delay(1);
}

