# CLAUDE.md

Context for Claude Code working in this repo.

## What this is

Manually triggered meeting recording, local Whisper transcription, and semantic
search over the archive from Claude via `chroma-mcp`. Fully local, no cloud, no
paid services. Jacob's machine is an Apple Silicon MacBook Pro.

Jacob does not write the code. Read the README before changing anything — most
of the non-obvious decisions are explained there, and several of them were
arrived at by finding the bug first.

## Non-negotiable constraints

These came from Jacob directly and must not be traded away for convenience:

1. **No automatic recording triggers, ever.** No calendar watching, no "meeting
   detected", no voice activation. Recording starts because a human pressed
   something. This is the whole premise.
2. **The on-stop popup never blocks.** Audio is safe on disk before any dialog
   appears. Dismissing it, or never seeing it, must change nothing about the
   recording, the transcript or the index.
3. **Nothing paid, nothing cloud.** Open source and self-hosted only.
4. **Transcripts on disk are the source of truth.** ChromaDB is a derived index
   that must always be rebuildable with `ma rebuild`. Never make Chroma the
   only home for anything.
5. **Never label speech with a speaker you can't actually identify.** See the
   room mode note below.

## Architecture in one pass

```
bin/rec              start/stop/toggle, writes a state file, calls `ma finalize`
bin/meeting-popup    AppleScript dialogs, calls `ma notes`
bin/speaker-popup    post-transcription voice naming with quips
ui/                  SwiftBar menu bar plugin template + LaunchAgent installer

meeting_archive/
  config.py          TOML config, all derived paths
  models.py          Segment / Turn / Chunk / NameCandidate, channel constants
  capture.py         the recorder: CoreAudio capture, installed as `ma-capture`
  audio.py           ffmpeg: device listing, probing, channel extraction
  transcribe.py      faster-whisper wrapper, one model per process
  diarize.py         local sherpa-onnx speaker spans for sys/room channels
  summarize.py       local Ollama summary; verifies model is already installed
  merge.py           interleave channels by timestamp, collapse into turns
  names.py           detect names from what was said (pattern matching)
  quips.py           extractive recognisable line per diarized voice
  chunking.py        turns -> overlapping chunks, + context header
  store.py           ChromaDB, metadata flattening
  records.py         manifest / transcript / notes file formats
  pipeline.py        finalize() on stop, process() in watcher, ingest()
  watcher.py         polling loop over inbox/ and notes/
  retention.py       prune archived audio past the window
  cli.py             the `ma` command
```

Flow: `rec stop` → `ma finalize` splits channels and writes a **manifest last**
into `inbox/` → watcher triggers on manifests only → transcribe, diarize
multi-person channels, merge, detect names, write transcript, index → optional
speaker naming popup → archive audio → prune after 7 days.

## Channel ids carry speaker knowledge

There is no "mode" flag threaded through the pipeline. The channel id is the
fact:

- `mic` — Jacob's microphone on a call. Known to be him.
- `sys` — system audio on a call. Known to be everyone else; diarization may
  split it into meeting-local `Speaker N` voices.
- `room` — an in-person meeting on one microphone. Several people, none
  identifiable by channel alone; diarization may split it into meeting-local
  `Speaker N` voices, otherwise it is labelled `Unknown`.

`room` exists because labelling a whole room as Jacob is confidently wrong in a
way nobody would notice for months. If you add a capture mode, add a channel id
and give it a label in `Config.label()`; do not reuse `mic` for audio that
isn't known to be one person.

`names._attribute()` depends on this: a self-introduction on `mic` is Jacob,
the same words on `room` are somebody else. There are tests for both.

## Traps found the hard way — don't undo these

- **ffmpeg needs `-f wav`** in `extract_mono`. The atomic-write temp suffix
  hides the `.wav` extension, so ffmpeg can't infer the container.
- **Chroma accepts list metadata and then silently won't filter on it.**
  `{"speakers": {"$eq": "Jacob"}}` matches nothing. `sanitize_metadata` is
  load-bearing. `None` raises outright.
- **Use Chroma's default embedding function on both sides.** `chroma-mcp` only
  supports default/cohere/openai/jina/voyageai/roboflow. Writing BGE or Nomic
  vectors here would make every search return confident nonsense with nothing
  appearing broken.
- **Reindex is delete-then-add, not upsert.** Notes arriving late can change
  the chunk count, and upsert would orphan the old chunks.
- **Aggressive VAD** (`vad_min_silence_ms = 700`). The far-side channel is
  mostly silence, and silence is where Whisper invents text.
