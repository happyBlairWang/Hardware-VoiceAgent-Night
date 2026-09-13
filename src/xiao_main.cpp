// Vintage phone / talking toy -- XIAO ESP32-S3 Sense.
//
//   onboard PDM mic  ->  WiFi  ->  relay on the Mac  ->  AssemblyAI
//   AssemblyAI  ->  relay  ->  WiFi  ->  I2S  ->  NS4168  ->  speaker
//
// Wiring (NS4168, all on the XIAO's right-hand row, USB-C at the top):
//   VIN  -> 5V   (1st pad, nearest USB.  On battery use 3V3: the 5V pad is
//                 dead without USB, since the XIAO has no boost converter)
//   GND  -> GND  (2nd pad)
//   DIN  -> D10  (4th pad, GPIO9)
//   LRC  -> D9   (5th pad, GPIO8)
//   BCLK -> D8   (6th pad, GPIO7)
//   CTRL -> leave unconnected
//   speaker across + and -, neither side grounded
//
// The onboard mic is fixed at GPIO42 (clock) and GPIO41 (data).
//
// Two separate I2S controllers, so neither fights the other:
//   I2S0 = PDM receive  (microphone)
//   I2S1 = standard transmit (NS4168)
// That split is why the S3 needs none of the DAC-timer contortions the plain
// ESP32 required -- its built-in DAC could only be driven from I2S0, which the
// microphone already owned.

#include <Arduino.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ESPmDNS.h>
#include <driver/i2s.h>
#include "esp_camera.h"
#include <mbedtls/base64.h>
#include "secrets.h"

// ---- microphone (onboard, fixed pins) ----
#define PDM_CLK       42
#define PDM_DATA      41
#define RATE          24000            // matches the agent: no resampling anywhere
#define FRAME         480              // 20 ms

// PDM mics have no analog gain stage, so a quiet room really does read quiet.
// Raw idle sits near 100 of 32767; speech needs lifting before it is useful.
#define MIC_GAIN      12

// ---- speaker (NS4168 over I2S) ----
#define I2S_BCLK      7                // D8
#define I2S_LRC       8                // D9
#define I2S_DIN       9                // D10
#define SPK_BUF       16384            // samples, power of two (~680 ms)
#define SPK_PRIME     (RATE / 7)       // ~150 ms cushion before playback starts
#define SPK_MAXLAG    (RATE / 2)       // ~500 ms ceiling on latency
#define ECHO_TAIL_MS  300              // stay deaf this long after the last sample

#define LED_PIN       21               // XIAO user LED, active LOW

// ---- camera (Sense expansion, fixed pins; none clash with the audio pins) ----
#define CAM_ENABLE    1
#define CAM_PERIOD_MS 1500             // one frame this often
#define XCLK_GPIO  10
#define SIOD_GPIO  40
#define SIOC_GPIO  39
#define Y9_GPIO    48
#define Y8_GPIO    11
#define Y7_GPIO    12
#define Y6_GPIO    14
#define Y5_GPIO    16
#define Y4_GPIO    18
#define Y3_GPIO    17
#define Y2_GPIO    15
#define VSYNC_GPIO 38
#define HREF_GPIO  47
#define PCLK_GPIO  13

static WebSocketsClient ws;
static bool linked = false;

static int16_t           spkBuf[SPK_BUF];
static volatile uint32_t spkHead = 0, spkTail = 0;
static volatile bool     spkPlaying = false;
static volatile uint32_t spkStall = 0;
static volatile bool     echoGate = true;

// ---------------------------------------------------------------- speaker
static void spkPush(const uint8_t *d, size_t n) {
  uint32_t q = (spkHead - spkTail) & (SPK_BUF - 1);
  if (q > SPK_MAXLAG) spkTail = (spkTail + (q - SPK_MAXLAG)) & (SPK_BUF - 1);

  const int16_t *src = (const int16_t *)d;
  for (size_t i = 0; i < n / 2; i++) {
    uint32_t nh = (spkHead + 1) & (SPK_BUF - 1);
    if (nh == spkTail) break;                  // full: drop rather than stall
    spkBuf[spkHead] = src[i];
    spkHead = nh;
  }
}

