#!/usr/bin/env python3
"""
Bridge: ESP32 microphone -> AssemblyAI Voice Agent -> Mac speakers + live web UI.

  ws://<mac>:8080/      the ESP32 pushes raw 24 kHz 16-bit PCM here
  ws://<mac>:8080/ui    the browser page subscribes to transcripts here
  http://localhost:8081/ui.html   the page itself

Run:  ~/esp-tools/bin/python -u relay.py

Echo control (half-duplex)
--------------------------
The mic and the speakers share a room, so the agent hears itself and replies
to its own voice. We hold the microphone shut for as long as the operator is
actually speaking. Note that reply.done fires well BEFORE the sound finishes
playing -- audio arrives faster than real time -- so the gate is driven by the
duration of audio queued for playback, not by the message. TAIL_S covers the
speaker -> air -> microphone flight time and the room's reverb.

Protocol notes (verified live; the docs omit these):
  * session.update nests under "session"; the server echoes it back as "config"
  * outbound audio -> {"type":"input.audio","audio": b64}
  * inbound  audio -> {"type":"reply.audio","data":  b64}
  * ONLY 24000 Hz works; other rates are accepted, then fail with internal_error

Echo control
------------
The authoritative gate is on the ESP32, which can see its own DAC buffer and
therefore knows when sound is genuinely still leaving the speaker. The window
kept here is only a coarse backstop -- relay-side timing runs early by the
device buffer depth plus the network hop, which is exactly how the operator
used to end up transcribing itself.
"""
import asyncio, base64, functools, http.server, json, queue, re, socketserver, sys, threading, time
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import sounddevice as sd
import websockets

AGENT_URL = "wss://agents.assemblyai.com/v1/ws"
RATE      = 24_000
WS_PORT   = 8080
UI_PORT   = 8081
TAIL_S    = 0.20        # coarse backstop; the ESP32 now owns the real gate
PROFILE   = "kids"      # "kids" or "operator" -- see PROFILES below
TO_PHONE  = True        # route the reply to the ESP32 speaker, not the Mac
PHONE_RATE = 8000       # what the ESP32 plays; 24k/8k = decimate by 3
PHONE_VOL  = 0.10        # speaker level, 0.0-1.0 (change and restart the relay)
HERE      = Path(__file__).parent
LOG       = HERE / "session.jsonl"
NOTES_LOG = HERE / "notes.jsonl"      # the notetaker's record, one turn per line

# Notes run on AssemblyAI's Streaming API, in parallel with the Voice Agent.
# The agent is tuned to reply fast; this one is tuned to be RIGHT, and it
# labels speakers, which the agent's own transcript does not.
# Verified live 2026-09-29, at this mic's native 24 kHz (no resampling):
#   header  Authorization: <key>          -- no "Bearer", unlike the agent
#   send    raw binary PCM frames, then {"type":"Terminate"}
#   recv    Begin / SpeechStarted / Turn / Termination
#   speaker arrives as "speaker_label" ("A", "B", ...) -- NOT "speaker"
NOTES_URL = "wss://streaming.assemblyai.com/v3/ws"
NOTES_PARAMS = {
    "sample_rate": 24000, "encoding": "pcm_s16le",
    "speech_model": "universal-3-5-pro",
    "format_turns": "true",            # punctuation and casing -- this is for reading
    "speaker_labels": "true",
    "mode": "max_accuracy",            # the agent wants speed; notes want accuracy
    "min_turn_silence": 560, "max_turn_silence": 2000,
}
# Every audio message must be 50-1000 ms long, or the server closes the socket
# with 3007 "Input Duration Violation". The phone sends 320-sample frames --
# 13 ms -- which the Voice Agent accepts and this API does not. So the
# notetaker batches them into 100 ms (2400 samples x 2 bytes) before sending.
NOTES_CHUNK = 4800

# In notes mode Pip is not listening, so it cannot be asked to come back.
# The notetaker still is -- so it watches for the name instead. "Pip" is short
# and gets misheard ("Phil" came back once in testing), so treat it as a
# convenience; the dashboard toggle is the reliable way back.
WAKE = re.compile(r"\b(?:hey\s+)?(?:pip|pippa)\b", re.I)
TESTLOG   = HERE / "tests.jsonl"

