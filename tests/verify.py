#!/usr/bin/env python3
"""
End-to-end verification for the whole stack. Start the relay first (`./phone`), then:

    ~/esp-tools/bin/python tests/verify.py           # everything (~3 min)
    ~/esp-tools/bin/python tests/verify.py --quick   # skip firmware builds

It pretends to be the ESP32 -- streaming synthesized voices into the relay exactly
as the phone does -- so the real code paths run against the live AssemblyAI APIs.
Results print here and are added to the dashboard's Test log tab.

Your notes, call history and Furby's memory are snapshotted first and restored afterwards, so the
test calls leave no trace in them. Secrets are read at runtime from the gitignored
files, never written into this script.
"""
import asyncio, json, re, subprocess, sys, tempfile, time, wave
import urllib.error, urllib.request
from pathlib import Path

import websockets

ROOT  = Path(__file__).resolve().parent.parent
RELAY = ROOT / "relay"
WS    = "ws://localhost:8080"
HTTP  = "http://localhost:8081"
RATE  = 24000
QUICK = "--quick" in sys.argv

results: list[tuple[str, bool, str]] = []

def check(name: str, ok: bool, detail: str = ""):
    results.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name:<42} {detail}", flush=True)

def section(title: str):
    print(f"\n{title}", flush=True)


# ------------------------------------------------------------------ web server
def http_status(path: str) -> int:
    try:
        with urllib.request.urlopen(HTTP + path, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0

def test_web():
    section("Web server")
    check("dashboard is served", http_status("/") == 200, f"HTTP {http_status('/')}")
    check("notes log is served (allowlisted)", http_status("/notes.jsonl") == 200)
    # The server's directory holds .env, and it listens on the network.
    probes = ["/.env", "/relay.py", "/../relay/.env", "/%2e%2e/relay/.env",
              "/ui.html/../.env", "/../src/secrets.h", "/.env%00.html"]
    leaked = [p for p in probes if http_status(p) == 200]
    check("secrets refused, incl. path tricks", not leaked,
          f"{len(probes)} probes, " + (f"LEAKED {leaked}" if leaked else "all refused"))


# ------------------------------------------------------------------ repo hygiene
def git(*args) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args],
                          capture_output=True, text=True).stdout

def read_secret(path: Path, pattern: str) -> str | None:
    if not path.exists():
        return None
    m = re.search(pattern, path.read_text())
    return m.group(1) if m else None

def test_repo():
    section("Repository")
    tracked = set(git("ls-files").split())
    for f in ("relay/.env", "src/secrets.h", "relay/notes.jsonl", "relay/session.jsonl"):
        check(f"{f} is not tracked", f not in tracked)

    # Search every commit ever made -- not just the current tree -- for the real
    # values, read from the gitignored files. Nothing secret is stored here.
    history = git("log", "-p", "--all")
    for label, value in (
        ("API key", read_secret(RELAY / ".env", r"ASSEMBLYAI_API_KEY=(\S+)")),
        ("WiFi password", read_secret(ROOT / "src/secrets.h", r'WIFI_PASS\s+"([^"]+)"')),
        ("WiFi network name", read_secret(ROOT / "src/secrets.h", r'WIFI_SSID\s+"([^"]+)"')),
    ):
        if not value:
            check(f"{label} absent from git history", True, "not configured - skipped")
        else:
            check(f"{label} absent from git history", value not in history,
                  "found in history!" if value in history else "none in any commit")


# ------------------------------------------------------------------ firmware
def test_firmware():
    section("Firmware")
    if QUICK:
        print("  skipped (--quick)")
        return
    pio = Path.home() / "esp-tools/bin/pio"
    for env in ("esp32dev", "xiao", "xiaospk", "xiaocam"):
        r = subprocess.run([str(pio), "run", "-e", env], cwd=ROOT,
                           capture_output=True, text=True)
        ok = r.returncode == 0
        err = next((l for l in r.stdout.splitlines() if "error" in l.lower()), "")
        check(f"builds: {env}", ok, "" if ok else err[:70])