// i2s_write blocks until DMA has room, so this paces itself off the hardware
// clock. A jitter buffer sits in front of it: WiFi delivers audio in uneven
// bursts, and draining on the first byte turns every hiccup into an audible
// gap rather than a little latency.
static void ampTask(void *) {
  static int16_t mono[128];
  static int16_t chunk[128 * 2];        // interleaved L,R
  for (;;) {
    uint32_t q = (spkHead - spkTail) & (SPK_BUF - 1);
    if (!spkPlaying) {
      if (q >= SPK_PRIME)            { spkPlaying = true; spkStall = 0; }
      else if (q && ++spkStall > 45) { spkPlaying = true; spkStall = 0; }
    } else if (q == 0) {
      spkPlaying = false; spkStall = 0;
    }

    size_t n = 0;
    if (spkPlaying) {
      while (n < 128 && spkTail != spkHead) {
        mono[n++] = spkBuf[spkTail];
        spkTail = (spkTail + 1) & (SPK_BUF - 1);
      }
    }
    while (n < 128) mono[n++] = 0;              // pad with silence
    for (size_t i = 0; i < 128; i++) {          // same sample to both slots
      chunk[i * 2]     = mono[i];
      chunk[i * 2 + 1] = mono[i];
    }
    size_t wrote = 0;
    i2s_write(I2S_NUM_1, chunk, sizeof(chunk), &wrote, portMAX_DELAY);
  }
}

static void startAmp() {
  i2s_config_t c = {};
  c.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
  c.sample_rate          = RATE;
  c.bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT;
  // BOTH channels, deliberately. The NS4168's CTRL pin selects left or right
  // by voltage window and floating is indeterminate -- sending one channel
  // only left it silent while every log line read healthy.
  c.channel_format       = I2S_CHANNEL_FMT_RIGHT_LEFT;
  c.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  c.intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1;
  c.dma_buf_count        = 8;
  c.dma_buf_len          = 256;
  c.use_apll             = false;
  Serial.printf("[spk] driver: %s\n",
                esp_err_to_name(i2s_driver_install(I2S_NUM_1, &c, 0, NULL)));

  i2s_pin_config_t p = {};
  p.bck_io_num   = I2S_BCLK;
  p.ws_io_num    = I2S_LRC;
  p.data_out_num = I2S_DIN;
  p.data_in_num  = I2S_PIN_NO_CHANGE;
  Serial.printf("[spk] pins:   %s\n", esp_err_to_name(i2s_set_pin(I2S_NUM_1, &p)));
  i2s_zero_dma_buffer(I2S_NUM_1);

  xTaskCreatePinnedToCore(ampTask, "amp", 4096, NULL, 2, NULL, 1);
  Serial.printf("[spk] NS4168  BCLK=D8(%d) LRC=D9(%d) DIN=D10(%d) @ %d Hz\n",
                I2S_BCLK, I2S_LRC, I2S_DIN, RATE);
}

// ---------------------------------------------------------------- microphone
static void startMic() {
  i2s_config_t c = {};
  c.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_PDM);
  c.sample_rate          = RATE;
  c.bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT;
  c.channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT;
  c.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  c.intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1;
  c.dma_buf_count        = 8;
  c.dma_buf_len          = FRAME;
  c.use_apll             = false;
  Serial.printf("[mic] driver: %s\n",
                esp_err_to_name(i2s_driver_install(I2S_NUM_0, &c, 0, NULL)));

  i2s_pin_config_t p = {};
  p.bck_io_num   = I2S_PIN_NO_CHANGE;
  p.ws_io_num    = PDM_CLK;
  p.data_out_num = I2S_PIN_NO_CHANGE;
  p.data_in_num  = PDM_DATA;
  Serial.printf("[mic] pins:   %s\n", esp_err_to_name(i2s_set_pin(I2S_NUM_0, &p)));
  Serial.printf("[mic] PDM CLK=%d DATA=%d @ %d Hz, gain x%d\n",
                PDM_CLK, PDM_DATA, RATE, MIC_GAIN);
}

