# Where Yarn is tied to the Mac

Written before the Windows port, from a read of the code in November 2026.
Nothing here has been ported. It is a map of what would have to move.

## The good news

The part that does the work does not know it is on a Mac. These modules
make no macOS call at all:

`pipeline`, `records`, `merge`, `diarize`, `transcribe` (apart from the
whisper.cpp path), `summarize`, `chunking`, `store`, `names`, `quips`,
`models`, `retention`, `space_saver`, `space_saved`, `jobs`, `queue`,
`watcher`, `library`, `config`, `audio` (it shells out to ffmpeg, which
exists on Windows).

The contract between capture and everything else is already a file
contract: a wav per channel plus a manifest written last into `inbox/`.
Anything that can produce those files is a valid recorder.

## What is Mac-only

| Area | Where | What it relies on |
|---|---|---|
| Recording the microphone | `capture.py` | PortAudio through `sounddevice`. Portable in itself; the device names and the aggregate-device fallback are CoreAudio. |
| Recording call audio | `ui/capture/YarnSystemAudio.swift` | ScreenCaptureKit. Windows needs WASAPI loopback instead. |
| Start, stop, state | `bin/rec` | bash, `launchctl kickstart`, `osascript` alerts, `df`, signals. |
| Background jobs | `ui/install-ui.sh`, `brief.py` | LaunchAgents and `launchctl`. Windows needs Task Scheduler or a service. |
| Menu bar | `ui/meeting.5s.sh.template` | SwiftBar. Windows needs a tray app. |
| Windows and dialogs | everything in `ui/` | AppKit. Five small apps. |
| Popups launched from Python | `popup.py`, `bin/meeting-popup`, `bin/speaker-popup` | builds and opens the Swift apps, falls back to AppleScript. |
| Machine facts | `resources.py` | `sysctl hw.memsize`, `memory_pressure`. |
| Opening things | `ask.py`, `brief.py`, `connect.py`, `cli.py`, `speakers.py` | `/usr/bin/open`, `osascript`, `afplay`. |
| Setup | `setup.py` | `ditto`, `codesign`, `xattr`, `~/Library`, Mac download URLs. |
| Speech engine | `transcribe.py`, config | whisper.cpp with Metal at a Homebrew path. Windows would use faster-whisper (already supported) or a CUDA/Vulkan build. |
| Paths | `config.py` | `~/.config/meeting-archive`, `~/meeting-archive`, `~/Library/Application Support/Yarn`. |

## Where the two are tangled

These are the places a port would trip, because platform calls sit inside
otherwise portable modules:

1. `cli.py` calls `osascript` directly for alerts and the name prompt.
2. `brief.py` writes a LaunchAgent plist and runs `launchctl` itself.
3. `ask.py`, `brief.py` and `speakers.py` each shell out to `open` or
   `afplay` on their own.
4. `resources.py` mixes the policy (should a heavy stage wait?) with how
   the answer is measured on macOS.
5. `connect.py` assumes where Claude, Cursor and ChatGPT keep their config
   files on a Mac.
6. `bin/rec` is the recorder's state machine, and it is a bash script.
   The rules in it (never start twice, refuse silence, alert on a refused
   stop) are product rules that a Windows recorder must also follow.

## The boundary to cut along

One small module, `meeting_archive/platform/`, with a `mac.py` now and a
`windows.py` later, holding only these calls:

- `alert(message)`, `ask_text(prompt, default)`, `notify(title, body)`
- `open_path(path)`, `open_app(name)`, `play_audio(path)`
- `memory_gb()`, `memory_pressure()`
- `schedule_at_login(name, command)`, `unschedule(name)`, `kick(name)`
- `app_support_dir()`, `default_config_path()`, `default_archive_root()`
- `client_config_paths()` for the Connect feature

Then:

1. Move the six tangles above onto those calls. This changes no behaviour
   on the Mac and can be done and tested before any Windows work.
2. Move the recorder's rules out of `bin/rec` into Python (`ma rec start`,
   `ma rec stop`), leaving the script as a thin launcher. The rules then
   have tests and exist once.
3. Only then write the Windows pieces: a WASAPI capture that produces the
   same per-channel wavs and manifest, a tray app, and an installer.

The UI is the larger cost. Five AppKit apps do not port. The realistic
choices are a second native UI for Windows, or one cross-platform UI for
both. That decision should be made before step 3, not during it.

## What must survive the port unchanged

The five rules at the top of `CLAUDE.md`. In particular, on Windows as on
the Mac: capture must be a process that can hold microphone permission
itself, must refuse digital silence, and must never be ffmpeg.
