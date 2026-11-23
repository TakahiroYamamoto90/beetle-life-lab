#ifdef BLL_VISION_BRINGUP
#include <Arduino.h>
#include <Seeed_Arduino_SSCMA.h>
#include <Wire.h>
#include <cstring>
#include <esp_heap_caps.h>
#include <mbedtls/base64.h>
#include <mbedtls/sha256.h>
#include <cstdio>
#include "vision_sscma.h"
#include "config.h"

namespace {
constexpr uint32_t kBusHz = 100000;
constexpr uint32_t kReplyTimeoutMs = 5000;
SSCMA camera;
bool busReady = false;
bool protocolReady = false;
// SSCMA queries run synchronously on loopTask. Clear on each callback; never
// retain references or call query recursively from a callback.
StaticJsonDocument<4096> queryReply;

bool acknowledged() {
  if (!busReady) return false;
  Wire.beginTransmission(I2C_ADDRESS);
  return Wire.endTransmission() == 0;
}

bool connect() {
  if (!acknowledged()) {
    protocolReady = false;
    Serial.println("VISION_UNAVAILABLE reason=no_ack address=0x62 label=unknown");
    return false;
  }
  if (!protocolReady) {
    Serial.println("VISION_CONNECT address=0x62");
    protocolReady = camera.begin(&Wire, -1, I2C_ADDRESS, 2, kBusHz);
    if (!protocolReady) {
      Serial.println("VISION_UNAVAILABLE reason=sscma_handshake_failed label=unknown");
      return false;
    }
    Serial.print("VISION_CONNECTED name=");
    Serial.println(camera.name());
  }
  return true;
}

// Use the SDK transport with raw, validated replies: its invoke() collapses
// device errors into a timeout and exposes cached vectors from earlier calls.
bool query(const char* command, const char* name, bool awaitEvent = false) {
  bool responseOk = false;
  bool eventOk = false;
  bool failed = false;
  Serial.print("VISION_QUERY ");
  Serial.println(name);
  camera.write(command, std::strlen(command));
  const uint32_t started = millis();
  while (millis() - started < kReplyTimeoutMs) {
    camera.fetch([&](const char* raw, size_t length) {
      auto& reply = queryReply;
      reply.clear();
      if (deserializeJson(reply, raw, length)) {
        Serial.println("VISION_PARSE_ERROR label=unknown");
        return;
      }
      Serial.print("VISION_REPLY ");
      serializeJson(reply, Serial);
      Serial.println();
      if (!reply["name"].is<const char*>() || !reply["type"].is<int>() ||
          !reply["code"].is<int>()) return;
      const char* received = reply["name"];
      if (std::strcmp(received, name) != 0) return;
      const int type = reply["type"];
      if (type != CMD_TYPE_RESPONSE && type != CMD_TYPE_EVENT) return;
      if (reply["code"].as<int>() != CMD_OK) {
        failed = true;
      } else if (type == CMD_TYPE_RESPONSE) {
        responseOk = true;
      } else {
        eventOk = true;
      }
    });
    if (failed || (responseOk && (!awaitEvent || eventOk))) break;
    delay(2);
  }
  const bool ok = !failed && responseOk && (!awaitEvent || eventOk);
  Serial.printf("VISION_QUERY_DONE name=%s status=%s elapsed_ms=%lu\n", name,
                ok ? "ok" : (failed ? "device_error" : "timeout"),
                static_cast<unsigned long>(millis() - started));
  if (!ok) protocolReady = false;
  return ok;
}
// Decoded JPEG transport payload (may include zero padding) -> SHA-256 hex.
bool hashPayload(const char* encoded, VisionCaptureResult& out) {
  const size_t length = std::strlen(encoded);
  const size_t capacity = length / 4 * 3 + 3;
  size_t decodedLength = 0;
  unsigned char* decoded = static_cast<unsigned char*>(
      heap_caps_malloc(capacity, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (!decoded) return false;
  bool ok = mbedtls_base64_decode(decoded, capacity, &decodedLength,
                                  reinterpret_cast<const unsigned char*>(encoded), length) == 0 &&
            decodedLength > 0;
  unsigned char digest[32];
  ok = ok && mbedtls_sha256_ret(decoded, decodedLength, digest, 0) == 0;
  free(decoded);
  if (!ok) return false;
  for (unsigned i = 0; i < 32; ++i) snprintf(out.payloadSha256 + i * 2, 3, "%02x", digest[i]);
  out.payloadBytes = static_cast<uint32_t>(decodedLength);
  return true;
}
}  // namespace

void visionBusBegin() {
  // SSCMA reads payloads up to 250 bytes, above ESP32 Wire's default 128.
  busReady = Wire.setBufferSize(512) == 512 && Wire.begin(config::kI2cSdaPin, config::kI2cSclPin, kBusHz);
  Wire.setTimeOut(50);
}

void visionScan() {
  Serial.printf("I2C_SCAN sda_gpio=%u scl_gpio=%u hz=%lu bus=%s\n", config::kI2cSdaPin, config::kI2cSclPin,
                static_cast<unsigned long>(kBusHz), busReady ? "ready" : "failed");
  unsigned found = 0;
  unsigned errors = 0;
  if (busReady) {
    for (uint8_t address = 0x08; address <= 0x77; ++address) {
      Wire.beginTransmission(address);
      const uint8_t status = Wire.endTransmission();
      if (status == 0) {
        ++found;
        Serial.printf("I2C_FOUND address=0x%02X\n", address);
      } else if (status != 2) {
        ++errors;
      }
      delay(1);
    }
  }
  Serial.printf("I2C_SCAN_DONE found=%u bus_errors=%u vision_ack=%s\n", found,
                errors, acknowledged() ? "yes" : "no");
}

void visionInspect() {
  if (connect()) {
    query("AT+VER?\r\n", "VER?");
    query("AT+MODEL?\r\n", "MODEL?");
    query("AT+MODELS?\r\n", "MODELS?");
    query("AT+INFO?\r\n", "INFO?");
    query("AT+SENSORS?\r\n", "SENSORS?");
    query("AT+INVOKE?\r\n", "INVOKE?");
  }
  Serial.println("VISION_INSPECT_DONE");
}

void visionInvokeOnce() {
  // Official AT protocol: N_TIMES, DIFFERED, RESULT_ONLY. The SDK's
  // invoke() flag mapping differs; request one immediate frame, results only.
  const bool ok = connect() && query("AT+INVOKE=1,0,1\r\n", "INVOKE", true);
  Serial.printf("VISION_INVOKE_DONE status=%s label=unknown model_semantics=unverified\n",
                ok ? "ok" : "unavailable");
}

// Invoke and return the exact image used by inference in one SSCMA event.
// RESULT_ONLY=0 asks Vision V2 to include that frame alongside its results.
// The large JSON is streamed to the PC unchanged; this avoids storing the
// base64 image in the small ArduinoJson document used by ordinary queries.
void visionClassifyCaptureOnce() {
  constexpr size_t kMaxFrameBytes = 256 * 1024;
  constexpr uint32_t kTimeoutMs = 20000;
  bool responseOk = false;
  bool eventOk = false;
  bool failed = false;
  const char* reason = "connect_failed";
  VisionCaptureResult captured;
  if (connect() && query("AT+BREAK\r\n", "BREAK")) {
    char* frame = static_cast<char*>(heap_caps_malloc(kMaxFrameBytes + 1,
                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!frame) {
      reason = "psram_allocation_failed";
    } else {
      size_t used = 0;
      reason = "timeout";
      const char command[] = "AT+INVOKE=1,0,0\r\n";
      camera.write(command, sizeof(command) - 1);
      const uint32_t started = millis();
      while (!failed && millis() - started < kTimeoutMs) {
        const int available = camera.available();
        if (available > 0) {
          char chunk[250] = {};
          const int count = camera.read(chunk, available > 250 ? 250 : available);
          for (int i = 0; i < count && !failed; ++i) {
            if (used >= kMaxFrameBytes) {
              failed = true;
              reason = "frame_too_large";
              break;
            }
            frame[used++] = chunk[i];
            if (chunk[i] != '\n') continue;
            frame[used] = '\0';
            const size_t jsonStart = frame[0] == '\r' ? 1 : 0;
            size_t jsonLength = used - jsonStart;
            while (jsonLength && (frame[jsonStart + jsonLength - 1] == '\n' ||
                                  frame[jsonStart + jsonLength - 1] == '\r')) {
              frame[jsonStart + --jsonLength] = '\0';
            }
            Serial.print("VISION_CLASSIFY_REPLY ");
            Serial.write(reinterpret_cast<const uint8_t*>(frame + jsonStart), jsonLength);
            Serial.println();
            StaticJsonDocument<2048> reply;
            DeserializationError parseError = deserializeJson(reply, frame + jsonStart);
            if (parseError) {
              failed = true;
              reason = "invalid_json";
            } else if (reply["name"].is<const char*>() &&
                       std::strcmp(reply["name"], "INVOKE") == 0) {
              if (!reply["code"].is<int>() || reply["code"].as<int>() != CMD_OK ||
                  !reply["type"].is<int>()) {
                failed = true;
                reason = "device_error";
              } else if (reply["type"].as<int>() == CMD_TYPE_RESPONSE) {
                responseOk = true;
              } else if (reply["type"].as<int>() == CMD_TYPE_EVENT) {
                const char* encoded = reply["data"]["image"];
                JsonArray classes = reply["data"]["classes"].as<JsonArray>();
                if (!encoded || !encoded[0]) {
                  failed = true;
                  reason = "missing_inference_frame";
                } else if (classes.isNull() || classes.size() == 0) {
                  failed = true;
                  reason = "missing_classification_output";
                } else if (!hashPayload(encoded, captured)) {
                  failed = true;
                  reason = "invalid_base64";
                } else {
                  eventOk = true;
                }
              }
            }
            used = 0;
          }
        }
        if (responseOk && eventOk && !failed) {
          reason = "none";
          break;
        }
        delay(1);
      }
      free(frame);
      if (!responseOk || !eventOk || failed) {
        query("AT+BREAK\r\n", "BREAK");
        protocolReady = false;
      }
    }
  }
  const bool ok = responseOk && eventOk && !failed;
  Serial.printf("VISION_CLASSIFY_DONE status=%s reason=%s image_bytes=%lu image_sha256=%s\n",
                ok ? "ok" : "unavailable", reason,
                static_cast<unsigned long>(captured.payloadBytes),
                ok ? captured.payloadSha256 : "");
}

// Diagnostics for the camera frame size (2026-11-11). Read: AT+SENSOR? / AT+SENSORS?.
// Set: AT+SENSOR=<id>,1,<opt> changes the sensor mode of the Vision V2 (whether it
// survives a Vision reboot is unverified). Writes nothing to the Evidence log.
void visionSensorQuery() {
  if (connect()) {
    query("AT+SENSOR?\r\n", "SENSOR?");
    query("AT+SENSORS?\r\n", "SENSORS?");
  }
  Serial.println("VISION_SENSOR_DONE");
}

void visionSensorSet(unsigned sensorId, unsigned optId) {
  char command[40];
  snprintf(command, sizeof(command), "AT+SENSOR=%u,1,%u\r\n", sensorId, optId);
  const bool ok = connect() && query("AT+BREAK\r\n", "BREAK") && query(command, "SENSOR");
  if (ok) query("AT+SENSOR?\r\n", "SENSOR?");
  Serial.printf("VISION_SENSOR_SET status=%s sensor=%u opt=%u\n", ok ? "ok" : "failed", sensorId, optId);
}

bool visionBusReady() { return busReady; }

const char* visionFrameSourceName(VisionFrameSource source) {
  return source == VisionFrameSource::Invoke ? "INVOKE" : "SAMPLE";
}

// One frame. SAMPLE: camera only (VISION_SAMPLE_REPLY, unchanged contract). INVOKE:
// the deployed model runs once and the event carries the frame it used plus its raw
// classes (VISION_CLASSIFY_REPLY). The decoded payload hash lets the PC verify that
// the file it saves is the frame this capture produced. The XIAO never interprets
// the classes: they are reference output for the PC, not Evidence or Policy input.
bool visionCapture(VisionCaptureResult& out, VisionFrameSource source) {
  out = VisionCaptureResult();
  const uint32_t began = millis();
  constexpr size_t kMaxFrameBytes = 256 * 1024;
  constexpr uint32_t kCaptureTimeoutMs = 20000;
  const bool invoke = source == VisionFrameSource::Invoke;
  const char* replyName = invoke ? "INVOKE" : "SAMPLE";
  const char* linePrefix = invoke ? "VISION_CLASSIFY_REPLY " : "VISION_SAMPLE_REPLY ";
  bool ok = false;
  const char* reason = "connect_failed";
  if (connect() && query("AT+BREAK\r\n", "BREAK")) {
    // Separate framing avoids the SDK fetch() 32 KiB limit. Parse in place
    // so the base64 image stays in PSRAM rather than being copied into JSON.
    char* frame = static_cast<char*>(heap_caps_malloc(kMaxFrameBytes + 1,
                                      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!frame) {
      reason = "psram_allocation_failed";
    } else {
      bool responseOk = false;
      bool imageOk = false;
      bool failed = false;
      size_t used = 0;
      reason = "timeout";
      // INVOKE: N_TIMES=1, DIFFERED=0, RESULT_ONLY=0 (include the inference frame).
      const char* command = invoke ? "AT+INVOKE=1,0,0\r\n" : "AT+SAMPLE=1\r\n";
      camera.write(command, std::strlen(command));
      const uint32_t started = millis();
      while (!failed && millis() - started < kCaptureTimeoutMs) {
        const int available = camera.available();
        if (available > 0) {
          char chunk[250] = {};
          const int count = camera.read(chunk, available > 250 ? 250 : available);
          for (int i = 0; i < count && !failed; ++i) {
            if (used >= kMaxFrameBytes) {
              failed = true;
              reason = "frame_too_large";
              break;
            }
            frame[used++] = chunk[i];
            if (chunk[i] != '\n') continue;
            frame[used] = '\0';
            StaticJsonDocument<2048> reply;
            if (deserializeJson(reply, frame)) {
              failed = true;
              reason = "invalid_json";
            } else if (reply["name"].is<const char*>() &&
                       std::strcmp(reply["name"], replyName) == 0) {
              if (!reply["code"].is<int>() || !reply["type"].is<int>() ||
                  reply["code"].as<int>() != CMD_OK) {
                failed = true;
                reason = "device_error";
              } else if (reply["type"].as<int>() == CMD_TYPE_RESPONSE) {
                responseOk = true;
              } else if (reply["type"].as<int>() == CMD_TYPE_EVENT) {
                const char* encoded = reply["data"]["image"];
                imageOk = encoded && encoded[0];
                if (!imageOk) {
                  failed = true;
                  reason = "missing_image";
                } else if (!hashPayload(encoded, out)) {
                  failed = true;
                  reason = "invalid_base64";
                }
                if (invoke) {
                  JsonArrayConst classes = reply["data"]["classes"].as<JsonArrayConst>();
                  out.classesPresent = !classes.isNull() && classes.size() > 0;
                  int best = -1, bestClass = -1; bool tied = false;
                  for (JsonArrayConst row : classes) {
                    if (row.size() < 2 || !row[0].is<int>() || !row[1].is<int>()) { best = -1; tied = true; break; }
                    const int score = row[0], id = row[1];
                    if (score < 0 || score > 100 || id < 0 || id > 255) { best = -1; tied = true; break; }
                    if (score > best) { best = score; bestClass = id; tied = false; }
                    else if (score == best) tied = true;
                  }
                  if (best >= 0 && !tied) { out.topClass = static_cast<int16_t>(bestClass); out.topScore = static_cast<int16_t>(best); }
                }
              }
              Serial.print(linePrefix);
              serializeJson(reply, Serial);
              Serial.println();
            }
            used = 0;
          }
        }
        if (responseOk && imageOk && !failed) {
          ok = true;
          reason = "none";
          break;
        }
        delay(1);
      }
      free(frame);
      if (!ok) {
        // Stop a timed-out acquisition before another diagnostic command.
        query("AT+BREAK\r\n", "BREAK");
        protocolReady = false;
      }
    }
  }
  out.ok = ok;
  out.reason = reason;
  out.durationMs = millis() - began;
  return ok;
}

void visionSampleOnce() {
  VisionCaptureResult result;
  visionCapture(result);
  Serial.printf("VISION_SAMPLE_DONE status=%s reason=%s label=unknown\n",
                result.ok ? "ok" : "unavailable", result.reason);
}
#endif
