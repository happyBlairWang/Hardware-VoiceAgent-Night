#!/usr/bin/env python3
"""
Granola-style meeting notetaker: capture audio locally on the Mac, stream it to
AssemblyAI, write markdown notes. No bot joins your call.

    python notetaker.py --list-devices          # find your input
    python notetaker.py --device 2              # press Enter to start/stop
    python notetaker.py --device 2 --serial /dev/cu.usbmodem1101

With --serial, the XIAO's button starts and stops the capture and its LED shows
whether this script is really recording.

Streaming settings come from AssemblyAI's meeting-notetaker guidance: longer
turn silences than a voice agent, because people in meetings pause mid-thought.
"""
import argparse, asyncio, json, os, queue, sys, threading, wave
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import sounddevice as sd
import websockets

RATE       = 16_000
CHUNK      = 1_600          # frames = 100 ms; 3200 bytes on the wire
NOTES_DIR  = Path(__file__).resolve().parent.parent / "notes"

WS_BASE = "wss://streaming.assemblyai.com/v3/ws"
WS_PARAMS = {
    "sample_rate":      RATE,
    "encoding":         "pcm_s16le",
    "speech_model":     "universal-3-5-pro",
    "mode":             "max_accuracy",  # notes aren't a voice agent - buy accuracy
    "format_turns":     "true",   # punctuation + casing. Non-negotiable for notes.
    "min_turn_silence": 560,      # meetings, not voice agents
    "max_turn_silence": 2000,
    "speaker_labels":   "true",
}

# Names the model would otherwise mangle. Cheap accuracy win, edit freely.
KEYTERMS = ["AssemblyAI", "Seeed XIAO", "Seeed", "XIAO", "SAMD21",
            "Granola", "notetaker", "Harnoor"]


def load_key() -> str:
    """Same lookup the vintage-phone relay uses: env first, then ../relay/.env."""
    key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    if key:
        return key
    for env in (Path(__file__).parent / ".env",
                Path(__file__).resolve().parent.parent.parent / "relay" / ".env"):
        if env.exists():
            for line in env.read_text().splitlines():
                if line.strip().startswith("ASSEMBLYAI_API_KEY="):
                    return line.split("=", 1)[1].strip()
    sys.exit("No API key. Set ASSEMBLYAI_API_KEY or put it in relay/.env")


# --------------------------------------------------------------------------
# XIAO link. Optional - absent hardware, Enter on stdin does the same job.
# --------------------------------------------------------------------------
class Board:
    def __init__(self, port: str | None, loop):
        self.ser, self.loop = None, loop
        self.presses: asyncio.Queue[str] = asyncio.Queue()
        if not port:
            return
        try:
            import serial                                    # pyserial
        except ImportError:
            sys.exit("--serial needs pyserial:  uv pip install pyserial")
        self.ser = serial.Serial(port, 115200, timeout=0.2)
        threading.Thread(target=self._read, daemon=True).start()
        print(f"  XIAO on {port}")

    def _read(self):
        while True:
            try:
                line = self.ser.readline().decode(errors="ignore").strip()
            except Exception:
                return
            if line in ("REC", "STOP"):
                self.loop.call_soon_threadsafe(self.presses.put_nowait, line)
            elif line:
                print(f"  [xiao] {line}")

    def tell(self, msg: str):
        if self.ser:
            try:
                self.ser.write((msg + "\n").encode())
            except Exception:
                pass


_stdin_eof = False


async def stdin_line(loop):
    """A line from stdin, or None at EOF. Parks forever once EOF is seen so it
    can never hand a caller an instant result over and over."""
    global _stdin_eof
    if _stdin_eof:
        await asyncio.Event().wait()
    line = await loop.run_in_executor(None, sys.stdin.readline)
    if line == "":
        _stdin_eof = True
        return None
    return line


