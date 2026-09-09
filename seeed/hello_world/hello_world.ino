/*
 * Hello World for the Seeed XIAO SAMD21 (a.k.a. Seeeduino XIAO, "Seeed XIAO M0").
 *
 * Proves three things, in order of how often they go wrong:
 *   1. the toolchain compiles and uploads
 *   2. the USB CDC serial port comes back up after upload
 *   3. you can drive a pin
 *
 * GOTCHA: the XIAO's user LED is ACTIVE LOW. digitalWrite(LED_BUILTIN, LOW)
 * turns it ON. If your blink looks inverted, it isn't - this is the board.
 *
 * Upload:  arduino-cli compile -b Seeeduino:samd:seeed_XIAO_m0 hello_world
 *          arduino-cli upload  -b Seeeduino:samd:seeed_XIAO_m0 -p /dev/cu.usbmodem1101 hello_world
 * Watch:   arduino-cli monitor -p /dev/cu.usbmodem1101 -c baudrate=115200
 */

const unsigned long TICK_MS = 1000;

unsigned long ticks = 0;
unsigned long last  = 0;

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, HIGH);   // HIGH = off on this board

  Serial.begin(115200);
  // Wait for the host to open the port, but never block forever - if this
  // sketch runs on battery with no host attached it still has to boot.
  unsigned long deadline = millis() + 3000;
  while (!Serial && millis() < deadline) { /* spin */ }

  Serial.println();
  Serial.println("hello from the XIAO SAMD21");
  Serial.println("type anything and press enter - I'll echo it back");
}

void loop() {
  if (millis() - last >= TICK_MS) {
    last = millis();
    ticks++;

    digitalWrite(LED_BUILTIN, LOW);    // on
    delay(40);
    digitalWrite(LED_BUILTIN, HIGH);   // off

    Serial.print("tick ");
    Serial.print(ticks);
    Serial.print("  uptime=");
    Serial.print(millis() / 1000);
    Serial.println("s");
  }

  if (Serial.available()) {
    String line = Serial.readStringUntil('\n');
    line.trim();
    if (line.length()) {
      Serial.print("you said: ");
      Serial.println(line);
    }
  }
}
