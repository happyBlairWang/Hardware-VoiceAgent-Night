// XIAO ESP32-S3 Sense -- camera check.
// Captures a JPEG and ships it over USB as base64 so it can be viewed on the
// Mac. Nothing else runs: no WiFi, no audio.
//
// The OV2640 sits on the Sense expansion board with a fixed pin map.

#include <Arduino.h>
#include "esp_camera.h"
#include <mbedtls/base64.h>

// XIAO ESP32-S3 Sense camera pins
#define PWDN_GPIO_NUM  -1
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM  10
#define SIOD_GPIO_NUM  40
#define SIOC_GPIO_NUM  39
#define Y9_GPIO_NUM    48
#define Y8_GPIO_NUM    11
#define Y7_GPIO_NUM    12
#define Y6_GPIO_NUM    14
#define Y5_GPIO_NUM    16
#define Y4_GPIO_NUM    18
#define Y3_GPIO_NUM    17
#define Y2_GPIO_NUM    15
#define VSYNC_GPIO_NUM 38
#define HREF_GPIO_NUM  47
#define PCLK_GPIO_NUM  13

static bool camReady = false;

static void startCam() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer   = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM;  c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM;  c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;  c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM;  c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM;   c.pin_pclk  = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM; c.pin_href  = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM; c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM;   c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size   = FRAMESIZE_VGA;      // 640x480
  c.jpeg_quality = 12;                 // lower number = better
  c.fb_count     = psramFound() ? 2 : 1;
  c.fb_location  = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
  c.grab_mode    = CAMERA_GRAB_LATEST;

  esp_err_t e = esp_camera_init(&c);
  Serial.printf("[cam] init: %s\n", esp_err_to_name(e));
  camReady = (e == ESP_OK);
  if (camReady) {
    sensor_t *s = esp_camera_sensor_get();
    Serial.printf("[cam] sensor PID 0x%02X  (0x26 = OV2640)\n", s->id.PID);
  }
}

static void shoot() {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) { Serial.println("[cam] capture FAILED"); return; }
  Serial.printf("[cam] captured %ux%u  %u bytes jpeg\n", fb->width, fb->height, fb->len);

  size_t need = 0;
  mbedtls_base64_encode(NULL, 0, &need, fb->buf, fb->len);
  uint8_t *b64 = (uint8_t *)malloc(need + 1);
  if (b64) {
    size_t wrote = 0;
    if (mbedtls_base64_encode(b64, need, &wrote, fb->buf, fb->len) == 0) {
      Serial.println("IMG_BEGIN");
      for (size_t i = 0; i < wrote; i += 512) {
        size_t n = (wrote - i) < 512 ? (wrote - i) : 512;
        Serial.write(b64 + i, n);
        Serial.flush();
      }
      Serial.println();
      Serial.println("IMG_END");
    }
    free(b64);
  }
  esp_camera_fb_return(fb);
}

void setup() {
  Serial.begin(115200);
  delay(2500);
  Serial.println("\n=== XIAO ESP32-S3 Sense camera check ===");
  Serial.printf("psram: %u bytes\n", ESP.getPsramSize());
  startCam();
}

void loop() {
  if (!camReady) { Serial.println("[cam] not initialised"); delay(5000); return; }
  shoot();
  delay(8000);
}