- **A clustering threshold is not a head count.** Even at the high 0.9
  threshold, one 10m32s three-person recording fragmented into 24 clusters.
  The "Who spoke?" list now constrains that meeting only; blank remains
  automatic and unlimited. Include the user and remote/phone voices. If exact
  clustering spends one slot on a sub-2s outlier, retry with one spare cluster
  and discard it only when exactly one tiny artifact is isolated. Never use
  the count to assign names—the post-transcription dialog remains the only
  authority for identity.
- **Do not cap speaker-identification dialogs by meeting size.** Six to eight
  speakers is normal. The user can cancel the first dialog to stop the
  sequence; silently suppressing all dialogs above six made the feature appear
  broken.
- **No BSD-specific `date` parsing in shell.** `rec` writes epoch seconds for
  the menu bar precisely so nothing has to parse an ISO string back.
- **Device indices shift.** avfoundation moved the aggregate from index 0 to 6
  when AirPods connected during live testing. Recording selects the configured
  device name and preflights its channel count; do not regress to index-only
  capture.
- **The capture must run under launchd, not as a child of SwiftBar.** macOS
  gives microphone permission to the responsible process. For a SwiftBar
  plugin that is SwiftBar, whose bundle has no
  `NSMicrophoneUsageDescription`, so it can never be granted access and never
  appears in the Microphone list. macOS returns silence rather than an error,
  so the only symptom is an empty recording found hours later. This cost a
  real 3m24s meeting. `bin/rec` kickstarts `com.meetingarchive.recorder`,
  which runs `ma-capture`. Terminal use runs the same program directly,
  because a terminal can hold the permission itself.
- **Never capture audio with ffmpeg.** `ffmpeg -f avfoundation` silently drops
  sample buffers. Measured with a tone beeping once per second, the capture
  came back beeping every 0.80 seconds with the pitch unchanged at 996 Hz --
  meaning a fifth of the audio was never written, not written at the wrong
  rate. It emits no warning, reports no dropped frames, and its own progress
  counter claims the full elapsed time. ffmpeg 7 and ffmpeg 9 both do it, on
  the aggregate device and on the built-in microphone alike, and
  `-thread_queue_size` makes no difference. Under launchd's background
  scheduling the loss reached 73%. The same device through CoreAudio
  (`capture.py`, PortAudio) returns 41.05 seconds of audio in 41.26 seconds
  with no overflow events, and the beeps come back 1.002 seconds apart.
  ffmpeg is still used for offline work -- probing and channel splitting --
  where it is completely reliable.
- **The recorder LaunchAgent must still be `ProcessType=Interactive`.**
  Background scheduling starves sustained capture regardless of backend.
  Capture at the Aggregate Device's native 48 kHz, convert split channels to
  16 kHz only after stop, and keep the duration check in `finalize`.
- **A refusal nobody sees is worse than no refusal.** `rec stop` is normally
  clicked in the menu bar, which discards stderr. When `finalize` correctly
  rejected two short captures, the message went nowhere: no inbox entry, no
  progress, no error, two meetings sitting unmentioned in `in-progress` for
  an hour. `rec` now appends to `logs/rec.log` and shows an alert, `ma status`
  and the SwiftBar menu report stranded captures, and `ma recover` publishes
  or discards them.
- **Digital silence is checked twice, and it is not paranoia.** -91 dBFS is
  all-zero 16-bit audio; a genuinely quiet room reads around -60 dB. The
  preflight probe refuses to start on it and `finalize` refuses to publish
  it, so a permission fault can never reach the archive as an empty
  transcript.
- **A Chroma collection handle is pinned to a uuid.** `ma rebuild` drops and
  recreates the collection, which invalidates the handle held by the running
  watcher. `MeetingStore._run` reopens on that error; without it a rebuild
  silently breaks background indexing until the watcher restarts.

## Tests

```bash
python -m pytest          # 236 tests, ~9s
```

Whisper and sherpa-onnx inference are stubbed dependencies. Their orchestration is
covered end to end; the channel split runs against real ffmpeg on a hand-built
multi-channel wav, and the store tests run against real ChromaDB with an
injected offline embedding function.

Two things to know before writing tests here:

- Don't build multi-channel test audio with ffmpeg's `join` filter. It maps
  mono inputs onto a layout by guessing, not positionally, and an earlier
  fixture made correct code look broken. `make_wav` in `tests/test_pipeline.py`
  writes interleaved PCM by hand for this reason.
- The channel-mapping test is mutation-checked: a one-channel offset in
  `pan_expression` does fail it. Keep it that way. Reversed channels are the
  failure mode where everything appears to work and every label is backwards.

## Style

Comments explain *why*, especially where the code looks odd because of a real
bug. Don't add comments restating what the line does. Prefer failing loudly and
keeping the audio over proceeding on a guess — a meeting that failed to
transcribe keeps its audio indefinitely, at any age.
