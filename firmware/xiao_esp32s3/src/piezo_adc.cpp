#include <Arduino.h>
#include <driver/adc.h>

#include "config.h"
#include "piezo_adc.h"

namespace {
constexpr uint32_t kSampleRateHz = 8000;
constexpr size_t kDmaReadBytes = 1024;
constexpr size_t kOutputBufferBytes = 8192;
constexpr uint32_t kReadTimeoutMs = 100;
constexpr uint32_t kSamplePeriodUs = 1000000UL / kSampleRateHz;
uint8_t dmaBytes[kDmaReadBytes];
char outputBuffer[kOutputBufferBytes];

enum class State { Idle, Capturing };
State state = State::Idle;
uint64_t sampleCount = 0;
uint64_t targetSampleCount = 0;  // 0 means no automatic limit.
uint32_t requestedDurationSeconds = 0;
bool stopRequested = false;
bool adcInitialized = false;

bool startDmaAdc() {
  static adc_digi_pattern_config_t pattern = {
      .atten = ADC_ATTEN_DB_12,
      .channel = ADC1_CHANNEL_0,  // XIAO D0 / GPIO1 / ADC1_CH0
      // ESP-IDF 4.4's legacy DMA pattern uses zero-based unit IDs and the
      // literal bit count (12), unlike ADC_UNIT_1 / ADC_WIDTH_BIT_12 enums.
      .unit = 0,  // ADC_NUM_1 in the ESP-IDF HAL
      .bit_width = SOC_ADC_DIGI_MAX_BITWIDTH,
  };
  const adc_digi_init_config_t initConfig = {
      .max_store_buf_size = 4096,
      .conv_num_each_intr = 256,
      .adc1_chan_mask = 1U << ADC1_CHANNEL_0,
      .adc2_chan_mask = 0,
  };
  const adc_digi_configuration_t adcConfig = {
      .conv_limit_en = false,
      .conv_limit_num = 0,
      .pattern_num = 1,
      .adc_pattern = &pattern,
      .sample_freq_hz = kSampleRateHz,
      .conv_mode = ADC_CONV_SINGLE_UNIT_1,
      .format = ADC_DIGI_OUTPUT_FORMAT_TYPE2,
  };

  esp_err_t err = adc_digi_initialize(&initConfig);
  if (err != ESP_OK) {
    Serial.printf("ADC_ERROR stage=initialize code=%d\n", static_cast<int>(err));
    return false;
  }
  adcInitialized = true;
  err = adc_digi_controller_configure(&adcConfig);
  if (err != ESP_OK) {
    Serial.printf("ADC_ERROR stage=configure code=%d\n", static_cast<int>(err));
    adc_digi_deinitialize();
    adcInitialized = false;
    return false;
  }
  err = adc_digi_start();
  if (err != ESP_OK) {
    Serial.printf("ADC_ERROR stage=start code=%d\n", static_cast<int>(err));
    adc_digi_deinitialize();
    adcInitialized = false;
    return false;
  }
  return true;
}

void finishCapture(const char* reason) {
  if (adcInitialized) {
    adc_digi_stop();
    adc_digi_deinitialize();
    adcInitialized = false;
  }
  Serial.printf("ADC_CSV_END samples=%llu duration_us=%llu reason=%s\n",
                static_cast<unsigned long long>(sampleCount),
                static_cast<unsigned long long>(sampleCount * kSamplePeriodUs), reason);
  sampleCount = 0;
  targetSampleCount = 0;
  requestedDurationSeconds = 0;
  stopRequested = false;
  state = State::Idle;
}

bool appendSample(char* output, size_t& used, uint16_t sample) {
  if (used >= kOutputBufferBytes) return false;
  const uint64_t elapsedUs = sampleCount * kSamplePeriodUs;
  const int length = std::snprintf(output + used, kOutputBufferBytes - used,
                                   "%llu,%u\n",
                                   static_cast<unsigned long long>(elapsedUs),
                                   static_cast<unsigned>(sample));
  if (length < 0 || static_cast<size_t>(length) >= kOutputBufferBytes - used) return false;
  used += static_cast<size_t>(length);
  ++sampleCount;
  return true;
}
}  // namespace

bool piezoAdcIsRunning() { return state == State::Capturing; }

void piezoAdcStart(uint32_t durationSeconds) {
#ifndef BLL_PASSIVE_ADC_FRONTEND_VERIFIED
  Serial.println("ADC_BLOCKED reason=frontend_not_verified action=identify_sensor_and_protection");
#else
  if (state != State::Idle) {
    Serial.println("ADC_ALREADY_RUNNING");
    return;
  }
  sampleCount = 0;
  requestedDurationSeconds = durationSeconds;
  targetSampleCount = durationSeconds == 0 ? 0 :
      static_cast<uint64_t>(durationSeconds) * kSampleRateHz;
  stopRequested = false;
  if (!startDmaAdc()) return;

  state = State::Capturing;
  Serial.printf("ADC_CSV_BEGIN fields=elapsed_us,raw12 sample_rate_hz=%lu duration_s=%lu timing=nominal\n",
                static_cast<unsigned long>(kSampleRateHz),
                static_cast<unsigned long>(requestedDurationSeconds));
#endif
}

void piezoAdcStop() {
#ifdef BLL_PASSIVE_ADC_FRONTEND_VERIFIED
  if (state == State::Capturing) {
    stopRequested = true;
    Serial.println("ADC_STOP_REQUESTED");
  } else {
    Serial.println("ADC_NOT_RUNNING");
  }
#else
  Serial.println("ADC_BLOCKED reason=frontend_not_verified");
#endif
}

void piezoAdcAbort(const char* reason) {
#ifdef BLL_PASSIVE_ADC_FRONTEND_VERIFIED
  if (state == State::Capturing) finishCapture(reason);
#else
  (void)reason;
#endif
}

void piezoAdcPoll() {
#ifdef BLL_PASSIVE_ADC_FRONTEND_VERIFIED
  if (state != State::Capturing) return;

  uint32_t bytesRead = 0;
  size_t outputBytes = 0;
  const esp_err_t err = adc_digi_read_bytes(dmaBytes, sizeof(dmaBytes), &bytesRead,
                                            kReadTimeoutMs);
  if (err == ESP_OK) {
    const size_t recordCount = bytesRead / sizeof(adc_digi_output_data_t);
    const adc_digi_output_data_t* records =
        reinterpret_cast<const adc_digi_output_data_t*>(dmaBytes);
    for (size_t i = 0; i < recordCount; ++i) {
      if (records[i].type2.unit == 0 && records[i].type2.channel == ADC1_CHANNEL_0) {
        if (!appendSample(outputBuffer, outputBytes,
                          static_cast<uint16_t>(records[i].type2.data))) {
          Serial.println("ADC_ERROR stage=format_buffer");
          stopRequested = true;
          break;
        }
        if (targetSampleCount != 0 && sampleCount >= targetSampleCount) break;
      }
    }
    if (outputBytes != 0) Serial.write(reinterpret_cast<const uint8_t*>(outputBuffer), outputBytes);
  } else if (err != ESP_ERR_TIMEOUT) {
    Serial.printf("ADC_ERROR stage=read code=%d samples=%llu\n", static_cast<int>(err),
                  static_cast<unsigned long long>(sampleCount));
    stopRequested = true;
  }

  if ((targetSampleCount != 0 && sampleCount >= targetSampleCount) || stopRequested) {
    finishCapture(stopRequested ? "stopped" : "duration_reached");
  }
#endif
}
