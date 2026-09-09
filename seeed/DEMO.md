# Demo runbook

## The framing that makes this land

Don't demo "a transcription script" — that's an API call, and nobody claps for
an API call. Demo **a physical button for an AI notetaker.**

The honest version of the story is also the better one:

> This board has no microphone. It has 32 KB of RAM and no radio. It could never
> do the transcription — and it doesn't have to. It's the *interface*. Everything
> AI now buries three menus deep, this puts back under your thumb: one button,
> one light that never lies about whether you're recording.

That reframes the SAMD21's limits from an apology into the point. The light is
the argument — it follows the *host*, not the button, so if the pipeline dies
the light drops to heartbeat. You always know whether you're actually recording.
Most notetaker UIs cannot honestly claim that.

## Wiring (do this first)

Momentary push button between **D1 and GND**. That's the whole circuit.

No button? A jumper wire — or a straightened paperclip — touched between D1 and
GND works identically and is arguably a better prop. Tapping two wires together
reads as *hardware* in a way a polished enclosure doesn't.

## T-minus 10 minutes

```bash
cd "/Users/harnoor/Developer/ESp32 Vintage Phone/seeed/relay" && ./.venv/bin/python preflight.py
```

Checks the board, PING/PONG, mic, audio routing, API key, and opens a real
streaming session to confirm model + mode + speaker labels. Exits non-zero on
anything that would bite you. **Run it on the demo network** — a captive portal
or blocked WSS is the failure it exists to catch.

Then flash the board **before** anyone is watching:

```bash
cd "/Users/harnoor/Developer/ESp32 Vintage Phone/seeed" && arduino-cli compile -b Seeeduino:samd:seeed_XIAO_m0 notetaker_ctl && arduino-cli upload -b Seeeduino:samd:seeed_XIAO_m0 -p /dev/cu.usbmodem1101 notetaker_ctl
```

Never flash live. The 1200-baud bootloader touch drops the USB port and the
first upload attempt sometimes loses the race — it happened once during this
build. Fine at your desk, fatal on stage. If it does fail: just run it again;
if the port is gone, double-tap the RST pads and re-upload.

## The run

```bash
cd "/Users/harnoor/Developer/ESp32 Vintage Phone/seeed/relay" && ./.venv/bin/python notetaker.py --device 0 --serial /dev/cu.usbmodem1101
```

Make the terminal font big. The live transcript IS the visual.

**1. The light (15s).** Board on the table, terminal behind. LED is on
heartbeat. "It's idle. Watch the light."

**2. Press it.** LED goes solid. Say a sentence with a proper noun in it —
"We're building this on AssemblyAI's universal three five pro model." Let the
transcript land on screen. The proper nouns come out right because they're in
`KEYTERMS`; that's worth pointing at.

**3. Get a second voice in.** Ask someone a question. `speaker_labels` splits
you into Speaker A and B live. This is the moment that separates it from a
dictation toy — do not skip it.

**4. Press again.** LED returns to heartbeat, file paths print. Open the `.md`.

**5. The second pass.**

```bash
cd "/Users/harnoor/Developer/ESp32 Vintage Phone/seeed/relay" && ./.venv/bin/python summarize.py ../notes/<stamp>.wav
```

Same audio, re-transcribed with full context plus a bulleted summary. "Live
gives you the feed. This gives you the notes." Have a **pre-recorded wav from
your dry run** ready to use here instead — it's a network round trip and a
silent 20 seconds is a long time in front of a room.

## Dry-run without talking to yourself

```bash
cd "/Users/harnoor/Developer/ESp32 Vintage Phone/seeed/relay" && ./.venv/bin/python notetaker.py --device 0 & sleep 3; say -r 160 "Testing the Seeed XIAO notetaker built on AssemblyAI."
```

Your speakers feed your mic. Good enough to prove the path end to end.

## Known risks, ranked

| risk | likelihood | mitigation |
|---|---|---|
| **Only your voice is captured** | **certain** | BlackHole + Aggregate device (README). Without it you get your side only — if the demo has a remote participant, this sinks it. |
| Conference wifi blocks WSS | medium | preflight on the demo network; phone hotspot as backup |
| Live re-flash fails | medium | flash beforehand; never live |
| Room noise garbles the transcript | medium | sit close to the laptop; add jargon to `KEYTERMS` |
| Someone asks about privacy | high | good question, not a risk — audio is captured locally and the wav stays on disk; you choose what to upload. Say so plainly. |

## Questions you will get

**"Why not just use Granola?"** You could. This is the same architecture —
local capture, no bot in the meeting — with the control surface pulled out into
hardware and the transcript pipeline fully in your hands.

**"Why does the board do so little?"** Because it should. The mic is on the
Mac, which already has a good one. Adding a worse mic to a 32 KB MCU buys
nothing. If you want it standalone, that's a XIAO ESP32S3 Sense and the same
protocol.

**"Does it work with Zoom/Meet/Teams?"** All of them — it never touches them.
It captures device audio, so the platform is irrelevant. That's the whole
advantage of the no-bot approach.

**"What's the latency?"** ~300 ms for partials. It's running `max_accuracy`
mode, which trades some latency for a better transcript — the right trade when
nobody's waiting on a reply.