# Two personalities. Switch with PROFILE above; the voice is fixed for the
# life of a session, so changing either one means restarting the relay.
PROFILES = {
    "kids": {
        "voice": "mary",
        "name": "Pip",
        "greeting": "Hello there! Pip speaking. What would you like to talk about?",
        "system_prompt": (
            "Your name is Pip. You are a friendly voice on a magic telephone "
            "that children call. You are warm, patient and playful.\n"
            "- If a child asks you to be louder or quieter, or to change the "
            "volume, use your volume tools and then say what you did in a few "
            "words.\n"
            "Always follow these rules:\n"
            "- Speak as if to a six-year-old: short sentences, simple words, "
            "one idea at a time.\n"
            "- Keep every reply to one or two sentences. This is a phone call, "
            "not a story.\n"
            "- Be encouraging and curious, and ask simple questions back.\n"
            "- Never say anything frightening, violent or grown-up, and do not "
            "tell scary stories even if asked.\n"
            "- Never ask for private details such as where they live, their "
            "school, their address or a phone number.\n"
            "- If a child says someone is hurt, that they feel unsafe, or that "
            "something frightening is happening for real, gently tell them to "
            "go and tell a grown-up they trust straight away.\n"
            "- Children mumble and trail off. If you did not understand, "
            "cheerfully ask them to say it again rather than guessing.\n"
            "- You are a fun voice on a phone, not a substitute for real "
            "friends or family. Encourage playing with real people too."
        ),
    },
    "operator": {
        "voice": "vera",
        "name": "Operator",
        "greeting": "Operator speaking. How may I connect your call?",
        "system_prompt": (
            "You are the operator of a vintage telephone exchange, answering a "
            "rotary phone from 1955. Warm, unhurried, a little formal. Keep "
            "replies to one or two sentences - this is a phone call."
        ),
    },
}

_p = PROFILES[PROFILE]
VOICE = _p["voice"]
NAME  = _p.get("name", "Operator")

# The agent can turn its own speaker up and down. Shapes verified against the
# live API: tools are declared under session.tools, the server invokes with
# "tool.call" carrying call_id/name/arguments, and the reply is "tool.result"
# whose "result" must be a JSON-ENCODED STRING, not a nested object.
TOOLS = [
    {
        "type": "function",
        "name": "start_listening",
        "description": ("Stop talking and quietly take notes instead. Use when "
                        "someone says 'just listen', 'take notes', 'be quiet for "
                        "a bit' or 'I want to think out loud'. Say a short goodbye "
                        "first. You will not hear anything until someone says your "
                        "name again."),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "set_volume",
        "description": ("Set the telephone speaker volume to a specific level. "
                        "Use when someone names a level, like 'set it to half'."),
        "parameters": {
            "type": "object",
            "properties": {"percent": {
                "type": "number",
                "description": "Loudness from 0 (silent) to 100 (loudest)."}},
            "required": ["percent"],
        },
    },
    {
        "type": "function",
        "name": "adjust_volume",
        "description": ("Make the telephone speaker louder or quieter by one "
                        "step. Use for 'turn it up', 'too loud', 'I can't hear you'."),
        "parameters": {
            "type": "object",
            "properties": {"direction": {
                "type": "string", "enum": ["up", "down"],
                "description": "Which way to move the volume."}},
            "required": ["direction"],
        },
    },
]

SESSION = {
    "type": "session.update",
    "session": {
        "system_prompt": _p["system_prompt"],
        "greeting": _p["greeting"],
        "tools": TOOLS,
        # Verified live: voice_focus takes "near-field"/"far-field", NOT a
        # boolean. near-field suits a handset held to the mouth. turn_detection
        # also carries undocumented "type" and "interruption_delay" fields.
        "input": {
            "format": {"encoding": "audio/pcm", "sample_rate": RATE},
            "voice_focus": "near-field",
            "voice_focus_threshold": 0.5,
            "turn_detection": {
                "vad_threshold": 0.5,
                "min_silence": 900,     # children trail off mid-sentence
                "max_silence": 3000,
                "interrupt_response": True,
            },
        },
        "output": {"voice": VOICE,
                   "format": {"encoding": "audio/pcm", "sample_rate": RATE}},
    },
}

# ------------------------------------------------------------------ speakers
spk_q: queue.Queue[bytes] = queue.Queue()
_tail = bytearray()

def on_speaker(outdata, frames, time_info, status):
    want = frames * 2
    while len(_tail) < want:
        try:
            _tail.extend(spk_q.get_nowait())
        except queue.Empty:
            _tail.extend(b"\x00" * (want - len(_tail)))
            break
    outdata[:] = np.frombuffer(bytes(_tail[:want]), dtype=np.int16).reshape(-1, 1)
    del _tail[:want]

# ------------------------------------------------------------------ browser fanout
ui_clients: set = set()
state = {"esp": False, "agent": False, "muted": False}

