#!/usr/bin/env python3
"""
The second half of the notetaker. Streaming gives you a live transcript during
the meeting; this runs afterwards on the saved wav and gives you the good one -
speaker-labelled, and summarised.

    python summarize.py ../notes/2026-08-30-141233.wav
    python summarize.py --keep-audio recording.wav      keep the wav
    python summarize.py --forget recording.wav          also delete it server-side

Rewrites the matching .md in place with speakers and a summary on top, then
deletes the wav - it is ~115 MB per hour and the notes are the point.

The audio is only ever deleted once the notes are on disk AND the transcript
came back with real words in it. A silent or failed result keeps the wav, because
that is precisely the case where you will want to run this again.
"""
import argparse, sys, time
from pathlib import Path

import requests

from notetaker import load_key, KEYTERMS   # same env/.env lookup, same vocabulary

BASE = "https://api.assemblyai.com/v2"

CONFIG = {
    # PLURAL. Singular "speech_model" is deprecated here and 400s; this is a
    # fallback chain, best first. Omit it and you silently get an older model -
    # the post-call transcript then reads WORSE than the live one.
    "speech_models":   ["universal-3-5-pro", "universal-2"],
    "keyterms_prompt": KEYTERMS,             # same vocabulary as the live pass
    "speaker_labels":  True,     # who said what
    "punctuate":       True,
    "format_text":     True,
    "summarization":   True,
    "summary_model":   "conversational",
    "summary_type":    "bullets",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--keep-audio", action="store_true",
                    help="keep the wav afterwards (default is to delete it)")
    ap.add_argument("--forget", action="store_true",
                    help="also delete the transcript from AssemblyAI once notes are saved")
    args = ap.parse_args()

    wav = Path(args.wav).expanduser().resolve()
    if not wav.exists():
        sys.exit(f"no such file: {wav}")

    key = load_key()
    h = {"authorization": key}          # note: no "Bearer" prefix

    size_mb = wav.stat().st_size / 1e6
    print(f"uploading {wav.name} ({size_mb:.1f} MB) ...")
    up = requests.post(f"{BASE}/upload", headers=h, data=wav.read_bytes())
    up.raise_for_status()
    audio_url = up.json()["upload_url"]

    job = requests.post(f"{BASE}/transcript", headers=h,
                        json={"audio_url": audio_url, **CONFIG})
    if job.status_code != 200:
        sys.exit(f"transcript request rejected ({job.status_code}): "
                 f"{job.json().get('error', job.text)}")
    tid = job.json()["id"]
    print(f"transcript {tid} queued ...")

    while True:
        r = requests.get(f"{BASE}/transcript/{tid}", headers=h)
        r.raise_for_status()
        data = r.json()
        if data["status"] == "completed":
            break
        if data["status"] == "error":
            sys.exit(f"failed: {data.get('error')}")
        time.sleep(3)

    lines = [f"# Meeting notes - {wav.stem}", ""]

    summary = data.get("summary")
    if summary:
        lines += ["## Summary", "", summary.strip(), ""]

    lines += ["## Transcript", ""]
    utterances = data.get("utterances") or []
    if utterances:
        for u in utterances:
            mins, secs = divmod(int(u["start"] / 1000), 60)
            lines.append(f"**Speaker {u['speaker']}** ({mins:02d}:{secs:02d}) "
                         f"{u['text']}")
            lines.append("")
    else:
        lines += [data.get("text", "_(empty)_"), ""]

    md = wav.with_suffix(".md")
    md.write_text("\n".join(lines))
    print(f"\n  notes -> {md}")

    # ---- cleanup, in the only safe order ----
    spoken = (data.get("text") or "").strip()
    notes_ok = md.exists() and md.stat().st_size > 64

    if not spoken:
        print("  !  transcript came back empty - keeping the wav so you can retry")
        return
    if not notes_ok:
        print("  !  notes file looks wrong - keeping the wav")
        return

    if args.forget:
        d = requests.delete(f"{BASE}/transcript/{tid}", headers=h)
        print(f"  forgotten server-side ({d.status_code})" if d.status_code == 200
              else f"  !  could not delete remotely: {d.status_code}")

    if args.keep_audio:
        print(f"  audio kept -> {wav.name}")
    else:
        wav.unlink()
        print(f"  audio deleted ({size_mb:.1f} MB freed)")


if __name__ == "__main__":
    main()