// ---------------------------------------------------------------- camera
#if CAM_ENABLE
static bool camReady = false;

static void startCam() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer   = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO; c.pin_d1 = Y3_GPIO; c.pin_d2 = Y4_GPIO; c.pin_d3 = Y5_GPIO;
  c.pin_d4 = Y6_GPIO; c.pin_d5 = Y7_GPIO; c.pin_d6 = Y8_GPIO; c.pin_d7 = Y9_GPIO;
  c.pin_xclk = XCLK_GPIO;   c.pin_pclk  = PCLK_GPIO;
  c.pin_vsync = VSYNC_GPIO; c.pin_href  = HREF_GPIO;
  c.pin_sccb_sda = SIOD_GPIO; c.pin_sccb_scl = SIOC_GPIO;
  c.pin_pwdn = -1; c.pin_reset = -1;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  // 320x240, not 640x480: a VGA frame is ~20 kB once base64'd, which overruns
  // the TCP send buffer (EAGAIN) and takes the whole connection down with it.
  c.frame_size   = FRAMESIZE_QVGA;
  c.jpeg_quality = 14;
  c.fb_count     = psramFound() ? 2 : 1;
  c.fb_location  = psramFound() ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM;
  c.grab_mode    = CAMERA_GRAB_LATEST;

  esp_err_t e = esp_camera_init(&c);
  camReady = (e == ESP_OK);
  Serial.printf("[cam] init: %s\n", esp_err_to_name(e));
}

// Frames go out as a TEXT frame prefixed "img:", so the relay can tell them
// apart from the microphone's binary audio without a framing scheme.
// The camera task ONLY captures and encodes -- it must never touch the socket.
// WebSocketsClient is not thread-safe and loop() already owns it on the other
// core. This parks one finished frame; loop() sends it and frees it.
static char  *pendingImg = nullptr;
static size_t pendingLen = 0;

static void camTask(void *) {
  for (;;) {
    if (!camReady || !linked || pendingImg) {
      vTaskDelay(pdMS_TO_TICKS(200)); continue;
    }
    camera_fb_t *fb = esp_camera_fb_get();
    if (fb) {
      size_t need = 0;
      mbedtls_base64_encode(NULL, 0, &need, fb->buf, fb->len);
      char *out = (char *)heap_caps_malloc(need + 8, MALLOC_CAP_SPIRAM);
      if (!out) out = (char *)malloc(need + 8);
      if (out) {
        memcpy(out, "img:", 4);
        size_t wrote = 0;
        if (mbedtls_base64_encode((uint8_t *)out + 4, need, &wrote,
                                  fb->buf, fb->len) == 0) {
          pendingLen = wrote + 4;
          pendingImg = out;                 // hand off; loop() frees it
        } else {
          free(out);
        }
      }
      esp_camera_fb_return(fb);
    }
    vTaskDelay(pdMS_TO_TICKS(CAM_PERIOD_MS));
  }
}
#endif

