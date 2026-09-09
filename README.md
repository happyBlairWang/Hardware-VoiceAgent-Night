# ESP32 Vintage Phone

A 1955 rotary telephone answering as a voice agent. You speak into the handset;
AssemblyAI's Voice Agent API answers through the earpiece.

## How it fits together

    MAX9814 mic -> ESP32 (GPIO34, ADC1) -> WiFi -> relay on a Mac -> AssemblyAI
    HW-104 amp  <- ESP32 (GPIO25, DAC)  <- WiFi <-      relay      <-

The ESP32 never holds the API key and never speaks TLS. A Python relay on the
Mac owns the credential, the TLS session, and the JSON framing; the device only
moves raw audio over a plain WebSocket. That split is deliberate: the ESP32 has
320 KB of RAM and no PSRAM, which is not enough for the WiFi stack, a TLS
session and audio buffers at once.

## Layout

| path | what it is |
|---|---|
| `src/main.cpp` | firmware: mic capture, speaker playback, echo gate |
| `relay/relay.py` | the bridge, the agent config, and the tool handlers |
| `relay/ui.html` | monitoring dashboard: meters, tests, transcripts |
| `docs/phone_scene.py` | Blender script that renders the wiring diagram |
| `phone` | launcher: `./phone` or `./phone bg` |

## Setting up

    python3 -m venv ~/esp-tools
    ~/esp-tools/bin/pip install platformio esptool websockets sounddevice numpy

Then create the two ignored files:

`relay/.env`

    ASSEMBLYAI_API_KEY=your_key_here

`src/secrets.h`

    #pragma once
    #define WIFI_SSID  "your-ssid"
    #define WIFI_PASS  "your-password"
    #define RELAY_HOST "192.168.1.50"          // fallback if mDNS is silent
    #define RELAY_PORT 8080
    #define RELAY_MDNS "your-mac-hostname"     // tried first

Flash and run:

    ~/esp-tools/bin/pio run -e esp32dev -t upload
    ./phone

The dashboard is at http://localhost:8081/ui.html

## Pin map

| ESP32 | goes to | note |
|---|---|---|
| GPIO 34 | MAX9814 `OUT` | ADC1 — ADC2 returns garbage once WiFi is up |
| 3V3 | MAX9814 `VDD` | **not 5V**; its output rides on the supply |
| GPIO 25 | HW-104 `L in` | built-in 8-bit analog output |
| 5V | HW-104 `VCC` | 5V here, unlike the mic |
| GND | both boards | one wire does power return and signal reference |
| GPIO 13,14,16,17 | keypad rows | 16/17 are free only because there is no PSRAM |
| GPIO 18,19,21,22 | keypad columns | internal pull-ups, no resistors needed |
| GPIO 32 | hook switch | other leg to GND |

Speaker floats across the amp's `L+`/`L-`. Grounding either one destroys the amp.

## Things that cost real time to find

- **Voice Agent API asymmetry.** `session.update` nests config under `session`,
  but the server echoes it back as `config`. Outbound audio arrives in a field
  called `data`; inbound audio must be sent in one called `audio`. Using the
  wrong one yields a session that connects, reports healthy, and plays silence.
- **Only 24 kHz works.** Other rates are accepted at handshake, then fail later
  with `internal_error`.
- **ADC2 dies when WiFi starts.** No error, just nonsense readings. The mic has
  to sit on ADC1.
- **The echo gate belongs on the device.** Gating in the relay runs early by the
  device buffer depth plus the network hop, so the microphone reopens while the
  speaker is still sounding and the agent transcribes itself.
- **A jitter buffer, not a bigger one.** Draining on the first byte leaves the
  DAC emitting silence between WiFi packets. ~90 ms of cushion fixes the
  choppiness; more capacity does not.
- **Watchdog resets.** A tight audio loop that never yields reboots the chip
  mid-tone.
