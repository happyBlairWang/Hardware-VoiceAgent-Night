// Vintage phone -- microphone half.
//
// Reads the MAX9814 on GPIO34 at 24 kHz and streams raw 16-bit PCM to the
// relay, and plays the operator's reply back out of GPIO25 into the HW-104.
// Full loop on the device: you talk into the mic, the phone answers.
//
// Wiring (no solder):
//   MAX9814 VDD  -> 3V3      (never 5V: its output rides on the supply)
//   MAX9814 GND  -> GND
//   MAX9814 OUT  -> GPIO 34  (ADC1 -- ADC2 dies when WiFi is on)
//   MAX9814 GAIN -> unconnected  (60 dB; tie to 3V3 for 40 dB if it clips)
//   MAX9814 A/R  -> unconnected

#include <Arduino.h>
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ESPmDNS.h>
#include <driver/i2s.h>
#include <driver/adc.h>
#include <driver/dac.h>
#include <soc/rtc_io_reg.h>
#include "secrets.h"

#define MIC_CHANNEL   ADC1_CHANNEL_6      // GPIO34
#define SAMPLE_RATE   24000
#define FRAME         320                 // 20 ms of audio
#define LED           2
#define SPK_CH        DAC_CHANNEL_1     // GPIO25 -> HW-104 L in
#define SPK_BUF       16384             // power of two; ~2 s at 8 kHz
#define SPK_RATE      8000              // telephone band; the relay downsamples
#define ECHO_TAIL_MS  300               // stay deaf this long after the last sample
#define SPK_PRIME     1200              // 150 ms cushion before playback starts
#define SPK_MAXLAG    4000              // 500 ms ceiling, so lag cannot pile up
#define SPK_STALL     2000              // 250 ms: play a short tail rather than
                                        // stranding it below the prime threshold

static WebSocketsClient ws;
static bool  linked = false;
static float dcLevel = 2048.0f;           // tracks the mic's resting voltage

// ---- speaker: 8-bit unsigned samples streamed from the relay ----
static uint8_t           spkBuf[SPK_BUF];
static volatile uint32_t spkHead = 0, spkTail = 0;

static volatile bool     spkPlaying = false;
static volatile uint32_t spkStall  = 0;

static void spkPush(const uint8_t *d, size_t n) {
  // Never let latency accumulate: if the network delivers a big burst, throw
  // away the oldest audio rather than falling further behind the conversation.
  uint32_t q = (spkHead - spkTail) & (SPK_BUF - 1);
  if (q > SPK_MAXLAG) spkTail = (spkTail + (q - SPK_MAXLAG)) & (SPK_BUF - 1);

  for (size_t i = 0; i < n; i++) {
    uint32_t nh = (spkHead + 1) & (SPK_BUF - 1);
    if (nh == spkTail) break;             // full: drop rather than stall
    spkBuf[spkHead] = d[i];
    spkHead = nh;
  }
}

// The built-in ADC and DAC both want I2S0, and the microphone already has it,
// so playback cannot use I2S. A paced task does not work either: at a higher
// priority than loop() it never blocks and starves it, and at the same
// priority the audio clock stutters. A hardware timer costs almost nothing
// and lets everything else run normally.
static hw_timer_t *spkTimer = nullptr;

static void IRAM_ATTR onSpkTick() {
  // A jitter buffer, which the first version lacked. Playing the instant the
  // first byte lands means every WiFi hiccup becomes an audible gap, and the
  // measured queue sat at zero -- starved, not backed up. So wait for a small
  // cushion, then play through it continuously.
  uint32_t q = (spkHead - spkTail) & (SPK_BUF - 1);
  if (!spkPlaying) {
    if (q >= SPK_PRIME) {
      spkPlaying = true; spkStall = 0;                // enough banked, go
    } else if (q && ++spkStall > SPK_STALL) {
      // The tail of a reply can be shorter than the cushion. Without this it
      // waits for audio that will never arrive, the queue never empties, and
      // the echo gate below stays shut forever.
      spkPlaying = true; spkStall = 0;
    }
  } else if (q == 0) {
    spkPlaying = false; spkStall = 0;                 // ran dry, re-prime
  }

  uint8_t v = 128;                                    // mid-scale = silence
  if (spkPlaying && q) {
    v = spkBuf[spkTail];
    spkTail = (spkTail + 1) & (SPK_BUF - 1);
  }
  // Write the DAC register directly -- dac_output_voltage() is not ISR-safe.
  SET_PERI_REG_BITS(RTC_IO_PAD_DAC1_REG, RTC_IO_PDAC1_DAC, v, RTC_IO_PDAC1_DAC_S);
}