# ------------------------------------------------------------------ voices
def make_clips(tmp: Path) -> dict:
    lines = {
        "milk":    ("Samantha", "Remind me to buy milk, and call Grandma on Sunday afternoon."),
        "dentist": ("Daniel",   "Also, the dentist appointment moved to Thursday at three."),
        "turnup":  ("Samantha", "Furby, please turn the volume up."),
        "quiet":   ("Samantha", "This part should only be written down. Please do not answer it."),
        "wake":    ("Daniel",   "Hey Furby, are you there?"),
    }
    clips = {}
    for name, (voice, text) in lines.items():
        aiff, wav = tmp / f"{name}.aiff", tmp / f"{name}.wav"
        subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1",
                        str(aiff), str(wav)], check=True)
        with wave.open(str(wav)) as w:
            clips[name] = w.readframes(w.getnframes())
    return clips


# ------------------------------------------------------------------ the call
class Dashboard:
    """Listens on the UI socket the way the web app does."""
    def __init__(self, ws):
        self.ws, self.stats, self.notes = ws, {}, []
    async def run(self):
        async for raw in self.ws:
            m = json.loads(raw)
            if m.get("type") == "stats":
                self.stats.update(m)
            elif m.get("type") == "note_turn" and m.get("src") == "mic":
                self.notes.append(m)
    async def send(self, **cmd):
        await self.ws.send(json.dumps(cmd))

class Phone:
    """Streams audio in like the ESP32, and counts reply audio coming back."""
    def __init__(self, ws):
        self.ws, self.reply, self.first, self.last = ws, 0, None, 0.0
    async def run(self):
        async for m in self.ws:
            if isinstance(m, bytes):
                now = time.monotonic()
                self.first = self.first or now
                self.reply += len(m); self.last = now
    async def speak(self, pcm: bytes):
        buf = pcm + b"\x00" * (RATE * 2 * 2)          # then 2 s of silence
        for i in range(0, len(buf), 640):             # 320-sample frames, like the phone
            await self.ws.send(buf[i:i + 640])
            await asyncio.sleep(320 / RATE)
    async def settle(self, dash=None) -> float:
        """Wait until the reply has finished playing; return its length in seconds.

        A reply can come in two parts with a pause between them, when Furby
        calls a tool part-way through ("I'll write that down" ... "done").
        Treating the first pause as the end made the next line start inside
        the second part, where the echo gate cut it. So after each burst has
        had time to play, require 3 s with no new audio before handing the
        floor back. (Not the relay's "muted" flag: on the phone path it only
        updates when a frame arrives, so once the phone goes quiet it can
        stay stuck and wait forever.) Every wait here is bounded."""
        await asyncio.sleep(1.0)
        spoken, deadline = 0.0, time.monotonic() + 40
        while time.monotonic() < deadline:
            t0 = time.monotonic()
            while time.monotonic() - t0 < 15:
                if self.first and time.monotonic() - self.last > 1.2:
                    break
                await asyncio.sleep(0.2)
            secs = self.reply / 2 / RATE
            if self.first:                            # it arrives faster than real time
                left = secs - (time.monotonic() - self.first)
                if left > 0:
                    await asyncio.sleep(left)
            spoken += secs
            self.reply, self.first = 0, None
            q0 = time.monotonic()
            while time.monotonic() - q0 < 3.0 and time.monotonic() < deadline:
                if self.first:                        # more of the reply arrived
                    break
                await asyncio.sleep(0.2)
            if not self.first:
                break
        await asyncio.sleep(0.8)
        return spoken

async def wait_for(cond, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.2)
    return False

