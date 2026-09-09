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
import asyncio, base64, functools, http.server, json, queue, socketserver, sys, threading, time
from pathlib import Path

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
PHONE_VOL  = 0.5        # speaker level, 0.0-1.0 (change and restart the relay)
HERE      = Path(__file__).parent
LOG       = HERE / "session.jsonl"
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
        "input":  {"format": {"encoding": "audio/pcm", "sample_rate": RATE}},
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
runtime = {"vol": PHONE_VOL}
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
    """Push raw 8-bit samples at the speaker, paced so the device ring buffer
    never overruns."""
    if esp_ws is None:
        await to_ui({"type": "note", "text": "no phone connected"})
        return False
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


async def telemetry():
    while True:
        await asyncio.sleep(0.3)
        if ui_clients:
            await to_ui({"type": "stats", **stats, **state,
                         "vol": runtime["vol"], "profile": PROFILE, "voice": VOICE})

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
    global esp_ws
    esp_ws = esp
    state["esp"] = True
    await push_status()
    print(f"\n=== phone connected from {esp.remote_address[0]} ===", flush=True)
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

            async def esp_to_agent():
                nonlocal sent, dropped
                await ready.wait()
                n = 0
                was_muted = None
                async for frame in esp:
                    if not isinstance(frame, bytes) or not frame:
                        continue

                    gated = time.monotonic() < speak_until + TAIL_S
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

                    sent += len(frame)
                    stats["sent_s"] = sent / 2 / RATE
                    await agent.send(json.dumps({
                        "type": "input.audio",
                        "audio": base64.b64encode(frame).decode(),
                    }))
                    n += 1
                    if n % 5 == 0:
                        await to_ui({"type": "level", "rms": stats["rms"]})

            async def agent_to_out():
                nonlocal speak_until
                async for raw in agent:
                    m = json.loads(raw)
                    t = m.get("type", "")

                    if t == "reply.audio":
                        pcm = base64.b64decode(m["data"])
                        if TO_PHONE:
                            # 24 kHz 16-bit  ->  8 kHz 8-bit unsigned.
                            # Averaging each group of three avoids the aliasing
                            # that plain decimation would fold into the voice.
                            a = np.frombuffer(pcm, dtype=np.int16)
                            k = (len(a) // 3) * 3
                            if k:
                                d = a[:k].reshape(-1, 3).mean(axis=1)
                                u8 = (d / 256 * runtime["vol"] + 128).clip(0, 255).astype(np.uint8)
                                await esp.send(u8.tobytes())
                        else:
                            spk_q.put(pcm)
                        # extend the quiet window by this chunk's real duration
                        dur = len(pcm) / 2 / RATE
                        speak_until = max(speak_until, time.monotonic()) + dur
                    elif t == "transcript.agent.delta":
                        line.append(m.get("delta", ""))
                    elif t in ("transcript.agent", "reply.done"):
                        if line:
                            said = " ".join(line); line.clear()
                            print(f"  operator: {said}", flush=True)
                            record("operator", said)
                            stats["turns"] += 1
                            await to_ui({"type": "agent", "text": said})
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
                        if TO_PHONE:
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

            try:
                await asyncio.gather(esp_to_agent(), agent_to_out())
            except websockets.ConnectionClosed:
                pass
            finally:
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
            elif c == "volume":
                runtime["vol"] = max(0.0, min(1.0, float(m.get("value", 0.5))))
            elif c == "clear":
                await to_ui({"type": "clear"})
    finally:
        ui_clients.discard(ws)


async def router(ws):
    if ws.request.path.rstrip("/").endswith("ui"):
        await handle_ui(ws)
    else:
        await handle_phone(ws)

ALLOWED = {"/": "ui.html", "/ui.html": "ui.html",
           "/session.jsonl": "session.jsonl", "/tests.jsonl": "tests.jsonl"}


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
    with sd.OutputStream(samplerate=RATE, channels=1, dtype="int16",
                         blocksize=1200, callback=on_speaker):
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
            await asyncio.Future()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nrelay stopped.")
