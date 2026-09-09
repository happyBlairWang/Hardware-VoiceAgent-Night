/*
 * notetaker_ctl - the XIAO SAMD21 as a physical start/stop button for the
 * Mac-side notetaker relay.
 *
 * The SAMD21 has no mic, no WiFi and 32 KB of RAM, so it does not touch audio.
 * It is the control surface: one button, one status light. All capture and
 * transcription happens in relay/notetaker.py on the Mac.
 *
 * WIRING (optional - everything also works over serial with no parts at all):
 *   momentary push button between D1 and GND.  Nothing else. The internal
 *   pull-up does the rest, so the pin idles HIGH and reads LOW when pressed.
 *
 * PROTOCOL (newline-delimited ASCII, 115200 baud, USB CDC)
 *   board -> host :  READY        booted, waiting
 *                    REC          user asked to start capturing
 *                    STOP         user asked to stop
 *   host  -> board :  REC          host confirms it is capturing  -> LED solid
 *                     IDLE         host is not capturing          -> LED heartbeat
 *                     PING         -> board replies PONG
 *
 * The LED follows the HOST, never the button. If the Mac never confirms, the
 * light stays on heartbeat and you know the relay is not actually running.
 *
 * GOTCHA: the XIAO user LED is ACTIVE LOW - LOW turns it on.
 */

const uint8_t  PIN_BUTTON  = 1;      // D1, to GND
const uint16_t DEBOUNCE_MS = 40;
const uint16_t BEAT_ON_MS  = 60;     // heartbeat blink while idle
const uint16_t BEAT_OFF_MS = 1940;

bool recording   = false;            // as confirmed by the host
bool lastReading = HIGH;
bool stableState = HIGH;
unsigned long lastEdge = 0;
unsigned long beatAt   = 0;
bool beatOn = false;

void ledOn()  { digitalWrite(LED_BUILTIN, LOW);  }
void ledOff() { digitalWrite(LED_BUILTIN, HIGH); }

void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
  ledOff();
  pinMode(PIN_BUTTON, INPUT_PULLUP);

  Serial.begin(115200);
  unsigned long deadline = millis() + 3000;
  while (!Serial && millis() < deadline) { /* spin */ }

  Serial.println("READY");
}

void handleHostLine(String line) {
  line.trim();
  if (line == "REC") {
    recording = true;
    ledOn();
  } else if (line == "IDLE") {
    recording = false;
    ledOff();
    beatAt = millis();
    beatOn = false;
  } else if (line == "PING") {
    Serial.println("PONG");
  }
}

void loop() {
  // ---- debounced button -> announce intent to the host ----
  bool reading = digitalRead(PIN_BUTTON);
  if (reading != lastReading) {
    lastReading = reading;
    lastEdge = millis();
  }
  if (millis() - lastEdge > DEBOUNCE_MS && reading != stableState) {
    stableState = reading;
    if (stableState == LOW) {               // pressed
      Serial.println(recording ? "STOP" : "REC");
    }
  }

  // ---- host commands ----
  if (Serial.available()) {
    handleHostLine(Serial.readStringUntil('\n'));
  }

  // ---- status light ----
  if (recording) {
    ledOn();                                 // solid = the Mac is capturing
  } else {
    unsigned long now = millis();
    if (!beatOn && now - beatAt >= BEAT_OFF_MS) {
      beatOn = true;  beatAt = now;  ledOn();
    } else if (beatOn && now - beatAt >= BEAT_ON_MS) {
      beatOn = false; beatAt = now;  ledOff();
    }
  }
}
