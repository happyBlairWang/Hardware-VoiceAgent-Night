#!/usr/bin/env python3
"""
Run this 10 minutes before the demo, not 10 seconds before.

Checks every moving part and tells you which one will embarrass you:
board, mic, audio routing, API key, streaming session. Exits non-zero if
anything a live demo depends on is broken.

    ./.venv/bin/python preflight.py
"""
import asyncio, json, subprocess, sys
from pathlib import Path
from urllib.parse import urlencode

OK, BAD, WARN = "  [ok]  ", "  [FAIL]", "  [warn]"
fails = []


def check(name, ok, detail="", fatal=True):
    print(f"{OK if ok else (BAD if fatal else WARN)} {name}" + (f"  {detail}" if detail else ""))
    if not ok and fatal:
        fails.append(name)
    return ok


def main():
    print("\n--- preflight ---\n")

    # 1. board
    import serial
    from serial.tools import list_ports
    ports = [p.device for p in list_ports.comports() if "usbmodem" in p.device]
    if check("XIAO enumerated", bool(ports), ports[0] if ports else "no usbmodem port", fatal=False):
        try:
            s = serial.Serial(ports[0], 115200, timeout=2)
            s.reset_input_buffer(); s.write(b"PING\n")
            reply = ""
            for _ in range(6):
                reply = s.readline().decode(errors="ignore").strip()
                if reply:
                    break
            check("board answers PING", reply == "PONG", f"got {reply!r}", fatal=False)
            s.write(b"IDLE\n"); s.close()
        except Exception as e:
            check("board answers PING", False, str(e), fatal=False)

    # 2. audio in
    import numpy as np, sounddevice as sd
    devs = sd.query_devices()
    ins = [(i, d["name"]) for i, d in enumerate(devs) if d["max_input_channels"] > 0]
    print(f"{OK} input devices: " + ", ".join(f"{i}:{n}" for i, n in ins))
    loopback = [n for _, n in ins if any(k in n.lower() for k in ("blackhole", "aggregate", "loopback"))]
    check("system-audio route present", bool(loopback),
          loopback[0] if loopback else "mic only - you will NOT capture the far end",
          fatal=False)
    try:
        rec = sd.rec(int(1.5 * 16000), samplerate=16000, channels=1, dtype="int16")
        sd.wait()
        peak = int(np.abs(rec).max())
        check("mic captures audio", peak > 0, f"peak={peak}")
    except Exception as e:
        check("mic captures audio", False, str(e))

    # 3. api key + live streaming session
    from notetaker import load_key, WS_BASE, WS_PARAMS, KEYTERMS
    try:
        key = load_key()
        check("API key found", bool(key), f"...{key[-4:]}")
    except SystemExit as e:
        check("API key found", False, str(e)); key = None

    if key:
        async def probe():
            import websockets, math, struct
            params = {**WS_PARAMS, "keyterms_prompt": json.dumps(KEYTERMS)}
            url = f"{WS_BASE}?{urlencode(params)}"
            async with websockets.connect(url, additional_headers={"Authorization": key},
                                          max_size=None) as ws:
                for i in range(10):
                    await ws.send(b"".join(struct.pack("<h", int(900 * math.sin(
                        2 * math.pi * 200 * (i * 1600 + n) / 16000))) for n in range(1600)))
                    await asyncio.sleep(0.05)
                await ws.send(json.dumps({"type": "Terminate"}))
                async for raw in ws:
                    m = json.loads(raw)
                    if m["type"] == "Begin":
                        return m["configuration"]
                    if m["type"] == "Error":
                        raise RuntimeError(m.get("error"))
            return None
        try:
            cfg = asyncio.run(probe())
            check("streaming session opens", bool(cfg))
            check("model is universal-3-5-pro", cfg.get("model") == "universal-3-5-pro", cfg.get("model"))
            check("mode is max_accuracy", cfg.get("mode") == "max_accuracy", cfg.get("mode"))
            check("speaker labels on", cfg.get("speaker_labels") is True)
        except Exception as e:
            check("streaming session opens", False, f"{type(e).__name__}: {e}")

    # 4. disk
    notes = Path(__file__).resolve().parent.parent / "notes"
    check("notes/ writable", notes.is_dir(), str(notes))

    print()
    if fails:
        print(f"--- {len(fails)} BLOCKER(S): " + ", ".join(fails) + " ---\n")
        sys.exit(1)
    print("--- clear for demo ---\n")


if __name__ == "__main__":
    main()
