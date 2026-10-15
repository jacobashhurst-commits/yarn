# Yarn V2 performance log

Local only. No cloud telemetry. Fill this in as `ma benchmark` and live
meetings produce numbers. Do not invent a single quality score.

## Hardware floor

MacBook Neo — Apple Silicon A18 Pro, 8 GB unified memory.

Whisper stays large-v3-turbo on this machine. Written summaries do not:
qwen3.5:9b needs ~7.1 GiB free, and an 8 GB Neo with the OS and a few
apps only has ~4.8 GiB. Setup therefore installs qwen3.5:4b here and
tells you. The work laptop still uses 9B. Evening scheduling used to
wait until other apps were closed so 9B might fit; that is not a launch
default.

## Baseline (pre-V2, 2026-09-11, work laptop)

From `HANDOFF.md`, Apple Silicon MacBook Pro:

| Check | Result |
|---|---|
| Whisper large-v3-turbo, warm, 12s audio | ~25s end-to-end; kept the key decision phrase |
| Whisper medium, warm, same clip | ~8s; **dropped** the key phrase |
| Decision | keep large-v3-turbo |
| Diarization, public 4-speaker fixture | 4 clusters at threshold 0.9 |
| Live 2-voice room | 2 speakers in 6.7s |
| Capture vs ffmpeg | CoreAudio 41.05s audio in 41.26s; ffmpeg dropped ~20% of samples |

## This laptop (MacBook Neo) — 2026-09-17

Setup only. Live quality bake-off not yet run.

| Piece | Installed |
|---|---|
| whisper.cpp | 1.9.4 (`whisper-cli`), ggml-large-v3-turbo 1.6 GB |
| faster-whisper | present (VAD + CUDA-server fallback) |
| sherpa-onnx | 1.13.8 |
| Ollama | qwen3.5:4b on ≤10 GB; qwen3.5:9b otherwise |
| Unit/integration tests | 267 passing after V2 queue/retention/import/library landing |

### Engine bake-off (large-v3-turbo, same files)

Run `ma benchmark` against `benchmark_audio/` (gitignored).

| File | Engine | Duration | Wall | RTF | Peak RSS | Notes |
|---|---|---|---|---|---|---|
| _pending_ | whisper_cpp | | | | | |
| _pending_ | faster_whisper | | | | | |

Do not switch the default off whisper.cpp until this table shows equal or
better proper-noun and key-phrase retention.

### Diarisation bake-off

| File | Engine | Voices expected | Voices found | Wall | RAM | Notes |
|---|---|---|---|---|---|---|
| _pending_ | sherpa | | | | | stable |
| _pending_ | pyannote | | | | | experimental |

### Model lifecycle

On machines reporting ≤10 GB unified memory, the watcher unloads Whisper
after transcription and the diarizer after speaker attribution, then loads
Ollama only if it is already running. Higher-memory Macs keep models warm.
