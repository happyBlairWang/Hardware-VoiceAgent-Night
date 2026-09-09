#!/usr/bin/env python3
"""Render relay/session.jsonl into a standalone transcript page.

    ~/esp-tools/bin/python publish_transcript.py [out.html]

Turns where "you" exactly echoes the operator's previous line are marked --
that is the microphone picking up the Mac's speakers, not you talking.
"""
import html, json, sys, time
from pathlib import Path
from difflib import SequenceMatcher

HERE = Path(__file__).parent
LOG  = HERE / "session.jsonl"
OUT  = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "transcript.html"

rows = [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()] if LOG.exists() else []

# mark probable speaker bleed
prev_op = ""
for r in rows:
    r["echo"] = False
    if r["who"] == "you" and prev_op:
        if SequenceMatcher(None, r["text"].lower(), prev_op.lower()).ratio() > 0.62:
            r["echo"] = True
    if r["who"] == "operator":
        prev_op = r["text"]

real  = [r for r in rows if not r["echo"]]
echoes = len(rows) - len(real)
span  = (rows[-1]["t"] - rows[0]["t"]) / 60 if len(rows) > 1 else 0
when  = time.strftime("%b %-d, %Y at %-I:%M %p", time.localtime(rows[-1]["t"])) if rows else "-"

turns = []
for r in rows:
    who = "You" if r["who"] == "you" else "Operator"
    cls = ("you" if r["who"] == "you" else "op") + (" echo" if r["echo"] else "")
    tag = '<span class="flag">speaker bleed</span>' if r["echo"] else ""
    turns.append(
        f'<div class="turn {cls}"><div class="who">{who}{tag}</div>'
        f'<div class="said">{html.escape(r["text"])}</div></div>')

STYLE = """
:root{--paper:#F1EEE5;--card:#FBFAF6;--sunk:#E5E1D5;--ink:#18232D;--ink-soft:#4A5A67;
--ink-faint:#8494A0;--rule:#CFCABA;--brass:#A0742A;--signal:#A8352A;--blu:#2F5D7C}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--paper:#10161B;
--card:#19222A;--sunk:#131B21;--ink:#E6E9EC;--ink-soft:#A3B1BC;--ink-faint:#6C7C88;
--rule:#2C3945;--brass:#D6A353;--signal:#E4796A;--blu:#79ADD1}}
:root[data-theme="dark"]{--paper:#10161B;--card:#19222A;--sunk:#131B21;--ink:#E6E9EC;
--ink-soft:#A3B1BC;--ink-faint:#6C7C88;--rule:#2C3945;--brass:#D6A353;--signal:#E4796A;--blu:#79ADD1}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font-family:"Libre Franklin",-apple-system,BlinkMacSystemFont,sans-serif;font-size:17px;line-height:1.6}
.wrap{max-width:760px;margin:0 auto;padding:0 24px 80px}
header{padding:64px 0 30px;border-bottom:3px solid var(--ink)}
.eyebrow{font-family:"IBM Plex Mono",monospace;font-size:12px;letter-spacing:.16em;
text-transform:uppercase;color:var(--brass);margin:0 0 18px}
h1{font-family:"Oswald",sans-serif;font-weight:600;text-transform:uppercase;
font-size:clamp(34px,7vw,58px);line-height:1;margin:0;letter-spacing:.005em}
.stats{display:flex;flex-wrap:wrap;gap:26px;margin-top:26px}
.stat b{display:block;font-family:"Oswald",sans-serif;font-size:27px;font-weight:500;
color:var(--brass);font-variant-numeric:tabular-nums;line-height:1.1}
.stat span{font-family:"IBM Plex Mono",monospace;font-size:11px;letter-spacing:.09em;
text-transform:uppercase;color:var(--ink-faint)}
.feed{display:flex;flex-direction:column;gap:15px;padding-top:34px}
.turn{display:flex;flex-direction:column;gap:5px}
.who{font-family:"Oswald",sans-serif;text-transform:uppercase;letter-spacing:.09em;
font-size:11.5px;color:var(--ink-faint);display:flex;align-items:center;gap:9px}
.said{font-size:18px;line-height:1.5;padding:14px 18px;border:1px solid var(--rule);
background:var(--card);text-wrap:pretty}
.turn.you .who{color:var(--blu)} .turn.you .said{border-left:4px solid var(--blu)}
.turn.op  .who{color:var(--brass)} .turn.op .said{border-left:4px solid var(--brass);background:var(--sunk)}
.turn.echo{opacity:.5} .turn.echo .said{border-style:dashed;font-style:italic}
.flag{font-family:"IBM Plex Mono",monospace;font-size:9.5px;letter-spacing:.08em;
color:var(--signal);border:1px solid var(--signal);padding:2px 6px;text-transform:uppercase}
.note{background:var(--card);border:1px solid var(--rule);border-left:5px solid var(--signal);
padding:20px 22px;margin:34px 0 0}
.note h3{font-family:"Oswald",sans-serif;text-transform:uppercase;letter-spacing:.05em;
font-size:15px;margin:0 0 8px;color:var(--signal)}
.note p{margin:0;font-size:15.5px;color:var(--ink-soft)}
footer{margin-top:44px;padding-top:22px;border-top:1px solid var(--rule);
font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--ink-faint)}
"""

note = ("" if not echoes else f"""
<div class="note"><h3>{echoes} turns were the microphone hearing the speakers</h3>
<p>The MAX9814 sits in the open air near the Mac, so it picks up the operator's own voice
and the agent transcribes itself. Those turns are dimmed above. Headphones on the Mac, or
muting the mic while the agent speaks, removes them entirely.</p></div>""")

OUT.write_text(f"""<title>Line 1 Transcript</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Oswald:wght@400;500;600&family=Libre+Franklin:wght@400;600&family=IBM+Plex+Mono:wght@400;600&display=swap">
<style>{STYLE}</style>
<div class="wrap">
<header>
  <p class="eyebrow">MAX9814 → ESP32 → Wi-Fi → AssemblyAI</p>
  <h1>Line 1 transcript</h1>
  <div class="stats">
    <div class="stat"><b>{len(rows)}</b><span>turns</span></div>
    <div class="stat"><b>{len(real)}</b><span>real</span></div>
    <div class="stat"><b>{echoes}</b><span>speaker bleed</span></div>
    <div class="stat"><b>{span:.0f}m</b><span>span</span></div>
  </div>
</header>
{note}
<div class="feed">{''.join(turns) if turns else '<p>No turns recorded yet.</p>'}</div>
<footer>Captured {when} · every word travelled from a breadboard microphone over a phone
hotspot and back. Regenerate with <code>publish_transcript.py</code>.</footer>
</div>""")
print(f"{OUT}  ({len(rows)} turns, {echoes} flagged as echo)")