# Everything the dashboard can see or change while running.
esp_ws  = None                      # the phone's socket, when it is connected
capture = None                      # collects mic audio during an echo test
runtime = {"vol": PHONE_VOL, "to_phone": TO_PHONE, "gate": True,
           "mic": "phone",          # "phone" = MAX9814, "mac" = built-in
           "mode": "talk",          # "talk" = Pip answers + notes; "notes" = listen only
           "pending_mode": None}    # a switch waiting for Pip to finish speaking

# The Mac's microphone, as a fallback source. The stream always runs; the
# callback only queues audio while that source is selected, so switching is
# instant and does not need the device re-opened.
mic_q: "queue.Queue[bytes]" = queue.Queue(maxsize=120)

def on_mac_mic(indata, frames, time_info, status):
    if runtime["mic"] != "mac":
        return
    try:
        mic_q.put_nowait(bytes(indata))
    except queue.Full:
        pass
stats   = {"sent_s": 0.0, "echo_s": 0.0, "turns": 0, "rms": 0.0, "queued": 0}


def synth(freqs, ms, vol, glide=None):
    """8-bit unsigned tone at the phone's rate, with short fades so the
    speaker does not click on and off."""
    n = int(PHONE_RATE * ms / 1000)
    t = np.arange(n) / PHONE_RATE
    if glide:
        f = freqs[0] * (glide / freqs[0]) ** (np.arange(n) / n)
        sig = np.sin(2 * np.pi * np.cumsum(f) / PHONE_RATE)
    else:
        sig = sum(np.sin(2 * np.pi * f * t) for f in freqs) / len(freqs)
    fade = int(PHONE_RATE * 0.006)
    if n > 2 * fade:
        env = np.ones(n)
        env[:fade] = np.linspace(0, 1, fade)
        env[-fade:] = np.linspace(1, 0, fade)
        sig = sig * env
    return ((sig * 110 * vol) + 128).clip(0, 255).astype(np.uint8).tobytes()