async def wait_for_start(board: Board) -> bool:
    """Whichever comes first: the XIAO button, or Enter on stdin.
    Returns False when no trigger can ever fire again - the caller should quit.

    stdin at EOF (Ctrl-D, or piped input running out) returns instantly and
    forever. Without the EOF guard this loop spawns a new session every few
    milliseconds and carpets notes/ with empty wavs.
    """
    loop = asyncio.get_running_loop()
    while True:
        if _stdin_eof and not board.ser:
            return False
        # ensure_future, NOT create_task: run_in_executor returns a Future, and
        # create_task rejects anything that isn't a coroutine (fatal on 3.14).
        stdin = asyncio.ensure_future(stdin_line(loop))
        btn   = asyncio.ensure_future(board.presses.get())
        done, pending = await asyncio.wait({stdin, btn},
                                           return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        if btn in done:
            return True
        if stdin.result() is not None:
            return True
        if not board.ser:
            return False          # EOF and no button: nothing left to wait on


# --------------------------------------------------------------------------
# one capture session = one websocket + one wav + one markdown file
# --------------------------------------------------------------------------
async def session(key: str, device, board: Board):
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    wav_path = NOTES_DIR / f"{stamp}.wav"
    md_path  = NOTES_DIR / f"{stamp}.md"

    audio_q: queue.Queue[bytes | None] = queue.Queue()
    turns: list[str] = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    wav = wave.open(str(wav_path), "wb")
    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(RATE)

    def on_audio(indata, frames, time_info, status):
        if status:
            print(f"  ! audio {status}", file=sys.stderr)
        buf = bytes(indata)
        wav.writeframes(buf)          # keep the full recording for the post-call pass
        audio_q.put(buf)

    # keyterms_prompt is ONE param holding a JSON array - repeating the param
    # gets you a 3006 "Invalid JSON array" and the socket closed on you.
    params = {**WS_PARAMS, "keyterms_prompt": json.dumps(KEYTERMS)}
    url = f"{WS_BASE}?{urlencode(params)}"
    async with websockets.connect(
        url, additional_headers={"Authorization": key}, max_size=None
    ) as ws:

        async def pump_audio():
            while not stop.is_set():
                buf = await loop.run_in_executor(None, audio_q.get)
                if buf is None:
                    break
                await ws.send(buf)                       # binary frame, raw PCM
            await ws.send(json.dumps({"type": "Terminate"}))

        async def pump_ws():
            partial = ""
            async for raw in ws:
                m = json.loads(raw)
                t = m.get("type")
                if t == "Begin":
                    print(f"  session {m.get('id')} - recording, "
                          f"press again to stop\n")
                elif t == "Turn":
                    text = m.get("transcript", "")
                    if not text:
                        continue
                    who = m.get("speaker")
                    tag = f"Speaker {who}: " if who is not None else ""
                    if m.get("end_of_turn") and m.get("turn_is_formatted"):
                        print(" " * (len(partial) + 24), end="\r")
                        print(f"  {tag}{text}")
                        turns.append(f"**{tag.rstrip(': ')}** {text}" if tag else text)
                        partial = ""
                    else:
                        partial = text
                        print(f"  {tag}{text[-100:]}", end="\r", flush=True)
                elif t == "Termination":
                    print(f"\n  {m.get('audio_duration_seconds', 0)}s of audio")
                    break

        async def watch_stop():
            if board.ser:
                await board.presses.get()
            else:
                await stdin_line(loop)     # None at EOF - stop either way
            stop.set()
            audio_q.put(None)

        board.tell("REC")
        with sd.InputStream(samplerate=RATE, channels=1, dtype="int16",
                            blocksize=CHUNK, device=device, callback=on_audio):
            tasks = [asyncio.create_task(pump_audio()),
                     asyncio.create_task(pump_ws()),
                     asyncio.create_task(watch_stop())]
            await asyncio.wait(tasks[:2], return_when=asyncio.ALL_COMPLETED)
            for t_ in tasks:
                t_.cancel()

    board.tell("IDLE")
    wav.close()

    body = "\n\n".join(turns) if turns else "_(no speech detected)_"
    md_path.write_text(
        f"# Meeting notes - {stamp}\n\n"
        f"Audio: `{wav_path.name}`\n\n## Transcript\n\n{body}\n"
    )
    print(f"\n  notes  -> {md_path}")
    print(f"  audio  -> {wav_path}")
    print(f"  polish -> python summarize.py '{wav_path}'\n")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", help="input device index or name substring")
    ap.add_argument("--serial", help="XIAO port, e.g. /dev/cu.usbmodem1101")
    ap.add_argument("--list-devices", action="store_true")
    args = ap.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return

    device = args.device
    if device is not None and device.isdigit():
        device = int(device)

    key = load_key()
    board = Board(args.serial, asyncio.get_running_loop())
    board.tell("IDLE")

    trigger = "the XIAO button" if board.ser else "Enter"
    print(f"\nnotetaker ready. Press {trigger} to start a meeting. Ctrl-C to quit.")
    while await wait_for_start(board):
        await session(key, device, board)
        print(f"idle. Press {trigger} to start another.")
    print("input closed - bye.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print()