static void onWs(WStype_t type, uint8_t *payload, size_t length) {
  switch (type) {
    case WStype_CONNECTED:
      linked = true;  Serial.println("[ws] relay connected");  break;
    case WStype_DISCONNECTED:
      linked = false; Serial.println("[ws] relay lost");       break;
    case WStype_BIN:
      spkPush(payload, length);                        // operator's voice
      break;
    case WStype_TEXT:
      spkTail = spkHead;                               // barge-in: drop the tail
      break;
    default: break;
  }
}

static void startMic() {
  i2s_config_t cfg = {};
  cfg.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_RX | I2S_MODE_ADC_BUILT_IN);
  cfg.sample_rate          = SAMPLE_RATE;
  cfg.bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT;
  cfg.channel_format       = I2S_CHANNEL_FMT_ONLY_LEFT;
  cfg.communication_format = I2S_COMM_FORMAT_STAND_I2S;
  cfg.intr_alloc_flags     = ESP_INTR_FLAG_LEVEL1;
  cfg.dma_buf_count        = 8;
  cfg.dma_buf_len          = FRAME;
  cfg.use_apll             = false;

  i2s_driver_install(I2S_NUM_0, &cfg, 0, NULL);
  adc1_config_width(ADC_WIDTH_BIT_12);
  adc1_config_channel_atten(MIC_CHANNEL, ADC_ATTEN_DB_11);   // full 0-3.3V swing
  i2s_set_adc_mode(ADC_UNIT_1, MIC_CHANNEL);
  i2s_adc_enable(I2S_NUM_0);
  Serial.println("[mic] listening on GPIO34 @ 24 kHz");
}

void setup() {
  Serial.begin(115200);
  pinMode(LED, OUTPUT);
  delay(300);
  Serial.printf("\n[wifi] joining %s ", WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 12000) {
    delay(400); Serial.print(".");
  }

  if (WiFi.status() != WL_CONNECTED) {
    // Say why, then show what the radio can actually see. The ESP32 has no
    // 5 GHz radio, so anything missing here is either out of range or 5 GHz.
    const char *why;
    switch (WiFi.status()) {
      case WL_NO_SSID_AVAIL:  why = "network name not found";        break;
      case WL_CONNECT_FAILED: why = "wrong password";                break;
      case WL_IDLE_STATUS:    why = "still negotiating";             break;
      case WL_DISCONNECTED:   why = "refused or wrong password";     break;
      default:                why = "unknown";                       break;
    }
    Serial.printf("\n[wifi] FAILED joining \"%s\" - %s (status %d)\n",
                  WIFI_SSID, why, WiFi.status());

    Serial.println("[scan] 2.4 GHz networks this board can see:");
    WiFi.disconnect(true);
    delay(200);
    WiFi.mode(WIFI_STA);
    delay(200);
    int n = WiFi.scanNetworks();
    if (n <= 0) Serial.println("[scan]   (none - check the antenna)");
    for (int i = 0; i < n && i < 18; i++) {
      Serial.printf("[scan]   %-32s  %4d dBm  %s\n",
                    WiFi.SSID(i).c_str(), WiFi.RSSI(i),
                    WiFi.encryptionType(i) == WIFI_AUTH_OPEN ? "open" : "locked");
    }
    Serial.println("[scan] --- fix secrets.h and reflash ---");
    while (true) delay(1000);
  }
  Serial.printf("\n[wifi] ok, ip %s\n", WiFi.localIP().toString().c_str());

  String host = RELAY_HOST;
  if (MDNS.begin("esp32phone")) {
    IPAddress found = MDNS.queryHost(RELAY_MDNS, 4000);
    if (found != IPAddress()) {
      host = found.toString();
      Serial.printf("[mdns] %s.local -> %s\n", RELAY_MDNS, host.c_str());
    } else {
      Serial.printf("[mdns] %s.local not answering, using %s\n",
                    RELAY_MDNS, host.c_str());
    }
  }
  Serial.printf("[ws] dialling %s:%d\n", host.c_str(), RELAY_PORT);
  ws.begin(host.c_str(), RELAY_PORT, "/");
  ws.onEvent(onWs);
  ws.setReconnectInterval(2000);

  startMic();

  dac_output_enable(SPK_CH);
  dac_output_voltage(SPK_CH, 128);
  spkTimer = timerBegin(0, 80, true);                 // 1 MHz tick
  timerAttachInterrupt(spkTimer, &onSpkTick, true);
  timerAlarmWrite(spkTimer, 1000000 / SPK_RATE, true);
  timerAlarmEnable(spkTimer);
  Serial.printf("[spk] speaker live on GPIO25 @ %d Hz\n", SPK_RATE);
}