async def test_call(clips: dict):
    section("Call: voice agent + notetaker + modes + tools")
    async with websockets.connect(f"{WS}/ui", max_size=None) as ui_ws:
        dash = Dashboard(ui_ws)
        dash_task = asyncio.create_task(dash.run())
        await wait_for(lambda: "vol" in dash.stats, 5)
        before = {"vol": dash.stats.get("vol", 0.5), "mode": dash.stats.get("mode", "talk")}
        await dash.send(cmd="mode", value="talk")

        async with websockets.connect(f"{WS}/", max_size=None) as phone_ws:
            phone = Phone(phone_ws)
            phone_task = asyncio.create_task(phone.run())
            await phone_ws.send("fmt:pcm16@24000")

            live = await wait_for(lambda: dash.stats.get("notes_live"), 15)
            check("notetaker connects", live)
            greet = await phone.settle(dash)
            check("Furby greets the caller", greet > 1.0, f"{greet:.1f}s of audio")

            # --- both streams, two voices
            await phone.speak(clips["milk"]);    r1 = await phone.settle(dash)
            await phone.speak(clips["dentist"]); r2 = await phone.settle(dash)
            # Wait for the content, not a count: one sentence can arrive as two
            # turns, which would satisfy "at least two notes" too early.
            find = lambda word: next((n for n in dash.notes if word in n["text"].lower()), None)
            await wait_for(lambda: find("milk") and find("dentist"), 12)
            milk, dent = find("milk"), find("dentist")
            if not (milk and dent):
                print("        notes captured:", [f"{n['speaker']}: {n['text']}" for n in dash.notes])
            check("Furby replies in talk mode", r1 > 0.3 or r2 > 0.3, f"{r1:.1f}s, {r2:.1f}s")
            check("notes capture voice 1", bool(milk), (milk or {}).get("text", "missing")[:52])
            check("notes capture voice 2", bool(dent), (dent or {}).get("text", "missing")[:52])
            if milk and dent:
                check("speakers told apart", milk["speaker"] != dent["speaker"],
                      f"{milk['speaker']} vs {dent['speaker']}")

            # --- the agent acts on a tool call
            await dash.send(cmd="volume", value=0.25)
            await wait_for(lambda: abs(dash.stats.get("vol", 0) - 0.25) < 0.01, 3)
            await phone.speak(clips["turnup"]); await phone.settle(dash)
            rose = await wait_for(lambda: dash.stats.get("vol", 0) > 0.26, 6)
            check("tool call: 'turn the volume up'", rose,
                  f"0.25 -> {dash.stats.get('vol', 0):.2f}")

            # --- notes only: Furby silent, notes still written
            await dash.send(cmd="mode", value="notes")
            await wait_for(lambda: dash.stats.get("mode") == "notes", 3)
            n0 = len(dash.notes)
            await phone.speak(clips["quiet"]); quiet = await phone.settle(dash)
            wrote = await wait_for(lambda: len(dash.notes) > n0, 8)
            check("notes-only mode keeps Furby silent", quiet < 0.3, f"{quiet:.1f}s of audio")
            check("notes-only mode still takes notes", wrote)

            # --- the notetaker hears the name and hands back to Furby
            await phone.speak(clips["wake"]); await phone.settle(dash)
            woke = await wait_for(lambda: dash.stats.get("mode") == "talk", 8)
            check("'Hey Furby' switches back to talk", woke,
                  "heard" if woke else "name not recognised - wake word is best-effort")
            phone_task.cancel()

        # --- after the phone hangs up, nothing may linger as a billed stream
        ended = await wait_for(lambda: not dash.stats.get("notes_live")
                               and not dash.stats.get("agent"), 12)
        check("phone hang-up ends the call", ended,
              "agent + notetaker closed" if ended else
              f"still open: agent={dash.stats.get('agent')} notes={dash.stats.get('notes_live')}")
        if ended:
            await asyncio.sleep(6)
            check("and it stays ended", not dash.stats.get("notes_live")
                  and not dash.stats.get("agent"), "no reconnect")

        # --- the dashboard's Hang up button ends a call too
        async with websockets.connect(f"{WS}/", max_size=None) as phone_ws:
            await phone_ws.send("fmt:pcm16@24000")
            up = await wait_for(lambda: dash.stats.get("agent"), 15)
            await dash.send(cmd="hangup")
            try:                                   # the relay should close the line on us
                await asyncio.wait_for(phone_ws.wait_closed(), 10)
                closed = True
            except asyncio.TimeoutError:
                closed = False
        ended = await wait_for(lambda: not dash.stats.get("agent")
                               and not dash.stats.get("notes_live"), 10)
        check("dashboard Hang up ends the call", up and closed and ended,
              "line closed, streams stopped" if (up and closed and ended) else
              f"call up={up} line closed={closed} streams stopped={ended}")

        await dash.send(cmd="volume", value=before["vol"])
        await dash.send(cmd="mode", value=before["mode"])
        await asyncio.sleep(0.5)
        dash_task.cancel()


