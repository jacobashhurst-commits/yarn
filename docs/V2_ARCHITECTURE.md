# Yarn V2 architecture

Later version. The current app is Yarn in the Dock and the menu bar.

Yarn V2 evolves the existing local meeting archive. It does not replace
capture, transcription, diarisation, or the on-disk transcript as source of
truth.

## Stable foundation (do not regress)

These are production-tested. Several were found the hard way; see `CLAUDE.md`.

- Recording starts only because a human pressed something.
- Live capture is CoreAudio / PortAudio (`ma-capture`), never ffmpeg.
- Menu-bar recording runs as a launchd job (`ProcessType=Interactive`).
- Capture is native device rate (48 kHz); 16 kHz conversion happens after stop.
- Digital silence is refused at preflight and at finalize.
- Wall-clock vs audio duration is checked before publishing.
- Manifest is written last into `inbox/`.
- Transcript JSON/Markdown on disk is the durable record. Chroma is derived.
- Diarisation failure keeps the transcript. Transcription failure keeps audio.
- Speaker names are never applied without human confirmation.
- Aggressive VAD (`vad_min_silence_ms = 700`) stays unless quality tests say
  otherwise.
- Whisper default remains `large-v3-turbo`.

## Current flow (unchanged contract)

```
rec start  →  ma-capture (own process, high priority)
rec stop   →  ma finalize  (split channels, duration/silence checks)
           →  inbox/{id}.json  +  jobs/{id}.json (queued)
ma watch   →  claim inbox item
           →  transcribe → diarize → (optional recognise/cleanup)
           →  write transcript → summarize if Ollama is up → index
           →  archive audio
```

Recording is a different process from the watcher. Back-to-back meetings were
already possible at the OS level. V2 makes that a product guarantee: a
persistent job queue, stage resume, and capture-always-wins scheduling.

## Processing queue

Jobs live at `{archive}/jobs/{meeting_id}.json` and survive restart.

A meeting is **Recorded** as soon as finalize succeeds. The menu bar counts
work waiting as inbox (not yet transcribed) plus transcripts that still need
a summary. That is the number in `⏺ 4` and in "Process waiting now".

Process-when is per machine:

- **After each meeting** — watcher transcribes as soon as stop publishes.
- **Evening** — today's recordings sit in the inbox; leftover meetings from
  yesterday still catch up in the morning. Notes still pop the moment you stop.
- **Manual** — nothing heavy runs until you press Process waiting now.

`ma process` always runs the waiting work, ignoring the schedule.

Stages, in order:

| Stage | Required? | On failure |
|---|---|---|
| transcribe | yes | keep audio, job failed |
| diarize | no | keep transcript, honest labels |
| recognise | no (toggle) | keep Speaker N |
| cleanup | no | keep raw turns |
| summarize | no | keep transcript, deferred summary |
| index | yes for search | keep files, Chroma rebuildable |

Successful stages are not redone. A restart mid-transcribe has no transcript
yet, so transcription runs again. A restart after the JSON is on disk skips
Whisper.

`{archive}/.processing-control.json` can pause/resume the watcher. Recording
is unaffected.

## Recording always wins

Capture stays in `ma-capture` under interactive launchd. The watcher:

- never holds the microphone
- nices itself as a background job
- will not start a new heavy stage (transcribe/summarize) under memory
  pressure while a recording is active
- continues a stage already in flight unless pressure is critical
- resumes when the recording ends

## Audio retention

Audio is temporary source material. Transcripts are the archive.

Policy (`audio_retention`): `1_week` (default), `1_month`, `never`.

Eligible for automatic deletion only when:

1. a transcript JSON exists on disk, and
2. the meeting is not marked `keep_audio`, and
3. the file is older than the policy window, and
4. policy is not `never`

Failed/unprocessed audio is never pruned. Deferred summaries do not block
pruning. `ma audio keep` / `ma audio delete` are per-meeting overrides.

## Progress

`.processing-status.json` reports **stage + stage-level progress**.

A percentage is written only when there is a real signal (Whisper audio
position, chunk counts). Otherwise the stage name, elapsed time, and a
detail line are shown. Global fake 65%/82% values are gone.

## Imports

Imported recordings and transcripts enter the same job queue and archive
layout as native captures. ffmpeg is used only for offline conversion.

## Speaker identity

Diarisation (Sherpa, stable) distinguishes voices in one meeting.

Known-speaker memory is a separate, off-by-default pass. It may suggest
"Speaker 2 sounds like Adam (84%)". Confirmation still writes the name.

The identification dialog shows the meeting date, title and your notes first,
then asks about each voice. Each dialog shows **what they said** (a verbatim
excerpt) and **Play clip**. The clip is a short isolated stretch of that
voice, not a long overlapping slice. If you listed three people and name
two, the last remaining listed name is assumed for the last unnamed voice
and you are told.

Confirming a name writes a durable identification snippet to
`speakers/clips/{name}.wav` plus an averaged embedding in `library.json`.
Meeting audio can later be pruned; those files are not. `ma voices --html`
plays the kept snippets. Automatic suggestions compare the current meeting
against those embeddings and still wait for confirmation.


## Library

`ma library` lists meetings from transcript JSON + job state. SwiftBar stays
the control surface. A generated local HTML file is the lightweight viewer.

## Benchmarks

`ma benchmark` reads gitignored `benchmark_audio/`. Results go in
`docs/V2_PERFORMANCE.md`. Speed without a quality check is not a win.

## Implemented in this increment

- Persistent jobs under `{archive}/jobs/` with stage resume after crash
- Recording stays a separate process; watcher nices itself and will not
  *start* a heavy stage under memory pressure while a capture is live
- Honest stage progress in `.processing-status.json` (no fake 65/82)
- Audio retention: `1_week` / `1_month` / `never`, `keep_audio`, `ma delete-audio`
- `ma import-audio` / `ma import-transcript`, `ma library`, `ma queue`,
  `ma pause` / `ma resume`, `ma setting`
- SwiftBar V2 menu: recording and processing shown independently
- Known-speaker library + recognise toggle (off by default, suggestions only)
- Conservative cleanup stage (off by default); raw turns preserved when it runs
- Stronger default summary prompt with a profile hook
- Local performance JSONL and `ma benchmark` corpus listing

