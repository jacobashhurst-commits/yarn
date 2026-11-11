# Handoff to Claude Code

Working notes from an earlier machine. The current product is the README. These paths are not the install.

The source checkout is `/Users/jashhurst/yarn/meeting-archive-src`; runtime
recordings, transcripts and Chroma data live in
`/Users/jashhurst/yarn/meeting-archive`.

## Current state

Installed and passing:

- Package installed into `meeting-archive-src/.venv`, `ma` symlinked into
  `/opt/homebrew/bin`
- ffmpeg and Python 3.12 are present
- Aggregate Device built: BlackHole on channels 0–1, MacBook Pro Microphone on
  channel 2. The mapping was verified against live per-channel levels on
  2026-09-11, but the device index can change whenever devices connect.
- `~/.config/meeting-archive/config.toml` selects `Aggregate Device` by its
  stable name with
  `mic_channels = [2]`, `sys_channels = [0, 1]`
- 236 tests passing on macOS
- Per-speaker diarization, extractive quips, handoff-based name suggestions and
  the optional speaker naming popup are implemented and verified with real
  local inference.
- SwiftBar, the recorder LaunchAgent, the watcher LaunchAgent, and the daily
  prune LaunchAgent are installed.
- Recording runs as the `com.meetingarchive.recorder` launchd job rather than
  as a child of SwiftBar. macOS only grants microphone access to the
  responsible process, and SwiftBar declares no `NSMicrophoneUsageDescription`
  so it can never hold it — macOS then returns digital silence instead of an
  error. A first live test lost a 3m24s recording to exactly this. The capture
  now refuses to start on an all-zero input, and `finalize` refuses to publish
  one.
- Capture is CoreAudio, not ffmpeg. `ffmpeg -f avfoundation` silently discarded
  sample buffers: a tone beeping once per second was recorded beeping every
  0.80 seconds with its pitch unchanged, so a fifth of the audio was never
  written. ffmpeg 7 and 9 both did it, on the aggregate device and the built-in
  mic, with and without `-thread_queue_size`; under launchd background
  scheduling the loss reached 73%. `meeting_archive/capture.py` records through
  PortAudio as the `ma-capture` console script, verified at 41.05s of audio in
  41.26s with zero overflows and beeps 1.002s apart. ffmpeg still probes files
  and splits channels.
- The recorder LaunchAgent uses interactive scheduling and captures the
  Aggregate Device at its native 48 kHz. Split channels are converted to 16 kHz
  only after stop, and `finalize` rejects any material wall/audio duration
  mismatch.
- A refused recording is now visible. `rec stop` runs from the menu bar, which
  discards stderr, so two correctly-refused captures once vanished with no
  message anywhere. Stop appends to `logs/rec.log` and shows an alert;
  `ma status` and the SwiftBar menu list stranded captures; `ma recover [<id>]
  --publish|--discard` deals with them.
- Transcript Markdown contains a proper summary above the voices and full
  transcript when Ollama is already running. Capture will not start the
  daemon. Deferred summaries are filled from SwiftBar: Open Ollama, then
  Summarize today's meetings (`ma summarize --today`).
- Later versions are noted in ROADMAP.md: V2 better Speaker 1/2/3 guesses
  (still confirm before labelling), V2/V3 home-server store/query/LLM, V4
  one-time-fee packaging for laptop or home server.
- Speaker review warns when the attendee list has more people than the
  diarizer detected, then lets Jacob map only the voices actually
  distinguished. It never forces attendee names onto speech.
- SwiftBar reads `.processing-status.json` and shows the current pipeline
  phase plus a progress bar. Whisper reports audio-position progress as its
  segment iterator advances; diarization, summary, and indexing report
  phase-level percentages.
- `chroma-mcp` is configured in Claude Desktop and a live MCP query returned
  the expected test meeting.

## Local validation on 2026-09-11

- Call capture: mic and BlackHole were separated correctly. With BlackHole
  selected as output, the system channel measured -16.3 dB mean while the mic
  remained at -48.0 dB.
- Room capture: produced only raw + room WAV files, then transcribed, diarized
  and indexed successfully.
- Whisper: warm large-v3-turbo processed 12 seconds in about 25 seconds
  end-to-end and retained the key decision phrase. Warm medium took 8 seconds
  but dropped that phrase, so accuracy won and large-v3-turbo remains selected.
- Diarization: the public four-speaker fixture produced four clusters at the
  calibrated 0.9 threshold. A live two-voice room test produced two speakers
  in 6.7 seconds, inferred Marina and John from self-introductions, showed
  verbatim quips, and reindexed confirmed names.
- Search: both `ma search` and a live `chroma-mcp` stdio query returned the
  blue-project decision.

## Recovery checks

### 1. Verify the channel mapping

The recorder now selects `Aggregate Device` by name because AVFoundation moved
it between indices 0 and 6 as AirPods connected. It also preflights the channel
count before every recording.

```bash
bin/rec start
# have Jacob say "this is the microphone channel", then play a video
bin/rec stop
```

Inspect what landed. `__mic.wav` must contain only Jacob's voice and
`__sys.wav` only the video. If they are swapped, fix `mic_channels` /
`sys_channels` in config rather than changing code.

macOS will prompt for microphone permission on the first run. If it is missed
or denied, recording silently produces silence — check this first if `mic.wav`
is empty.

### 2. Run Whisper for real

Let the watcher process that recording, then read the transcript. Check:

- speaker labels match who actually spoke
- no hallucinated text in the silent stretches (the classic symptom is
  "Thanks for watching!" or a repeated phrase in a gap)
- timestamps roughly line up
- decode speed against real time — if a 30-minute meeting takes much longer
  than a few minutes to transcribe, try `compute_type = "int8_float16"` or drop
  to `model = "medium"`

The Whisper weights are installed in the local cache and
`local_files_only = true`, so normal meeting processing performs no model-host
check. Keep the measured speed/accuracy trade-off above in mind before changing
the model.

### 3. Test room mode

```bash
bin/rec start room
bin/rec stop
```

Verify a single `__room.wav` was produced with no `__sys.wav`. With diarization
active it should use meeting-local `Speaker N` labels; without diarization it
must safely fall back to `Unknown:` and never `Jacob:`.

### 4. Run diarization and speaker identification

Process a multi-person recording with the local sherpa-onnx models and verify
the optional dialog shows one verbatim quip per voice, handoff guesses remain
suggestions, and confirmed names re-render and reindex the transcript.

### 5. Wire up chroma-mcp

`ma doctor` prints the exact JSON. Use the repo launcher
`meeting-archive-src/bin/chroma-mcp` (not bare `uvx chroma-mcp`) — it pins
`pydantic<2.14` so Claude/Cursor updates don't break on the
`eval_type_backport` import. Add the snippet to Claude Desktop and/or
`~/.cursor/mcp.json`, restart the host, and confirm a real query against the
archive returns the meeting. Needs `uv` installed (`brew install uv`).

### 6. Verify the menu bar

Both recording modes startable from the menu, timer counting, stop working, and
the ⚠️ failed indicator appearing if a meeting is quarantined.

## Things to be careful about

Read `CLAUDE.md` first. The non-negotiable constraints there came from Jacob
directly, and the "traps found the hard way" section lists fixes that look
removable but are not.

Diarization is meeting-local and does not identify anyone by itself. The label
precedence remains: Jacob-confirmed name, then `Speaker N`, then the honest
channel fallback. Never turn transcript-derived guesses into names without
human confirmation.
