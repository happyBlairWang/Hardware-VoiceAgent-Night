# Seeed XIAO + AssemblyAI notetaker

## Two corrections before you start

**1. The board plugged in has no microphone.**

`ioreg` says the device on `/dev/cu.usbmodem1101` is a **Seeed XIAO M0** —
the XIAO SAMD21. Specs: ATSAMD21G18A Cortex-M0+ at 48 MHz, 256 KB flash,
**32 KB SRAM, no WiFi, no Bluetooth, no microphone.**

The XIAO you're thinking of is the **XIAO ESP32S3 Sense** (PDM mic + camera +
WiFi) or the **XIAO nRF52840 Sense** (PDM mic + BLE). Those are different
boards. The SAMD21 cannot capture, buffer, or ship audio — 32 KB of RAM is
about two seconds of 16 kHz mono PCM with nothing left over for a TLS stack it
also doesn't have.

So the SAMD21 is wired in here as the **control surface**: a button that starts
and stops the meeting, and a light that tells you it's really recording. The
audio and the transcription live on the Mac.

**2. There is no "AssemblyAI Notetaker API".**

Granola isn't calling a notetaker endpoint. Granola's whole pitch is that *no
bot joins your call* — the app captures your Mac's audio locally and streams it
to a speech-to-text provider (AssemblyAI is one of theirs). The "notetaker" is
the architecture, not a product SKU:

| | what it does | endpoint |
|---|---|---|
| **during** the meeting | live transcript | `wss://streaming.assemblyai.com/v3/ws` |
| **after** the meeting | speaker labels + summary | `https://api.assemblyai.com/v2/transcript` |

