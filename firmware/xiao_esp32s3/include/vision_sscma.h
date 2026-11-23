#pragma once
#include <cstdint>

#ifdef BLL_VISION_BRINGUP
struct VisionCaptureResult {
  bool ok = false;
  const char* reason = "not_started";
  char payloadSha256[65] = {};  // SHA-256 of the decoded base64 payload sent to the PC
  uint32_t payloadBytes = 0;
  uint32_t durationMs = 0;
  // INVOKE only: the event carried a non-empty data.classes array. The scores are
  // streamed to the PC (VISION_CLASSIFY_REPLY) and never interpreted on the XIAO.
  bool classesPresent = false;
  // 0.17.0: the top class of data.classes [[score 0-100, class id], ...]; -1 when absent,
  // malformed or tied. Used only for the image AI stop rule (config.h).
  int16_t topClass = -1;
  int16_t topScore = -1;
};

// SAMPLE = camera frame only. INVOKE = run the deployed model once and return the
// frame it used together with its raw class output (AT+INVOKE=1,0,0).
enum class VisionFrameSource : uint8_t { Sample, Invoke };
const char* visionFrameSourceName(VisionFrameSource source);

void visionBusBegin();
bool visionBusReady();
void visionScan();
void visionInspect();
void visionInvokeOnce();
void visionClassifyCaptureOnce();
void visionSampleOnce();
void visionSensorQuery();                            // "vision-sensor"
void visionSensorSet(unsigned sensorId, unsigned optId);  // "vision-sensor-set <id> <opt>"
// Blocking single frame (up to ~25 s). Prints VISION_SAMPLE_REPLY (Sample) or
// VISION_CLASSIFY_REPLY (Invoke) lines. Success means a valid frame; missing class
// output does not fail the capture (classesPresent stays false).
bool visionCapture(VisionCaptureResult& out, VisionFrameSource source = VisionFrameSource::Sample);
#endif
