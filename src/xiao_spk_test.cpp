// XIAO ESP32-S3 -- speaker only. No WiFi, no relay, no microphone.
// If this is silent, the fault is the amp, the wiring or the speaker.
//
//   NS4168 VIN -> 5V   (right row, 1st pad from USB-C)
//          GND -> GND  (2nd)
//          DIN -> D10  (4th, GPIO9)
//          LRC -> D9   (5th, GPIO8)
//          BCLK-> D8   (6th, GPIO7)
//
// Sends BOTH channels. The NS4168's CTRL pin chooses left or right by voltage
// window, and floating is indeterminate -- send one channel only and a board
// that picked the other plays nothing while every log line looks healthy.

#include <Arduino.h>
#include <driver/i2s.h>

#define I2S_BCLK 7      // D8
#define I2S_LRC  8      // D9
#define I2S_DIN  9      // D10
#define RATE     24000

static void startAmp() {
  i2s_config_t c = {};
  c.mode                 = (i2s_mode_t)(I2S_MODE_MASTER | I2S_MODE_TX);
  c.sample_rate          = RATE;
  c.bits_per_sample      = I2S_BITS_PER_SAMPLE_16BIT;
  c.channel_format       = I2S_CHANNEL_FMT_RIGHT_LEFT;     // both channels
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
}

// stereo frame: same sample in both slots
static void tone_ms(float hz, uint32_t ms, float amp) {
  const int N = 256;
  static int16_t frame[N * 2];
  uint32_t total = (uint32_t)((uint64_t)RATE * ms / 1000);
  static float phase = 0;
  uint32_t done = 0;
  while (done < total) {
    int n = (int)min((uint32_t)N, total - done);
    for (int i = 0; i < n; i++) {
      phase += 2 * PI * hz / RATE;
      if (phase > 2 * PI) phase -= 2 * PI;
      int16_t v = (int16_t)(sinf(phase) * 32767.0f * amp);
      frame[i * 2] = v; frame[i * 2 + 1] = v;
    }
    size_t wrote = 0;
    i2s_write(I2S_NUM_1, frame, n * 2 * sizeof(int16_t), &wrote, portMAX_DELAY);
    done += n;
  }
}

static void silence(uint32_t ms) {
  const int N = 256;
  static int16_t z[N * 2] = {0};
  uint32_t total = (uint32_t)((uint64_t)RATE * ms / 1000), done = 0;
  while (done < total) {
    int n = (int)min((uint32_t)N, total - done);
    size_t w = 0;
    i2s_write(I2S_NUM_1, z, n * 2 * sizeof(int16_t), &w, portMAX_DELAY);
    done += n;
  }
}

void setup() {
  Serial.begin(115200);
  delay(2500);
  Serial.println("\n=== XIAO speaker test (both channels, loud) ===");
  startAmp();
  Serial.printf("[spk] BCLK=D8(%d) LRC=D9(%d) DIN=D10(%d) @ %d Hz\n",
                I2S_BCLK, I2S_LRC, I2S_DIN, RATE);
  Serial.println("this is LOUD on purpose -- it should be unmissable\n");
}

void loop() {
  Serial.println("1 kHz, 80% volume");            tone_ms(1000, 1200, 0.80); silence(300);
  Serial.println("440 Hz, 80%");                  tone_ms(440, 1200, 0.80);  silence(300);
  Serial.println("three short beeps");
  for (int i = 0; i < 3; i++) { tone_ms(1500, 180, 0.80); silence(180); }
  Serial.println("-- loop --\n");                 silence(1200);
}