That hybrid is exactly what AssemblyAI's own
[meeting-notetaker guidance](https://www.assemblyai.com/docs/meeting-notetaker-best-practices)
recommends, and it's what's built here.

If you *do* want a bot that dials into Zoom/Meet/Teams, that's a different
product category (Recall.ai, Nylas, MeetingBaaS) — several of them transcribe
with AssemblyAI underneath.

---

## What's in here

```
seeed/
  DEMO.md                         runbook: framing, arc, ranked risks
  hello_world/hello_world.ino     blink + serial echo. Proves the toolchain.
  notetaker_ctl/notetaker_ctl.ino button = start/stop, LED = recording status
  relay/notetaker.py              mic/system audio -> streaming API -> live notes
  relay/summarize.py              the saved wav -> speaker labels + summary
  relay/preflight.py              pre-demo check of every moving part
  notes/                          <timestamp>.wav + <timestamp>.md land here
```

---

## Status: deployed and verified on hardware

Both sketches are compiled and flashed; the full pipeline has been run end to
end. See **Verified** at the bottom, and [DEMO.md](DEMO.md) for the runbook.

Steps 1–2 below are already done on this machine — they're kept for the next
machine, and because Step 1 has an Apple Silicon trap in it.

## Step 1 — Arduino toolchain (~5 min)

You have no brew, no arduino-cli, no PlatformIO. Install arduino-cli into
`~/.local/bin` (already on your PATH, and where `gh` already lives):

```bash
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=~/.local/bin sh
```

Then add Seeed's board index and install the SAMD core (~300 MB, one time):

```bash
arduino-cli config init
```

```bash
arduino-cli config add board_manager.additional_urls https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json
```

```bash
arduino-cli core update-index && arduino-cli core install Seeeduino:samd
```

**Apple Silicon trap:** the Seeed SAMD core ships an **x86_64** ARM toolchain.
On an M-series Mac with no Rosetta you get `bad CPU type in executable` at the
first compile. Fix (no password needed, ~30s):

```bash
softwareupdate --install-rosetta --agree-to-license
```

Confirm the board is recognised:

```bash
arduino-cli board list
```

You want to see `/dev/cu.usbmodem1101` with FQBN `Seeeduino:samd:seeed_XIAO_m0`.

> Prefer the GUI? Install Arduino IDE 2.x, then
> Settings → Additional board manager URLs → paste the Seeed URL above →
> Boards Manager → install **Seeed SAMD Boards** → Tools → Board → **Seeed XIAO M0**.

## Step 2 — Hello World

```bash
cd "seeed" && arduino-cli compile -b Seeeduino:samd:seeed_XIAO_m0 hello_world && arduino-cli upload -b Seeeduino:samd:seeed_XIAO_m0 -p /dev/cu.usbmodem1101 hello_world
```

```bash
arduino-cli monitor -p /dev/cu.usbmodem1101 -c baudrate=115200
```

Expected: the orange LED flicks once a second, and you get

```
hello from the XIAO SAMD21
tick 1  uptime=1s
```

Type something and press enter — it echoes back. That's the whole toolchain
proven: compile, upload, USB CDC in both directions.

**If upload fails or the port vanishes:** short the two `RST` pads with tweezers
**twice quickly** to force the UF2 bootloader. The LED will breathe and a new
port appears — upload to that one. This is normal XIAO behaviour, not a brick.

## Step 3 — Python relay

Already set up and installed at `relay/.venv` (numpy, sounddevice, websockets,
requests, pyserial). Your existing `relay/.env` key is picked up automatically —
no key is duplicated and none of it goes near the board.

Find your input device:

```bash
cd "seeed/relay" && ./.venv/bin/python notetaker.py --list-devices
```

Run it — press Enter to start, Enter again to stop:

```bash
cd "seeed/relay" && ./.venv/bin/python notetaker.py --device 0
```

You'll get a live speaker-labelled transcript in the terminal, plus
`notes/<timestamp>.wav` and `notes/<timestamp>.md`.

## Step 4 — The XIAO as the button

Flash the control sketch:

```bash
cd "seeed" && arduino-cli compile -b Seeeduino:samd:seeed_XIAO_m0 notetaker_ctl && arduino-cli upload -b Seeeduino:samd:seeed_XIAO_m0 -p /dev/cu.usbmodem1101 notetaker_ctl
```

Wire a momentary push button between **D1 and GND**. That's the entire circuit —
no resistor, the internal pull-up handles it. (No button yet? The sketch still
runs and the LED still reports status; use Enter to trigger.)

```bash
cd "seeed/relay" && ./.venv/bin/python notetaker.py --device 0 --serial /dev/cu.usbmodem1101
```

- LED **heartbeat** (one blink every 2s) = idle
- LED **solid** = the Mac is genuinely streaming

The light follows the *host*, not the button — if the relay dies, the light
drops back to heartbeat and you know your meeting isn't being recorded.

## Step 5 — The good transcript, after the call

```bash
cd "seeed/relay" && ./.venv/bin/python summarize.py ../notes/2026-08-30-141233.wav
```

Rewrites that meeting's `.md` with a bulleted summary on top and proper
`Speaker A / Speaker B` attribution with timestamps.

---

## The one thing that will actually bite you: system audio

Right now the relay records **your microphone only**. In a real meeting that
captures you and whatever leaks out of your speakers — not the far end.

macOS gives no app the other side of a call without a virtual loopback device.
Granola ships one. You need [BlackHole 2ch](https://existential.audio/blackhole/)
(free, `.pkg` installer — no brew required):

1. Install BlackHole 2ch.
2. **Audio MIDI Setup** → `+` → **Create Multi-Output Device** → tick your
   speakers **and** BlackHole. Set that as your system output so you can still
   hear the call.
3. **Audio MIDI Setup** → `+` → **Create Aggregate Device** → tick your
   microphone **and** BlackHole. That device is you + them on one stream.
4. `--list-devices` again and point `--device` at the aggregate.

Without step 2 you go deaf on the call; without step 3 you only get one side.

**Also:** the first run needs Microphone permission for whichever terminal
you're in — macOS prompts once. If it doesn't, System Settings → Privacy &
Security → Microphone.

And: recording a meeting usually requires everyone's consent. Two-party-consent
states and most workplaces require you to say so out loud.

---

## Verified vs. assumed

Checked live on 2026-08-30 — hardware and API, not just docs:

**On the board**
- arduino-cli 1.5.1 + `Seeeduino:samd@1.8.6`; board auto-detects as
  `Seeeduino:samd:seeed_XIAO_m0`
- `hello_world` compiled (13% flash), flashed, verified: ticks, uptime, and
  bidirectional serial echo
- `notetaker_ctl` compiled, flashed, verified: `PING`→`PONG`, `REC`/`IDLE`
  accepted, LED follows host state

**Full pipeline**
- mic → streaming → notes ran end to end. Transcript came back as *"Hello, this
  is a test of the Seeed Xiao Note Taker built on AssemblyAI"* — keyterms did
  their job on the proper nouns
- `summarize.py` ran against that recording: speaker labels + bulleted summary

**API**

- `wss://streaming.assemblyai.com/v3/ws` accepts every parameter in
  `WS_PARAMS`, including `speech_model=universal-3-5-pro`, `format_turns`,
  `min_turn_silence=560`, `max_turn_silence=2000`
- `mode=max_accuracy` echoed back in `Begin`. The default is `balanced`, which
  trades accuracy for latency you don't need when nobody is waiting on a reply.
  (`min_latency` is the third option — that's the voice-agent setting.)
- `speaker_labels=true` **is** enabled on your key — the server echoed
  `"speaker_labels": true` in its `Begin` message
- `keyterms_prompt` is **one** param holding a JSON array. Repeating it once per
  term — the obvious guess — returns `3006 Invalid JSON array` and closes the
  socket. The working form is in `notetaker.py`.
- **pre-recorded API: `speech_model` (singular) is deprecated and 400s.** Use
  `speech_models: ["universal-3-5-pro", "universal-2"]` — a plural fallback
  chain. Omitting it silently downgrades you to an older model, which is why the
  first post-call pass read *worse* than the live one (`"assembly. AI"`,
  `"Notaker"`). Not mentioned on the quickstart page; found by probing.
- audio ingested and `Termination` returned cleanly

**Still open**
- no system-audio loopback installed, so capture is **your mic only** — see
  below. This is the one thing standing between this and a real meeting.
- the button isn't wired yet (D1 to GND); `preflight.py` and Enter both work
  without it

## If you'd rather have the mic on the board

Get a **XIAO ESP32S3 Sense** (~$14). Then the PDM mic and WiFi are onboard, and
`notetaker.py` becomes a WebSocket server the board connects to — the same shape
as `relay/voice_agent.py` in the vintage phone project. The `notetaker_ctl`
protocol here transfers unchanged; only the transport swaps from USB serial to
WiFi.
