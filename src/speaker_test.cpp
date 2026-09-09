// Standalone speaker check -- no WiFi, no microphone, no relay.
// Drives the HW-104 through the ESP32's built-in analog output on GPIO 25.
//
//   HW-104 L in -> GPIO 25      GND -> GND      VCC -> 5V
//   speaker across L+ and L-, neither side grounded
//
// Flash with:  pio run -e speakertest -t upload
// Back to the phone firmware:  pio run -e esp32dev -t upload

#include <Arduino.h>

#define DAC_PIN 25
#define SR      16000     // samples per second
#define AMP     45        // swing from mid-scale (max 127). Modest on purpose:
                          // a 4ohm load on USB power browns the board out.

// A tight audio loop starves the idle task and the watchdog resets the chip,
// so hand time back every few hundred samples.
static void render(float f1, float f2, float glideTo, uint32_t ms) {
  const uint32_t period = 1000000UL / SR;
  const uint32_t n = (uint32_t)((uint64_t)ms * SR / 1000);
  const uint32_t t0 = micros();
  float phase = 0, phase2 = 0;

  for (uint32_t i = 0; i < n; i++) {
    float s;
    if (glideTo > 0) {                          // exponential glide
      float f = f1 * powf(glideTo / f1, (float)i / n);
      phase += 2 * PI * f / SR;
      s = sinf(phase);
    } else {
      phase += 2 * PI * f1 / SR;
      s = sinf(phase);
      if (f2 > 0) { phase2 += 2 * PI * f2 / SR; s = 0.5f * (s + sinf(phase2)); }
    }
    dacWrite(DAC_PIN, (uint8_t)(128 + AMP * s));

    while ((uint32_t)(micros() - t0) < (uint32_t)((i + 1) * period)) { }
    if ((i & 0x1FF) == 0x1FF) yield();           // ~32 ms: feed the watchdog
  }
}

static void twotone(float a, float b, uint32_t ms) { render(a, b, 0, ms); }
static void sweep(float a, float b, uint32_t ms)   { render(a, 0, b, ms); }
static void hush(uint32_t ms) { dacWrite(DAC_PIN, 128); delay(ms); }

void setup() {
  Serial.begin(115200);
  delay(400);
  dacWrite(DAC_PIN, 128);
  Serial.println("\n[spk] speaker test on GPIO 25");
  Serial.printf("[spk] rate %d Hz, amplitude %d/127\n", SR, AMP);
  Serial.println("[spk] short bursts first -- if the board resets, it is power, not code\n");
}

void loop() {
  Serial.println("A. 1 kHz  x3   (short)");   Serial.flush();
  for (int i = 0; i < 3; i++) { twotone(1000, 0, 150); hush(150); }

  Serial.println("B. dial tone 350+440 Hz");  Serial.flush();
  twotone(350, 440, 1500); hush(400);

  Serial.println("C. sweep 200 Hz -> 3 kHz"); Serial.flush();
  sweep(200, 3000, 1500); hush(400);

  Serial.println("D. ring 440+480 Hz x2");    Serial.flush();
  for (int i = 0; i < 2; i++) { twotone(440, 480, 700); hush(300); }

  Serial.println("-- survived a full pass, looping --\n"); Serial.flush();
  hush(1200);
}
