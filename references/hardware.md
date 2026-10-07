# Hardware paths

Nothing on the AI side changes between these — the relay is the same. Pick one,
read only its section, and read **Safety** before connecting power.

Contents: Safety · reSpeaker XVF3800 (USB) · XIAO ESP32-S3 + NS4168 · Classic ESP32
· Wi-Fi · Firmware traps · Recovery

## Safety — read before power

- **Lithium battery polarity.** JST plugs seat either way round and some makers wire
  red/black reversed. Match the **red** lead to the `+` printed by the socket before
  pushing it in. Reversed can kill the board; LiPos can catch fire.
- **A hot amplifier means unplug now.** A Class-D amp barely warms when wired right.
  Heat means a fault — often *firmware*, not wiring: an I2S amp (NS4168) fed by
  DAC firmware has no valid clock and burns power. Overcurrent can make the host cut
  the USB port, so the board "disappears" — the amp, not the board, is the cause.
  Unplug the amp, confirm the board enumerates alone, fix the firmware, then
  reconnect with power last.
- **Never ground either speaker terminal.** Class-D outputs are bridged: both `+`
  and `−` are driven. Grounding one shorts an output stage.
- **3V3 vs 5V.** Analog mics (MAX9814) on **3V3** — their output rides on the supply
  and 5V overdrives the ADC pin. Amps on **5V** for full volume.
- **Old phones:** disconnect from any live phone line (48 V DC, ~90 V AC ringing).
  Leave the bells alone.

## reSpeaker XVF3800 over USB — easiest, best audio

A 4-mic array with an XMOS DSP doing **echo cancellation**, beamforming, noise
suppression and 60 dB AGC, plus an onboard amp with a 2-pin JST speaker socket.
Out of the box it enumerates as a USB audio device: mic **and** speaker.

- **The bare board has no Wi-Fi and no microcontroller.** It needs a host — the
  laptop over USB. Wi-Fi only comes with the variant that carries a XIAO.
- **Point the relay at it by name**, falling back to the default device if absent:
  ```python
  for i, d in enumerate(sd.query_devices()):
      if "respeaker" in d["name"].lower(): ...   # use i for input and output
  ```
  Show the real device name in the UI — "Mac speakers" when it's the reSpeaker
  confuses everyone.
- **Native rate is 16 kHz**; 24 kHz streams also open, so the relay can stay at 24 kHz.
- **Retry opening the input.** CoreAudio does not release a USB device the moment the
  previous process exits; reopening immediately fails with `PaErrorCode -9986`
  (and a capture attempt can segfault). Retry 3–4 times, ~0.8 s apart.
- **Low level?** Speak closer first; if transcripts stay fragmentary at 30 cm, the
  XVF3800's AGC is configurable over I2C.
- **I2S mode (to drive it from a XIAO)** needs a firmware reflash with `dfu-util`
  (`brew install dfu-util`), through the **XMOS USB-C port nearest the 3.5 mm jack**.
  `dfu-util -l` must list both `reSpeaker DFU Upgrade` and `reSpeaker DFU Factory`.
  **The I2S firmware drops USB DFU** — the Factory (safe-mode) partition is the only
  way back, so confirm it exists first. Firmware: the `respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY`
  repo, `xmos_firmwares/`. `i2s_slave` pairs with an ESP32 that is I2S master.

## XIAO ESP32-S3 (Sense) + NS4168 — cordless

The Sense expansion adds a **PDM microphone** (CLK `GPIO42`, DATA `GPIO41`), an
OV2640 camera and an SD slot. There is **no speaker and no amplifier** — add one.

**The S3 has no built-in DAC and no I2S-ADC mode**, so code written for a classic
ESP32's analog audio does not port. Use two I2S controllers: `I2S0` = PDM receive
(mic), `I2S1` = standard transmit (amp). They don't fight.

NS4168 wiring — all on the XIAO's right-hand row, counted from the USB-C end
(labels are hidden under the Sense board, so count):

| pad (from USB-C) | XIAO | GPIO | NS4168 |
|---|---|---|---|
| 1 | `5V` | — | VIN |
| 2 | `GND` | — | GND |
| 3 | `3V3` | — | (skip) |
| 4 | `D10` | 9 | DIN |
| 5 | `D9` | 8 | LRC |
| 6 | `D8` | 7 | BCLK |
| 7 | `D7` | 44 | (skip — UART RX) |

