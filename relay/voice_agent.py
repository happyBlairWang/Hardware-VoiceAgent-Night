#!/usr/bin/env python3
"""
Phase 1: talk to the AssemblyAI Voice Agent from the Mac. No ESP32 involved.

Pair your Nothing headphones and select them in Sound settings first, or just
use the built-in mic and speakers.

    python voice_agent.py          Ctrl-C to hang up.

Protocol notes (verified live against the API, not guessed):
  * session.update nests config under "session"; the server echoes it as "config"
  * outbound audio  -> {"type":"input.audio", "audio": <base64>}
  * inbound  audio  -> {"type":"reply.audio", "data":  <base64>}
"""
import asyncio, base64, json, os, queue, signal, sys
from pathlib import Path

import numpy as np
import sounddevice as sd
import websockets

WS_URL = "wss://agents.assemblyai.com/v1/ws"
RATE   = 24_000     # audio/pcm is 24kHz, 16-bit LE, mono
BLOCK  = 1200       # 50 ms per chunk

# ---- the agent's personality. Edit, restart, redial. ----
SESSION = {
    "type": "session.update",
    "session": {
        "system_prompt": (
            "You are the operator of a vintage telephone exchange, answering a "
            "rotary phone manufactured in 1955. You are warm, unhurried and a "
            "little formal. Keep replies to one or two sentences - this is a "
            "phone call, not an essay."
        ),
        "greeting": "Operator speaking. How may I connect your call?",
        "input":  {"format": {"encoding": "audio/pcm", "sample_rate": RATE}},
        "output": {"voice": "anna",
                   "format": {"encoding": "audio/pcm", "sample_rate": RATE}},
    },
}


def load_key() -> str:
    key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    env = Path(__file__).parent / ".env"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("ASSEMBLYAI_API_KEY="):
                key = line.split("=", 1)[1].strip()
    if not key:
        sys.exit("No API key. Put it in relay/.env as ASSEMBLYAI_API_KEY=...")
    return key


async def main() -> None:
    key = load_key()
    mic_q: queue.Queue[bytes] = queue.Queue()
    spk_q: queue.Queue[bytes] = queue.Queue()
    residual = bytearray()

    def on_mic(indata, frames, time_info, status):
        mic_q.put(bytes(indata))

    def on_spk(outdata, frames, time_info, status):
        want = frames * 2
        while len(residual) < want:
            try:
                residual.extend(spk_q.get_nowait())
            except queue.Empty:
                residual.extend(b"\x00" * (want - len(residual)))
                break
        outdata[:] = np.frombuffer(bytes(residual[:want]),
                                   dtype=np.int16).reshape(-1, 1)
        del residual[:want]

    print(f"dialling {WS_URL} ...")
    async with websockets.connect(
        WS_URL, additional_headers={"Authorization": f"Bearer {key}"},
        max_size=None,
    ) as ws:
        await ws.send(json.dumps(SESSION))
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        loop.add_signal_handler(signal.SIGINT, stop.set)
        line: list[str] = []

        async def pump_mic():
            while not stop.is_set():
                chunk = await loop.run_in_executor(None, mic_q.get)
                await ws.send(json.dumps({
                    "type": "input.audio",
                    "audio": base64.b64encode(chunk).decode(),
                }))

        async def pump_ws():
            async for raw in ws:
                m = json.loads(raw)
                t = m.get("type", "?")

                if t == "reply.audio":
                    spk_q.put(base64.b64decode(m["data"]))
                elif t == "transcript.agent.delta":
                    line.append(m.get("delta", ""))
                elif t in ("transcript.agent", "reply.done"):
                    if line:
                        print(f"  agent: {' '.join(line)}")
                        line.clear()
                elif t.startswith("transcript.user"):
                    txt = m.get("text") or m.get("delta") or ""
                    if txt:
                        print(f"    you: {txt}")
                elif t == "session.ready":
                    print("  connected - start talking\n")
                elif t == "reply.started":
                    while not spk_q.empty():      # barge-in: drop stale audio
                        spk_q.get_nowait()
                    residual.clear()
                elif t == "session.error":
                    print(f"  !! {m.get('code')}: {m.get('message')}")
                elif t not in ("session.updated",):
                    print(f"  <{t}>")
                if stop.is_set():
                    break

        with sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                            blocksize=BLOCK, callback=on_mic), \
             sd.OutputStream(samplerate=RATE, channels=1, dtype="int16",
                             blocksize=BLOCK, callback=on_spk):
            tasks = [asyncio.create_task(pump_mic()),
                     asyncio.create_task(pump_ws()),
                     asyncio.create_task(stop.wait())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t_ in tasks:
                t_.cancel()

    print("\nhung up.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