# ------------------------------------------------------------------ chat
async def test_chat():
    section("Chat with your notes (LLM Gateway)")
    async with websockets.connect(f"{WS}/ui", max_size=None) as ws:
        async def reply_of(*types):
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 90))
                if m.get("type") in types:
                    return m
        await ws.send(json.dumps({"cmd": "models"}))
        m = await reply_of("models")
        check("model list loads live", len(m["models"]) >= 10 and m["default"] in m["models"],
              f"{len(m['models'])} models, default {m['default']}")

        async def ask(q, model=None):
            cid = f"verify-{time.time()}"
            await ws.send(json.dumps({"cmd": "chat", "id": cid, "question": q,
                                      "model": model or m["default"]}))
            text = ""
            while True:
                r = await reply_of("chat_delta", "chat_done", "chat_error")
                if r.get("id") != cid:
                    continue
                if r["type"] == "chat_delta":
                    text += r["text"]
                else:
                    return r["type"], text, r.get("error")

        kind, text, _ = await ask("When is the dentist appointment?")
        check("answers from the notes", kind == "chat_done" and "thursday" in text.lower(),
              " ".join(text.split())[:60])
        check("cites when it was said", bool(re.search(r"\[[A-Z][a-z]{2} \d{1,2} \d{2}:\d{2}\]", text)))

        kind, text, _ = await ask("What did I say about my submarine?")
        declined = any(w in text.lower() for w in
                       ("don't", "doesn't", "not ", "no information", "no mention", "can't", "isn't"))
        check("declines what the notes don't cover", kind == "chat_done" and declined,
              " ".join(text.split())[:60])

        kind, _, err = await ask("hello", model="not-a-real-model")
        check("bad model fails cleanly", kind == "chat_error" and "not supported" in (err or ""),
              (err or "")[:60])


# ------------------------------------------------------------------ main
def snapshot(paths):
    return {p: p.read_bytes() if p.exists() else None for p in paths}

def restore(snap):
    """Put logs back, but only if they merely grew -- never clobber real changes.
    Says so when it can't, rather than quietly leaving test data behind."""
    for p, before in snap.items():
        now = p.read_bytes() if p.exists() else None
        if before is None:
            if now is not None:          # the test run created it
                p.unlink()
            continue
        if now == before:
            continue
        if now is not None and now.startswith(before):
            p.write_bytes(before)
        else:
            print(f"  !! {p.name} changed in a way that isn't a plain append; "
                  f"left as is -- check it for test entries")

async def main():
    print("Verifying the Hardware Voice Agent stack\n" + "=" * 42)
    if http_status("/") != 200:
        print("\nThe relay isn't running. Start it with ./phone, then re-run this.")
        sys.exit(2)

    test_web()
    test_repo()
    test_firmware()

    # memory.md too: test calls say things like "remind me to buy milk", and
    # Furby's remember tool writes those into the real memory file.
    logs = snapshot([RELAY / "notes.jsonl", RELAY / "session.jsonl", RELAY / "memory.md"])
    try:
        with tempfile.TemporaryDirectory() as tmp:
            clips = make_clips(Path(tmp))
            await test_call(clips)
            await test_chat()
    finally:
        restore(logs)
        print("\n  (test calls removed from your notes, call history and memory)")

    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} passed")
    with (RELAY / "tests.jsonl").open("a") as f:
        for name, ok, detail in results:
            f.write(json.dumps({"t": time.time(), "test": f"verify: {name}",
                                "ok": ok, "detail": detail}) + "\n")
    print("Results added to the dashboard's Test log tab.")
    sys.exit(0 if passed == len(results) else 1)

if __name__ == "__main__":
    asyncio.run(main())