Leave NS4168 `CTRL` unconnected. Verify `5V` with a meter (~5 V against pad 2)
before trusting the count.

- **Send both I2S channels** (`I2S_CHANNEL_FMT_RIGHT_LEFT`, same sample in L and R).
  `CTRL` picks left or right by a voltage window and floating is indeterminate —
  send one channel and a board that picked the other is silent while every log
  line looks healthy.
- **The `5V` pad is dead on battery** — the XIAO has no boost converter. On battery,
  run the amp from `3V3` (quieter) or straight from the cell.
- **Battery:** bare solder pads, onboard charger at **50 mA**. A 3000 mAh cell takes
  ~60 h to charge — pick 100–500 mAh, or power it from USB / a power bank.
- **The port is native USB:** `/dev/cu.usbmodem*`, and the number changes on reset.
  Glob for it; don't hard-code. Set `-DARDUINO_USB_CDC_ON_BOOT=1` for Serial.
  Not appearing at all → power-only cable first; then hold BOOT (B), tap RESET (R),
  release B — both tiny buttons beside the USB-C port.
- **PDM mics read quiet** (~100 of 32767 idle). Apply a software gain (start ~12)
  and tune it against real speech.
- **Camera:** QVGA JPEG is plenty. A VGA frame is ~20 kB once base64'd and overran
  the socket send buffer (`errno 11`), dropping the connection.
- **The WebSocket client is not thread-safe.** A camera task on core 0 calling
  `sendTXT` while `loop()` used the socket on core 1 corrupted it. Capture on the
  task, hand one frame to `loop()`, send from there only.
- Announce the audio format on connect (`fmt:pcm16@24000`) so the relay adapts —
  don't keep a matching constant in two places.

## Classic ESP32 (WROOM-32) — e.g. inside a rotary phone

- **Mic on ADC1 only — `GPIO34`.** ADC2 (0, 2, 4, 12–15, 25–27) is seized by Wi-Fi and
  returns garbage with no error.
- **Avoid pins** 6–11 (flash), 0/12/15 (strapping), 1/3 (USB serial), and 34–39 for
  switches (input-only, no pull-up). `GPIO16/17` are free on WROOM (PSRAM boards
  use them).
- The built-in DAC (`GPIO25`) can only be driven from `I2S0`, which the built-in ADC
  mic also needs — so DAC playback needs a **hardware-timer ISR** writing the DAC
  register, not I2S. An external I2S amp avoids this by using `I2S1`.
- **4 Ω speakers need an amplifier.** A GPIO gives ~20 mA; a 4 Ω speaker wants
  hundreds.
- **An amp on the same USB 5 V can sag the 3V3 rail** and collapse an analog mic's
  bias (raw ADC pinned at 0). Power the amp separately; keep the grounds joined.
- **No PSRAM, 320 KB RAM:** don't attempt TLS + Wi-Fi + audio buffers on-device.
  That's what the relay is for.

## Wi-Fi

- **2.4 GHz only.** A 5 GHz-only network never shows up.
- **SSIDs are case-sensitive.** `connection` ≠ `Connection`.
- **Corporate and guest networks usually isolate clients:** the device joins, gets an
  IP, and can never reach the laptop. A phone hotspot is the reliable fallback.
- **Find the relay by mDNS** (`<hostname>.local`), with a hard-coded IP only as a
  fallback — the laptop's address changes with every network.
- On failure, scan and print the visible networks — but disconnect and reset the
  radio first, or the scan returns nothing and looks like a dead antenna.

## Firmware traps

- **Watchdog resets** in tight audio loops: hand time back every few hundred samples
  (`yield()` / `taskYIELD()`).
- **Jitter buffer:** don't start playback on the first byte — wait for ~150 ms of
  cushion, cap latency at ~500 ms, and play out a short tail after ~250 ms rather
  than stranding it below the threshold (a stranded tail kept an echo gate shut
  forever).
- **Gate on real playback** (`spkPlaying`), not on "bytes queued".
- **A duplicate `#define` only warns** — the later value wins silently. Read the file
  before patching it.
- **Slow uploads:** if `esptool` reports the chip stopped responding, retry at
  `--baud 115200`.

## Recovery

- ESP32/XIAO won't enumerate: different (data) cable, direct port, BOOT+RESET.
- reSpeaker after a bad I2S flash: enter Safe Mode, reflash the USB firmware with
  `dfu-util`.
- Mic dead after restarting the relay: wait a second and restart; the retry should
  cover it.
