# Yarn

Your meetings. Your memories. Your computer.

Yarn records a meeting when you press the button, works out who said what, and keeps the transcript, the notes, and a daily brief on your Mac. No account. No subscription. Recording, transcription, summaries, and Ask Yarn stay on this machine.

Apple Silicon only for now, Windows coming soon.

## Download

Unzip and double-click **Yarn Setup**. Click Install Yarn and leave the window open. Yarn then sits in the Dock and in the menu bar. You do not need Python or Xcode.

- [Yarn.Setup.Apple.Silicon.zip](https://github.com/jacobashhurst-commits/yarn/releases/latest/download/Yarn.Setup.Apple.Silicon.zip) is the installer for most places. Setup downloads the speech model.
- [Yarn.Setup.Japan.Apple.Silicon.zip](https://github.com/jacobashhurst-commits/yarn/releases/latest/download/Yarn.Setup.Japan.Apple.Silicon.zip) is about 1.6 GB. The speech model is already inside. Use this where that download stops before it finishes.

The same two files are on the [latest release](https://github.com/jacobashhurst-commits/yarn/releases/latest). Code, then Download ZIP, is the source tree, not the app.

## Using it

Open Yarn.

**In the room** uses the laptop microphone. Several people can be in the room. Yarn does not decide that the person who owns the laptop was the one speaking.

**Headphones** is a call. Your voice is the microphone. Everyone else is what the Mac is playing, including audio that only goes to the headphones, so the speakers can stay quiet. The first time you press Headphones, Yarn asks what to call you. That name is you on the recording.

Nothing starts a recording except that button. Yarn does not watch the calendar, and it does not listen for a meeting on its own.

When you stop, the audio is already on disk. You can add notes and say who was there. Closing that window does not drop the meeting. After the transcript is ready, Yarn asks you to name the voices it separated. A suggestion is only a suggestion. Yarn does not label a person it cannot actually identify.

Yarn then writes a summary on this Mac, and a daily brief of what needs attention. You can search the meetings, or ask Yarn about them, without sending them anywhere. Audio is kept for a week by default. You can keep it for a month, or keep it. A recording that never became a transcript is kept. The transcript stays either way.

The first room recording asks for the microphone. A headphone call also needs Yarn under System Settings, Privacy & Security, Screen & System Audio Recording. On a newer Mac there is no Allow button. Click + and choose Yarn from the Applications folder in your home folder. On an older Mac that row is called Screen Recording.

Ask Yarn stays on this Mac. Settings can also connect Claude, Cursor, or ChatGPT. When one of those apps searches, it receives the passages it asked for, and those passages then go wherever that app sends them.

## How it is put together

```
You press record
        │
        ▼
Microphone, and on a headphone call the audio the Mac is playing
        │
        ▼
Stop. The recording is saved, then a manifest is written last
        │
        ▼
On this Mac: transcribe, separate voices, write the transcript
        │
        ├── summary and daily brief, if the local model is available
        └── search index, rebuilt any time from the transcripts
```

The transcript on disk is the record. The search index is a copy that can be thrown away and rebuilt.

There is no mode switch inside the pipeline. The channel is the fact:

| Channel | What it is | Who it can be |
|---|---|---|
| `mic` | Your microphone on a headphone call | The name you gave Yarn |
| `sys` | What the Mac is playing on that call | Everyone else, then Speaker 1, Speaker 2, once you name them |
| `room` | One microphone, several people | Unknown, until you name the voices |

A headphone call does not need an aggregate audio device. The older BlackHole layout is only a fallback, and a silent far side stays silent rather than being copied from the microphone.

The longer design notes are in [docs/audit/01-high-level-architecture.md](docs/audit/01-high-level-architecture.md).

## From a checkout

Daily use is the installer above. From this repo, on an Apple Silicon Mac:

```bash
brew install ffmpeg
python -m pytest
```

`ma doctor` checks a hand-built setup. `ma watch` is the background job the installer starts for you. `config.example.toml` matches that setup. Copy it to `~/.config/meeting-archive/config.toml` only if you are not using Yarn Setup.

## Licence

[MIT](LICENSE). Free to use, change, and share.
