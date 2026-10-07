# AssemblyAI contracts, verified against the live APIs

Every row here was checked by sending real traffic, because the docs were wrong or
silent on several of them. Most of the "gotchas" below fail with **no error** —
the session just behaves wrongly — so read this before writing a client, not after.

Contents: Voice Agent · Streaming STT (the notetaker) · LLM Gateway · Auth at a glance

## Voice Agent — talk to it

| | |
|---|---|
| URL | `wss://agents.assemblyai.com/v1/ws` |
| Auth header | `Authorization: Bearer <key>` — **with** `Bearer` |
| First message | `{"type": "session.update", "session": {...}}` |
| Send audio | `{"type": "input.audio", "audio": "<base64 PCM>"}` |
| Receive audio | `{"type": "reply.audio", "data": "<base64 PCM>"}` |
| Audio format | 16-bit mono little-endian PCM at **24 kHz**, both ways |
| Lifecycle | `session.updated` → `session.ready` → `reply.started` → `reply.audio`… → `reply.done` |
| Transcripts | `transcript.user.*`, `transcript.agent.delta`, `transcript.agent` |
| Tool call in | `tool.call` with `call_id`, `name`, `arguments` |
| Tool result out | `{"type": "tool.result", "call_id": "...", "result": "<JSON string>"}` |
| Errors | `session.error` with `code` and `message` |

A working `session.update`:

```python
{"type": "session.update", "session": {
    "system_prompt": "...",            # + "\n\n" + memory block, rebuilt each call
    "greeting": "Hello there! Furby speaking.",
    "tools": TOOLS,
    "input":  {"format": {"encoding": "audio/pcm", "sample_rate": 24000},
               "voice_focus": "near-field"},           # a string, not a boolean
    "output": {"voice": "mary",
               "format": {"encoding": "audio/pcm", "sample_rate": 24000}}}}
```

Gotchas:

- **Config in, under `session`; echoed back under `config`.** Send it as `config`
  and `session.updated` comes back with `system_prompt: ""` — no error.
- **Inbound audio is `audio`, outbound is `data`.** Read `m["audio"]` off
  `reply.audio` and you get silence forever, while the session reports healthy.
- **Use 24 kHz.** Other rates were accepted at setup and failed later.
- **`tool.result` must be a JSON-encoded string**, not a nested object. Sending it
  immediately on `tool.call` works; there is no need to wait for `reply.done`.
- **A reply can come in two parts** when the agent calls a tool mid-way ("let me
  write that down" … pause … "done"). Anything that waits for "the reply finished"
  must not treat the first pause as the end.
- **The voice is fixed for the session.** Change it and reconnect. English voices:
  `alba` `eve` `george` `jane` `jean` `mary` `michael` (US), `anna` `charles` `paul`
  `vera` (UK). `anna` peaks at full scale — scale it down for an 8-bit DAC.
- **The model has no idea what day it is** unless the prompt says. Put today's date
  in front of the memory block.

A tool declaration — the description is what the model reads to decide *when* to
call it, so write it around what people actually say:

```python
{"type": "function", "name": "adjust_volume",
 "description": "Make the speaker louder or quieter by one step. Use for "
                "'turn it up', 'too loud', 'I can't hear you'.",
 "parameters": {"type": "object",
                "properties": {"direction": {"type": "string", "enum": ["up", "down"]}},
                "required": ["direction"]}}
```

## Streaming STT — the notetaker

| | |
|---|---|
| URL | `wss://streaming.assemblyai.com/v3/ws?<params>` |
| Auth header | `Authorization: <key>` — **no** `Bearer` |
| Send | raw binary PCM frames, **50–1000 ms each** |
| Finish | `{"type": "Terminate"}` so the last turn is finalised |
| Receive | `Begin` → `SpeechStarted` → `Turn`… → `Termination` |
| A finished turn | `Turn` with `end_of_turn` **and** `turn_is_formatted` true |
| Speaker | `speaker_label` (`"A"`, `"B"`…) and `speaker_confidence` |
| Sample rate | 24 kHz and 16 kHz both work |

Parameters that suit notes (accuracy over speed):

```python
{"sample_rate": 24000, "encoding": "pcm_s16le", "speech_model": "universal-3-5-pro",
 "format_turns": "true", "speaker_labels": "true", "mode": "max_accuracy",
 "min_turn_silence": 560, "max_turn_silence": 2000}
```

Gotchas:

- **`3007 Input Duration Violation`** — chunks under 50 ms. Devices often send ~13 ms
  frames; buffer to ~100 ms. The socket closes on the first bad chunk.
- **Speaker labels never appear** — the field is `speaker_label`; `speaker` is
  always `None`.
- **`keyterms_prompt` is one parameter holding a JSON array.** Repeating it per term
  gives `3006 Invalid JSON array` and closes the socket.
- **Partials look like duplicates.** Keep only turns with both `end_of_turn` and
  `turn_is_formatted`.
- **It ends a turn by hearing silence.** Stop sending audio altogether (an echo gate
  that drops frames) and the open turn may never close. Send zeros instead.
- **Formatted turns punctuate direct address** — "Furby, wake up" gets a comma, "Furby
  workshop starts at six" doesn't. Useful for a wake word.

## LLM Gateway — ask your notes

| | |
|---|---|
| URL | `POST https://llm-gateway.assemblyai.com/v1/chat/completions` |
| Auth header | raw key **or** `Bearer <key>` — both work |
| Body | OpenAI chat format: `model`, `messages`, `max_tokens` |
| Models | `GET /v1/models` — pick from this, don't hard-code |
| Streaming | `"stream": true` → `data: {...}` lines, ending `data: [DONE]` |

Gotchas:

- **The quickstart's example model doesn't exist** — `400 model ... is not
  supported`. Fill any model picker from `/v1/models`.
- **Answers are markdown, and the notes are untrusted text.** Escape before rendering
  in a page, then add back only the tags you support.
- **Asking sends the notes to whichever provider serves the model.** Say so to users.

## Auth at a glance

The three APIs disagree. These are the forms verified to work — use exactly these:

| API | Header |
|---|---|
| Voice Agent | `Authorization: Bearer <key>` |
| Streaming STT | `Authorization: <key>` |
| LLM Gateway | either |