void loop() {
  ws.loop();

  static uint16_t raw[FRAME];
  static int16_t  pcm[FRAME];
  size_t got = 0;

  if (i2s_read(I2S_NUM_0, raw, sizeof(raw), &got, portMAX_DELAY) != ESP_OK) return;
  int n = got / sizeof(uint16_t);

  // Echo control lives HERE, not in the relay. The relay only knows when it
  // queued audio; the device knows when the DAC is actually still draining it,
  // which is later by the buffer depth plus the WiFi hop. Gating on the real
  // playback state is what stops the operator hearing itself.
  // Gate on whether the DAC is ACTUALLY playing, not on whether bytes are
  // queued. Those differ whenever a residue sits below the prime threshold,
  // and treating that as "speaking" wedges the microphone shut permanently.
  static uint32_t lastAudioMs = 0;
  uint32_t queued = (spkHead - spkTail) & (SPK_BUF - 1);
  if (spkPlaying) lastAudioMs = millis();
  bool speaking = spkPlaying || (millis() - lastAudioMs < ECHO_TAIL_MS);

  int32_t peak = 0;
  uint16_t rawLo = 4095, rawHi = 0;
  for (int i = 0; i < n; i++) {
    float s = (float)(raw[i] & 0x0FFF);          // low 12 bits are the sample
    if ((raw[i] & 0x0FFF) < rawLo) rawLo = raw[i] & 0x0FFF;
    if ((raw[i] & 0x0FFF) > rawHi) rawHi = raw[i] & 0x0FFF;
    dcLevel += (s - dcLevel) * 0.0015f;          // slow high-pass, kills the bias
    int32_t v = (int32_t)((s - dcLevel) * 40.0f);
    if (v >  32767) v =  32767;
    if (v < -32768) v = -32768;
    pcm[i] = (int16_t)v;
    if (abs(v) > peak) peak = abs(v);
  }

  if (linked && !speaking) ws.sendBIN((uint8_t*)pcm, n * sizeof(int16_t));
  digitalWrite(LED, !speaking && peak > 2000);   // blinks only when listening

  static uint32_t last = 0;
  if (millis() - last > 2000) {
    last = millis();
    Serial.printf("[mic] raw %4u-%4u  peak %5d  q %4u %s  %s\n",
                  rawLo, rawHi, (int)peak,
                  (unsigned)queued, spkPlaying ? "PLAY" : "    ",
                  !linked ? "(no relay)" : (speaking ? "DEAF (operator talking)"
                                                     : "-> relay"));
  }
}