// ---------------------------------------------------------------- network
static void onWs(WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      linked = true;
      Serial.println("[ws] relay connected");
      ws.sendTXT("fmt:pcm16@24000");             // tell the relay what to send
      break;
    case WStype_DISCONNECTED:
      linked = false;
      Serial.println("[ws] relay lost");
      break;
    case WStype_BIN:
      spkPush(payload, length);
      break;
    case WStype_TEXT: {
      String cmd((char *)payload, length);
      if (cmd == "gate:off")      { echoGate = false; Serial.println("[gate] OFF"); }
      else if (cmd == "gate:on")  { echoGate = true;  Serial.println("[gate] ON"); }
      else                        { spkTail = spkHead; }   // barge-in: drop the tail
      break;
    }
    default: break;
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(LED_PIN, HIGH);                   // active LOW: off
  delay(2500);                                   // native USB needs a moment

  Serial.println("\n=== XIAO ESP32-S3 phone ===");
  Serial.printf("chip %s  psram %u bytes\n", ESP.getChipModel(), ESP.getPsramSize());
  Serial.printf("[wifi] joining %s ", WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 15000) {
    delay(400); Serial.print(".");
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.printf("\n[wifi] FAILED (status %d). Networks in range:\n", WiFi.status());
    WiFi.disconnect(true); delay(200); WiFi.mode(WIFI_STA); delay(200);
    int n = WiFi.scanNetworks();
    for (int i = 0; i < n && i < 15; i++)
      Serial.printf("[scan]   %-32s %4d dBm\n", WiFi.SSID(i).c_str(), WiFi.RSSI(i));
    Serial.println("[scan] --- fix secrets.h and reflash ---");
    while (true) delay(1000);
  }
  Serial.printf("\n[wifi] ok, ip %s\n", WiFi.localIP().toString().c_str());

  String host = RELAY_HOST;
  if (MDNS.begin("xiaophone")) {
    IPAddress found = MDNS.queryHost(RELAY_MDNS, 4000);
    if (found != IPAddress()) {
      host = found.toString();
      Serial.printf("[mdns] %s.local -> %s\n", RELAY_MDNS, host.c_str());
    } else {
      Serial.printf("[mdns] no answer, falling back to %s\n", host.c_str());
    }
  }
  Serial.printf("[ws] dialling %s:%d\n", host.c_str(), RELAY_PORT);
  ws.begin(host.c_str(), RELAY_PORT, "/");
  ws.onEvent(onWs);
  ws.setReconnectInterval(2000);

  startMic();
  startAmp();
#if CAM_ENABLE
  startCam();
  if (camReady)
    xTaskCreatePinnedToCore(camTask, "cam", 8192, NULL, 1, NULL, 0);
#endif
  Serial.println("\nready - talk to it\n");
}

void loop() {
  ws.loop();

  static int16_t raw[FRAME];
  size_t got = 0;
  if (i2s_read(I2S_NUM_0, raw, sizeof(raw), &got, portMAX_DELAY) != ESP_OK) return;
  int n = got / sizeof(int16_t);

  // Gate on real playback, not on bytes queued: a residue sitting below the
  // prime threshold is not sound in the room, and treating it as such wedges
  // the microphone shut.
  static uint32_t lastAudioMs = 0;
  if (spkPlaying) lastAudioMs = millis();
  bool speaking = echoGate &&
                  (spkPlaying || (millis() - lastAudioMs < ECHO_TAIL_MS));

  int32_t peak = 0;
  for (int i = 0; i < n; i++) {
    int32_t v = (int32_t)raw[i] * MIC_GAIN;
    if (v >  32767) v =  32767;
    if (v < -32768) v = -32768;
    raw[i] = (int16_t)v;
    if (abs(v) > peak) peak = abs(v);
  }

  if (linked && !speaking) ws.sendBIN((uint8_t *)raw, n * sizeof(int16_t));

#if CAM_ENABLE
  // Sent here, on the task that owns the socket, and only while the speaker is
  // idle so a frame never competes with reply audio for the same buffer.
  if (pendingImg && linked && !speaking && !spkPlaying) {
    ws.sendTXT(pendingImg, pendingLen);
    free(pendingImg);
    pendingImg = nullptr;
  }
#endif
  digitalWrite(LED_PIN, (!speaking && peak > 3000) ? LOW : HIGH);   // active LOW

  static uint32_t last = 0;
  if (millis() - last > 2000) {
    last = millis();
    Serial.printf("[mic] peak %5d  spk q %5u  %s\n", (int)peak,
                  (unsigned)((spkHead - spkTail) & (SPK_BUF - 1)),
                  !linked ? "(no relay)" : (speaking ? "DEAF (speaker playing)"
                                                     : "-> relay"));
  }
}