async def send_to_phone(data: bytes) -> bool:
    """Play generated audio out of whichever speaker is selected. The samples
    arrive as unsigned 8-bit at PHONE_RATE; the Mac path needs signed 16-bit at
    RATE, so convert rather than refusing when there is no phone attached."""
    if esp_ws is None or not runtime["to_phone"]:
        a = np.frombuffer(data, dtype=np.uint8).astype(np.float32)
        pcm = (a - 128.0) * 256.0                       # u8 -> s16
        reps = max(1, RATE // PHONE_RATE)               # 8 k -> 24 k
        pcm = np.repeat(pcm, reps)
        spk_q.put(pcm.clip(-32768, 32767).astype(np.int16).tobytes())
        return True
    try:
        await esp_ws.send("flush")
        step = PHONE_RATE // 10                     # 100 ms per chunk
        for i in range(0, len(data), step):
            await esp_ws.send(data[i:i + step])
            await asyncio.sleep(0.09)
        return True
    except Exception as e:
        await to_ui({"type": "note", "text": f"send failed: {e}"})
        return False


async def echo_test(seconds: float = 3.0):
    """Record from the microphone, then play it straight back out of the
    speaker. Exercises both halves in one go."""
    global capture
    capture = bytearray()
    await to_ui({"type": "note", "text": f"recording {seconds:.0f}s - say something"})
    await asyncio.sleep(seconds)
    raw, capture = bytes(capture), None
    if len(raw) < 4000:
        await to_ui({"type": "note",
                     "text": "captured almost nothing - mic muted or unplugged?"})
        record_test("mic echo", False, f"only {len(raw)} bytes captured")
        return
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    a = a[:len(a) // 3 * 3].reshape(-1, 3).mean(axis=1)        # 24k -> 8k
    u8 = ((a / 256) * runtime["vol"] + 128).clip(0, 255).astype(np.uint8).tobytes()
    peak = int(np.abs(a).max())
    await to_ui({"type": "note", "text": f"playing back {len(u8)/PHONE_RATE:.1f}s"})
    ok = await send_to_phone(u8)
    record_test("mic echo", ok,
                f"{len(u8)/PHONE_RATE:.1f}s captured, peak {peak}/32767")


async def run_tool(name: str, args: dict) -> dict:
    """Apply a tool the agent invoked. Returns whatever it should hear back."""
    if name == "start_listening":
        # Defer the switch until Pip finishes its goodbye: switching now would
        # discard the very reply that says it. Applied on the next reply.done,
        # with a fallback in case no reply comes.
        runtime["pending_mode"] = "notes"

        async def fallback():
            await asyncio.sleep(8)
            if runtime.get("pending_mode") == "notes":
                await set_mode("notes", why=f"{NAME} was asked to listen")
        asyncio.create_task(fallback())
        return {"mode": "listening",
                "note": "notes are being taken; you are muted until your name is said"}

    before = runtime["vol"]
    if name == "set_volume":
        runtime["vol"] = max(0.0, min(1.0, float(args.get("percent", 50)) / 100))
    elif name == "adjust_volume":
        step = 0.25 if args.get("direction") == "up" else -0.25
        runtime["vol"] = max(0.0, min(1.0, before + step))
    else:
        return {"error": f"no such tool: {name}"}

    pct = round(runtime["vol"] * 100)
    print(f"  [tool] {name}{args} -> volume {pct}%", flush=True)
    await to_ui({"type": "note", "text": f"{NAME} set the volume to {pct}%"})
    if runtime["vol"] >= 1.0 and before >= 1.0:
        return {"volume_percent": pct, "note": "already at maximum"}
    if runtime["vol"] <= 0.0:
        return {"volume_percent": 0, "note": "now silent"}
    return {"volume_percent": pct}


def record_note(src: str, speaker: str, text: str, conf: float | None = None):
    """One finished turn of notes. `src` is "mic" (the notetaker heard it) or
    "agent" (Pip said it -- taken from the agent's own clean transcript rather
    than re-transcribed off the speaker)."""
    row = {"t": time.time(), "src": src, "speaker": speaker, "text": text}
    if conf is not None:
        row["conf"] = round(conf, 2)
    with NOTES_LOG.open("a") as f:
        f.write(json.dumps(row) + "\n")
    return row


async def set_mode(mode: str, why: str = ""):
    runtime["pending_mode"] = None
    if mode not in ("talk", "notes") or runtime["mode"] == mode:
        return
    runtime["mode"] = mode
    if mode == "notes":
        # Stop anything Pip was mid-way through saying. A reply already being
        # generated would otherwise finish playing after the switch.
        while not spk_q.empty():
            spk_q.get_nowait()
        _tail.clear()
        if esp_ws is not None and runtime["to_phone"]:
            try:
                await esp_ws.send("flush")
            except Exception:
                pass
    label = "talking - Pip answers" if mode == "talk" else "listening - notes only"
    print(f"  [mode] {label}{f'  ({why})' if why else ''}", flush=True)
    await to_ui({"type": "note", "text": f"now {label}"})
    await to_ui({"type": "mode", "mode": mode})


async def telemetry():
    while True:
        await asyncio.sleep(0.3)
        if ui_clients:
            await to_ui({"type": "stats", **stats, **state,
                         "vol": runtime["vol"], "profile": PROFILE, "voice": VOICE,
                         "to_phone": runtime["to_phone"], "gate": runtime["gate"],
                         "mic": runtime["mic"], "mode": runtime["mode"],
                         "notes_live": state.get("notes", False)})

async def to_ui(msg: dict):
    if not ui_clients:
        return
    dead, payload = [], json.dumps(msg)
    for c in list(ui_clients):
        try:
            await c.send(payload)
        except Exception:
            dead.append(c)
    for c in dead:
        ui_clients.discard(c)

async def push_status():
    await to_ui({"type": "status", **state})

def record(who: str, text: str):
    with LOG.open("a") as f:
        f.write(json.dumps({"t": time.time(), "who": who, "text": text}) + "\n")


def record_test(test: str, ok: bool, detail: str = ""):
    with TESTLOG.open("a") as f:
        f.write(json.dumps({"t": time.time(), "test": test,
                            "ok": ok, "detail": detail}) + "\n")

def pick_device(want: str = "reSpeaker"):
    """Prefer the reSpeaker if it is plugged in, else the system default.
    Returns (input_index, output_index) or (None, None) for the default."""
    try:
        for i, d in enumerate(sd.query_devices()):
            if want.lower() in d["name"].lower():
                ins  = i if d["max_input_channels"]  > 0 else None
                outs = i if d["max_output_channels"] > 0 else None
                print(f"audio  using '{d['name']}' (device {i})", flush=True)
                return ins, outs
    except Exception:
        pass
    return None, None


def load_key() -> str:
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("ASSEMBLYAI_API_KEY="):
                k = line.split("=", 1)[1].strip()
                if k:
                    return k
    sys.exit("No API key in relay/.env")

# ------------------------------------------------------------------ one call
async def handle_phone(esp):
    """One call. `esp` is the phone's socket, or None for a Mac-only call --
    which is how the reSpeaker works as a standalone USB mic and speaker with
    no ESP32 in the picture at all."""
    global esp_ws
    esp_ws = esp
    # The device tells us what it wants; nothing to keep in sync by hand.
    #   "fmt:u8@8000"      HW-104 on the 8-bit DAC  -> decimate + convert
    #   "fmt:pcm16@24000"  NS4168 over I2S          -> pass 16-bit straight through
    fmt = {"bits": 8, "rate": 8000}
    state["esp"] = esp is not None
    if esp is None:
        runtime["mic"] = "mac"          # there is no phone mic to use
        runtime["to_phone"] = False     # ...and nowhere to send audio but here
    await push_status()
    who = esp.remote_address[0] if esp is not None else "the Mac (no phone)"
    print(f"\n=== call started from {who} ===", flush=True)
    record("_call", "start")

    try:
        async with websockets.connect(
            AGENT_URL, additional_headers={"Authorization": f"Bearer {load_key()}"},
            max_size=None,
        ) as agent:
            await agent.send(json.dumps(SESSION))
            ready = asyncio.Event()
            line: list[str] = []
            sent = dropped = 0
            speak_until = 0.0        # monotonic time the speakers fall silent

            # Audio for the notetaker. Bounded so a stalled notes connection
            # drops frames instead of growing without limit or blocking Pip.
            notes_q: asyncio.Queue = asyncio.Queue(maxsize=400)

            def to_notes(frame: bytes):
                try:
                    notes_q.put_nowait(frame)
                except asyncio.QueueFull:
                    pass

            async def esp_to_agent():
                if esp is None:
                    await asyncio.Future()      # nothing to read; just park
                nonlocal sent, dropped
                await ready.wait()
                n = 0
                was_muted = None
                async for frame in esp:
                    if isinstance(frame, str):
                        if frame.startswith("img:"):
                            stats["frames"] = stats.get("frames", 0) + 1
                            await to_ui({"type": "image", "data": frame[4:]})
                            continue
                        if frame.startswith("fmt:"):
                            spec = frame[4:]
                            enc, _, rate = spec.partition("@")
                            fmt["bits"] = 16 if enc == "pcm16" else 8
                            fmt["rate"] = int(rate or 8000)
                            print(f"  [fmt] phone wants {enc} @ {fmt['rate']} Hz",
                                  flush=True)
                            await to_ui({"type": "note",
                                         "text": f"speaker format: {enc} @ {fmt['rate']} Hz"})
                        continue
                    if not frame:
                        continue
                    if runtime["mic"] != "phone":
                        continue                      # Mac mic has the floor

                    gated = (runtime["gate"]
                             and time.monotonic() < speak_until + TAIL_S)
                    if gated != was_muted:
                        was_muted = gated
                        state["muted"] = gated
                        await push_status()

                    if capture is not None:
                        capture.extend(frame)

                    a = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
                    stats["rms"] = min(float(np.sqrt((a * a).mean())) / 6000.0, 1.0)

                    if gated:
                        dropped += len(frame)     # the operator is talking; ignore the room
                        stats["echo_s"] = dropped / 2 / RATE
                        continue

                    n += 1
                    if n % 5 == 0:
                        await to_ui({"type": "level", "rms": stats["rms"]})

                    # Fan out. Echo-gated frames never reach here, so neither
                    # stream transcribes Pip's own voice off the speaker.
                    to_notes(frame)                   # the notetaker always hears
                    if runtime["mode"] != "talk":
                        continue                      # Pip only hears in talk mode
                    sent += len(frame)
                    stats["sent_s"] = sent / 2 / RATE
                    await agent.send(json.dumps({
                        "type": "input.audio",
                        "audio": base64.b64encode(frame).decode(),
                    }))

            async def mac_to_agent():
                """Same job as esp_to_agent, but sourced from the laptop. Uses
                the relay-side gate, since the device gate only governs the
                ESP32's own microphone."""
                nonlocal sent, dropped
                await ready.wait()
                loop = asyncio.get_running_loop()
                while True:
                    frame = await loop.run_in_executor(None, mic_q.get)
                    if runtime["mic"] != "mac":
                        continue
                    if runtime["gate"] and time.monotonic() < speak_until + TAIL_S:
                        dropped += len(frame)
                        stats["echo_s"] = dropped / 2 / RATE
                        state["muted"] = True
                        continue
                    state["muted"] = False
                    a = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
                    stats["rms"] = min(float(np.sqrt((a * a).mean())) / 6000.0, 1.0)
                    to_notes(frame)
                    if runtime["mode"] != "talk":
                        continue
                    sent += len(frame)
                    stats["sent_s"] = sent / 2 / RATE
                    await agent.send(json.dumps({
                        "type": "input.audio",
                        "audio": base64.b64encode(frame).decode(),
                    }))

            async def agent_to_out():
                nonlocal speak_until
                async for raw in agent:
                    m = json.loads(raw)
                    t = m.get("type", "")

                    if t == "reply.audio":
                        if runtime["mode"] == "notes":
                            continue        # listening only: Pip stays silent
                        pcm = base64.b64decode(m["data"])
                        if runtime["to_phone"]:
                            a = np.frombuffer(pcm, dtype=np.int16)
                            if fmt["bits"] == 16:
                                # NS4168 over I2S: the agent already speaks
                                # 24 kHz 16-bit, so only the level changes.
                                out = (a * runtime["vol"]).clip(-32768, 32767)
                                await esp.send(out.astype(np.int16).tobytes())
                            else:
                                # 24 kHz 16-bit -> 8 kHz 8-bit unsigned for the
                                # built-in DAC. Averaging each group of three
                                # avoids the aliasing plain decimation folds in.
                                k = (len(a) // 3) * 3
                                if k:
                                    d = a[:k].reshape(-1, 3).mean(axis=1)
                                    u8 = (d / 256 * runtime["vol"] + 128).clip(0, 255)
                                    await esp.send(u8.astype(np.uint8).tobytes())
                        else:
                            spk_q.put(pcm)
                        # extend the quiet window by this chunk's real duration
                        dur = len(pcm) / 2 / RATE
                        speak_until = max(speak_until, time.monotonic()) + dur
                    elif t == "transcript.agent.delta":
                        line.append(m.get("delta", ""))
                    elif t in ("transcript.agent", "reply.done"):
                        if t == "reply.done" and runtime.get("pending_mode"):
                            await set_mode(runtime["pending_mode"],
                                           why=f"{NAME} was asked to listen")
                        if line:
                            # deltas carry their own spacing; collapse the doubles
                            said = " ".join(" ".join(line).split()); line.clear()
                            print(f"  operator: {said}", flush=True)
                            record("operator", said)
                            stats["turns"] += 1
                            await to_ui({"type": "agent", "text": said})
                            # Pip's side of the notes comes from its own clean
                            # transcript, not re-transcribed off the speaker.
                            row = record_note("agent", NAME, said)
                            await to_ui({"type": "note_turn", **row})
                    elif t.startswith("transcript.user"):
                        txt = (m.get("text") or m.get("transcript")
                               or m.get("delta") or "").strip()
                        if txt:
                            final = not t.endswith(".delta") and not m.get("partial")
                            print(f"      you: {txt}", flush=True)
                            if final:
                                record("you", txt)
                            await to_ui({"type": "user" if final else "partial",
                                         "text": txt})
                    elif t == "session.ready":
                        ready.set(); state["agent"] = True; await push_status()
                        print("  agent ready - speak into the mic\n", flush=True)
                    elif t == "reply.started":
                        while not spk_q.empty():
                            spk_q.get_nowait()
                        _tail.clear()
                        if runtime["to_phone"]:
                            await esp.send("flush")     # drop stale audio on the phone
                        speak_until = time.monotonic()
                    elif t == "tool.call":
                        out = await run_tool(m.get("name", ""),
                                             m.get("arguments") or {})
                        await agent.send(json.dumps({
                            "type": "tool.result",
                            "call_id": m.get("call_id"),
                            "result": json.dumps(out),   # must be a string
                        }))
                    elif t == "session.error":
                        print(f"  !! {m.get('code')}: {m.get('message')}", flush=True)
                        await to_ui({"type": "note",
                                     "text": f"agent error: {m.get('code')}"})

            async def notes_stream():
                """The notetaker: a second, independent connection to the
                Streaming API. A failure here must never end the call, so it
                contains its own errors and reconnects."""
                url = f"{NOTES_URL}?{urlencode(NOTES_PARAMS)}"
                key = load_key()
                while True:
                    try:
                        async with websockets.connect(
                            url, additional_headers={"Authorization": key},
                            max_size=None, open_timeout=15,
                        ) as ns:
                            while not notes_q.empty():      # stale while we were away
                                notes_q.get_nowait()

                            async def pump():
                                buf = bytearray()
                                while True:
                                    buf += await notes_q.get()
                                    while len(buf) >= NOTES_CHUNK:
                                        await ns.send(bytes(buf[:NOTES_CHUNK]))
                                        del buf[:NOTES_CHUNK]

                            async def read():
                                async for raw in ns:
                                    m = json.loads(raw)
                                    t = m.get("type")
                                    if t == "Begin":
                                        state["notes"] = True
                                        await push_status()
                                        print("  [notes] notetaker listening", flush=True)
                                    elif t == "Turn":
                                        text = (m.get("transcript") or "").strip()
                                        if not text:
                                            continue
                                        spk = m.get("speaker_label") or "?"
                                        if m.get("end_of_turn") and m.get("turn_is_formatted"):
                                            row = record_note("mic", spk, text,
                                                              m.get("speaker_confidence"))
                                            print(f"  [notes] {spk}: {text}", flush=True)
                                            await to_ui({"type": "note_turn", **row})
                                            if runtime["mode"] == "notes" and WAKE.search(text):
                                                await set_mode("talk", why=f'heard "{text[:40]}"')
                                        else:
                                            await to_ui({"type": "note_partial",
                                                         "speaker": spk, "text": text})
                                    elif t == "Error":
                                        print(f"  [notes] server error "
                                              f"{m.get('error_code')}: {m.get('error')}",
                                              flush=True)
                                    elif t == "Termination":
                                        return

                            jobs = [asyncio.create_task(pump()),
                                    asyncio.create_task(read())]
                            try:
                                done, _ = await asyncio.wait(
                                    jobs, return_when=asyncio.FIRST_COMPLETED)
                                for j in done:
                                    j.result()              # surface the error, if any
                            except asyncio.CancelledError:
                                # hanging up: ask the server to finalise the
                                # last turn rather than just dropping the line
                                try:
                                    await asyncio.wait_for(
                                        ns.send(json.dumps({"type": "Terminate"})), 1)
                                except Exception:
                                    pass
                                raise
                            finally:
                                for j in jobs:
                                    j.cancel()
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        print(f"  [notes] dropped ({type(e).__name__}: {e}); "
                              f"retrying in 2s", flush=True)
                    finally:
                        state["notes"] = False
                    await asyncio.sleep(2)

            # A task of its own, cancelled on hang-up. Inside the gather below it
            # would outlive the call: gather does not cancel siblings when one
            # fails, so a dead call would leave a billed stream reconnecting.
            notes_task = asyncio.create_task(notes_stream())
            try:
                await asyncio.gather(esp_to_agent(), mac_to_agent(), agent_to_out())
            except websockets.ConnectionClosed:
                pass
            finally:
                notes_task.cancel()
                record("_call", "end")
                print(f"=== hung up ({sent/2/RATE:.1f}s sent, "
                      f"{dropped/2/RATE:.1f}s muted as echo) ===", flush=True)
    finally:
        globals()["esp_ws"] = None
        state["esp"] = state["agent"] = state["muted"] = False
        await push_status()

async def handle_ui(ws):
    ui_clients.add(ws)
    await push_status()
    try:
        async for raw in ws:
            try:
                m = json.loads(raw)
            except Exception:
                continue
            c, v = m.get("cmd"), runtime["vol"]
            if c == "beep":
                ok = await send_to_phone(synth([1000], 700, v))
                record_test("beep", ok, f"1 kHz 0.7s at {v:.0%}")
            elif c == "dialtone":
                ok = await send_to_phone(synth([350, 440], 1600, v))
                record_test("dial tone", ok, f"350+440 Hz at {v:.0%}")
            elif c == "ring":
                ok = True
                for _ in range(2):
                    ok = await send_to_phone(synth([440, 480], 900, v)) and ok
                    await asyncio.sleep(0.3)
                record_test("ring", ok, f"440+480 Hz x2 at {v:.0%}")
            elif c == "sweep":
                ok = await send_to_phone(synth([200], 1600, v, glide=3000))
                record_test("sweep", ok, f"200 Hz -> 3 kHz at {v:.0%}")
            elif c == "echo":
                asyncio.create_task(echo_test())
            elif c == "call":
                if state["esp"] or state["agent"]:
                    await to_ui({"type": "note", "text": "a call is already open"})
                else:
                    asyncio.create_task(handle_phone(None))
            elif c == "hangup":
                globals()["hangup_flag"] = True
            elif c == "mode":
                await set_mode(m.get("value", ""), why="dashboard")
            elif c == "mic":
                v2 = m.get("value")
                if v2 in ("phone", "mac"):
                    runtime["mic"] = v2
                    while not mic_q.empty():
                        mic_q.get_nowait()
                    label = "MAX9814 on the phone" if v2 == "phone" else "the Mac's mic"
                    print(f"  [mic] source -> {v2}", flush=True)
                    await to_ui({"type": "note", "text": f"listening through {label}"})
            elif c == "volume":
                runtime["vol"] = max(0.0, min(1.0, float(m.get("value", 0.5))))
            elif c == "output":
                runtime["to_phone"] = (m.get("value") == "phone")
                where = "phone speaker" if runtime["to_phone"] else "Mac speakers"
                if not runtime["to_phone"] and esp_ws is not None:
                    try:
                        await esp_ws.send("flush")
                    except Exception:
                        pass
                print(f"  [out] -> {where}", flush=True)
                await to_ui({"type": "note", "text": f"output -> {where}"})
                record_test("output route", True, where)
            elif c == "gate":
                runtime["gate"] = bool(m.get("value"))
                mode = ("mic muted while speaking" if runtime["gate"]
                        else "server turn detection only")
                if esp_ws is not None:
                    try:
                        await esp_ws.send("gate:on" if runtime["gate"] else "gate:off")
                    except Exception:
                        pass
                print(f"  [gate] {mode}", flush=True)
                await to_ui({"type": "note", "text": f"echo control: {mode}"})
                record_test("echo mode", True, mode)
            elif c == "clear":
                await to_ui({"type": "clear"})
    except websockets.ConnectionClosed:
        pass                    # a closed browser tab is not an error
    finally:
        ui_clients.discard(ws)


async def router(ws):
    if ws.request.path.rstrip("/").endswith("ui"):
        await handle_ui(ws)
    else:
        await handle_phone(ws)

ALLOWED = {"/": "ui.html", "/ui.html": "ui.html",
           "/session.jsonl": "session.jsonl", "/tests.jsonl": "tests.jsonl",
           "/notes.jsonl": "notes.jsonl"}


class Guarded(http.server.SimpleHTTPRequestHandler):
    """Serves only the dashboard and its logs. Everything else is 404 -- the
    directory also holds .env, and this server listens on the network."""

    def translate_path(self, path):
        name = ALLOWED.get(path.split("?")[0])
        return str(HERE / name) if name else str(HERE / "__denied__")

    def send_head(self):
        if path_of(self.path) not in ALLOWED:
            self.send_error(404, "Not found")
            return None
        return super().send_head()

    def log_message(self, *a):
        pass


def path_of(p: str) -> str:
    return p.split("?")[0]


def serve_page():
    class Quiet(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
        def handle_error(self, *a): pass
    with Quiet(("0.0.0.0", UI_PORT), Guarded) as httpd:
        httpd.serve_forever()

async def main():
    load_key()
    threading.Thread(target=serve_page, daemon=True).start()
    try:
        dev_in, dev_out = pick_device()
        mac_in = sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                                blocksize=1200, callback=on_mac_mic,
                                device=dev_in)
        mac_in.start()
        mac_ok = True
    except Exception as e:
        mac_in, mac_ok = None, False
        print(f"mac mic unavailable ({e}) - phone mic only", flush=True)

    with sd.OutputStream(samplerate=RATE, channels=1, dtype="int16",
                         blocksize=1200, callback=on_speaker,
                         device=dev_out):
        asyncio.create_task(telemetry())
        async with websockets.serve(router, "0.0.0.0", WS_PORT, max_size=None):
            print(f"relay  ws://0.0.0.0:{WS_PORT}", flush=True)
            print(f"UI     http://localhost:{UI_PORT}/ui.html", flush=True)
            print(f"echo   half-duplex gate on, {TAIL_S*1000:.0f} ms tail", flush=True)
            print(f"agent  '{NAME}' - profile '{PROFILE}', voice '{VOICE}'", flush=True)
            print(f"tools  {', '.join(t['name'] for t in TOOLS)}", flush=True)
            print(f"volume {PHONE_VOL:.0%}", flush=True)
            print(f"audio  output -> "
                  f"{'ESP32 speaker' if TO_PHONE else 'Mac speakers'}", flush=True)
            print("turn   detection on (vad 0.5, silence 900-3000 ms), "
                  "voice focus near-field", flush=True)
            print("notes  streaming notetaker in parallel "
                  "(universal-3-5-pro, speaker labels, max accuracy)", flush=True)
            print(f"mic    source '{runtime['mic']}'"
                  f"{'' if mac_ok else '  (mac mic unavailable)'}", flush=True)
            await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nrelay stopped.")
