# meeting-archive

Manually triggered meeting recording, local transcription, and semantic search
over the archive from Claude. No cloud, no auto-triggering, no paid tools.

Recording only ever starts because you ran a command or pressed a hotkey.
Nothing in here listens for meetings, watches your calendar, or decides on its
own that something is worth capturing.

## How it works

```
rec start ──▶ CoreAudio captures the aggregate device (mic + system audio)
rec stop  ──▶ splits the channels, publishes a manifest to inbox/, offers a popup
                │                                       │
                ▼                                       ▼
          ma watch picks it up:                   optional, dismissible
          transcribe each channel                 notes + attendees
          diarize multi-person channels
          merge into one transcript                       │
          detect names from the text                      │
          write transcript to disk  ◀─── reindexed ───────┘
          index into ChromaDB                     whenever they arrive
                │
                ▼
          Claude queries it via chroma-mcp
```

### Two recording modes

**`rec start room`** (the recommended v1 choice) — one laptop microphone for
an in-room meeting or a speakerphone/Zoom call without headphones. The mic
acoustically captures both you and the laptop speakers. Diarization
distinguishes `Speaker 1:`, `Speaker 2:`, and so on. Until you confirm their
names the system never guesses labels. If diarization is unavailable, every
turn safely falls back to `Unknown:`.

**`rec start`** (separate-channel call mode) — your microphone is you and
BlackHole system audio is everyone else. Use this with headphones after
configuring a Multi-Output Device. Your channel reads `Jacob:`; system audio is
diarized into meeting-local speakers. Without routing into BlackHole the system
channel is silent, and without headphones speaker leakage duplicates far-side
speech on both channels.

Room mode is not just call mode with a quiet second channel. If it were, every
word your colleagues said would be attributed to you — which is worse than no
label at all, because it is confidently wrong and nobody would notice for
months. Name detection changes too: in a room, "I'm Marina" is Marina
introducing herself rather than you introducing yourself, so it is kept rather
than discarded.

Speaker identity is meeting-local: `Speaker 1` in one recording is not assumed
to be the same person in another. After transcription, an optional dialog shows
a recognisable verbatim line from each voice so you can name it. Confirmed
names re-render and reindex the transcript without running Whisper again.

**Transcripts on disk are the source of truth.** ChromaDB is a derived index.
Delete the whole `chroma/` directory and `ma rebuild` puts it back from the
text. That is what makes the embedding choice, the chunk size and the context
header all reversible later.

## Setup

### Mac (Apple Silicon)

Open the Yarn folder and double-click **Yarn Setup**. Read **Start here.txt**
if the Mac refuses to open it (unsigned download). Setup copies Yarn out of
Downloads into `~/Library/Application Support/Yarn` so the recorder can run
as a background job — macOS blocks LaunchAgents from reading Downloads.
When it finishes, Yarn is in the Dock: record, watch progress, name voices,
read the daily brief, and ask about meetings on this Mac. A menu-bar icon
does the same things. Developers can still run `bin/yarn-setup --cli`.

### 1. Install (developers)

```bash
brew install ffmpeg
git clone <this repo> && cd meeting-archive
pip install -e .
cp config.example.toml ~/.config/meeting-archive/config.toml
```

### 2. Capture system audio (optional separate-channel mode)

macOS cannot record its own output. Install [BlackHole
2ch](https://github.com/ExistentialAudio/BlackHole) (free, open source), then
build an aggregate device so one recording contains both you and the call:

Skip this section when using the recommended laptop-mic mode: `rec start room`
captures an in-room meeting or speaker call through the microphone alone.

1. Open **Audio MIDI Setup**
2. `+` at the bottom left → **Create Aggregate Device**
3. Tick your microphone and **BlackHole 2ch**
4. Tick **Drift Correction** on the microphone row, but *not* on BlackHole.
   BlackHole is the clock source, so it is the reference everything else is
   corrected to. This is the entire reason for using an aggregate device
   instead of two recorders: without it the two channels drift apart over an
   hour, and since the transcript is merged on timestamps, drift scrambles who
   said what.
5. Check the **Subdevices** strip at the top and note the order. It follows the
   order you ticked, not the order shown in the list.
6. Name it something like `Meeting Capture`
7. In **System Settings → Sound**, set output to a **Multi-Output Device**
   containing both BlackHole and your headphones, so you can still hear the
   call while BlackHole captures it

The Subdevices order is what `mic_channels` and `sys_channels` refer to.
BlackHole first gives `sys = [0, 1]` and `mic = [2]`; the mic first gives
`mic = [0]` and `sys = [1, 2]`.

**Wear headphones.** On speakers your microphone also picks up the far side, so
both channels transcribe the same words and the transcript doubles up.

### 3. Point the config at the device

```bash
ma devices          # find the index of your aggregate device
```

Set `audio.device_name = "Aggregate Device"` (or the exact name you chose) in
the config, then:

```bash
ma doctor           # everything should be [ok]
```

### 4. Prove the capture before anything else

Do this before caring about transcription quality. If the channel mapping is
wrong, everything downstream still appears to work and every speaker label is
simply backwards.

```bash
rec start
# say something, then play a video or ring yourself on a call
rec stop
ma status           # one recording waiting in the inbox
```

Then check the two channels landed the right way round:

```bash
ffplay ~/meeting-archive/recordings/inbox/*__mic.wav   # should be only you
ffplay ~/meeting-archive/recordings/inbox/*__sys.wav   # should be only them
```

If they are swapped, fix `mic_channels` / `sys_channels`. The raw
multi-channel capture is kept alongside the split channels until the meeting is
processed, so a wrong mapping is recoverable rather than a lost meeting.

### 5. Run the watcher

```bash
ma watch
```

Only for this first check — step 6 installs it as a background job so you never
run this by hand again.

Speaker diarization uses sherpa-onnx with model files downloaded from its
public GitHub releases into the archive's `models/diarization/` directory.
There is no account, token, hosted API, or network access at inference time.
If the runtime or model files are unavailable, transcription and indexing
continue with the honest `Other`/`Unknown` fallback labels.

Install the public model files once:

```bash
bin/install-diarization-models
```

### 6. Install the menu bar UI

```bash
ui/install-ui.sh
```

This is what removes the terminal from daily use. It installs:

- a **Dock app** — record, watch progress, name voices, open the daily brief,
  and ask about meetings
- a **SwiftBar menu bar item** — the same controls, including the running
  timer. While processing, it shows a progress bar and the active phase:
  transcription, speaker identification, summary, or search indexing.
- a **recorder LaunchAgent**, idle until you start a recording, which is what
  actually captures the audio (see below)
- a **LaunchAgent running the watcher**, so transcription and indexing happen
  in the background from login, with no terminal window open
- a **daily prune** at 09:00 for audio past the retention window

SwiftBar is the only new dependency: free, open source, and it hosts shell
scripts in the menu bar. The installer asks before installing it. When it
offers to pick a plugin folder, choose `~/.swiftbar-plugins` — that is where
the installer writes the plugin.

**Why the recorder is a separate LaunchAgent.** macOS grants microphone
permission to the *responsible process*, which for anything a SwiftBar plugin
launches is SwiftBar itself. SwiftBar's bundle declares no
`NSMicrophoneUsageDescription`, so it can never hold that permission and never
even appears in the Microphone list in System Settings. macOS does not report
this as an error — it hands the process a stream of zeros, and the only
symptom is a recording that is perfectly, silently empty. Running the capture
as a launchd job makes the recorder responsible for itself, so the permission
is requested once, granted properly, and remembered. Allow it the first time
you record. That job is explicitly scheduled as `Interactive`; background
scheduling can starve sustained AVFoundation capture even though short probes
look healthy. The raw recording stays at the device's native 48 kHz, then the
finished channel files are converted to 16 kHz for Whisper. Stop also compares
audio duration with wall-clock duration and refuses a damaged recording
instead of quietly producing a partial transcript.

### Local summaries

Each readable `.md` file contains a concise summary above the full transcript
when Ollama is already running. Capture does **not** start Ollama: if the app
is closed, the transcript still saves and the summary is deferred. From the
menu bar: **Open Ollama**, then **Summarize today's meetings**.

If you would rather use a hotkey, point Raycast, Alfred, Shortcuts or skhd at
`bin/rec toggle` for calls or `bin/rec toggle room` for in-person meetings. The
menu bar and hotkeys share the same state file, so mixing them is fine.

### 7. Connect Claude

Add to `claude_desktop_config.json` (`ma doctor` prints this with your real
path filled in):

```json
{
  "mcpServers": {
    "chroma": {
      "command": "/ABS/PATH/TO/meeting-archive-src/bin/chroma-mcp",
      "args": ["--client-type", "persistent",
               "--data-dir", "/Users/you/meeting-archive/chroma"],
      "env": {"ANONYMIZED_TELEMETRY": "false"}
    }
  }
}
```

Use the repo launcher (`bin/chroma-mcp`), not bare `uvx chroma-mcp`. The
launcher pins `pydantic<2.14` so Claude/Cursor updates don't pull a pydantic
release that breaks `mcp`/`chroma-mcp`. `ma doctor` prints this with your real
paths filled in.

Then ask things like *"what did we decide about the pricing tiers?"* or *"find
the meeting where Marina raised the billing issue"*.

Needs a local MCP host — Claude Desktop, Claude Code, or Cursor — not
claude.ai in a browser. Your workplace may need to approve it.

## Commands

| | |
|---|---|
| `rec start` / `rec stop` / `rec toggle` | recording control, call mode |
| `rec start room` / `rec toggle room` | recording control, in-person meeting |
| `ma watch` | the long-running processor |
| `ma doctor` | check the setup |
| `ma devices` | list audio devices |
| `ma status` | what's indexed, waiting, failed, or stranded |
| `ma recover` | publish or discard a capture that was refused |
| `ma search "<query>"` | search without going through Claude |
| `ma names <meeting-id>` | detected names and the evidence for each |
| `ma speakers <meeting-id>` | diarized voices, quips, and confirmed names |
| `ma speakers <meeting-id> --set room-2=Marina` | name a voice for reindexing |
| `ma process` | process the inbox once and exit |
| `ma rediarize <meeting-id>` | rebuild speakers from retained audio without rerunning Whisper |
| `ma ingest <meeting-id>` | reindex one meeting |
| `ma rebuild` | reindex everything from the transcripts |
| `ma prune` | delete archived audio past the retention window |

## The popup

Two dialogs after you stop recording: freeform notes, and who actually spoke
in the recording. Include yourself and anyone heard over a phone or video call.
Both are optional, both dismissible with Escape, and neither blocks anything.
If you dismiss them the meeting is processed with automatic, unlimited speaker
clustering.

Notes are not required up front because processing starts immediately. Type
them ten minutes later and that meeting is quietly reindexed with them.

### Name suggestions

Because calendar lookup is deferred, the speaker box is the only place
participant names come from — so the transcript is scanned for names people
actually said:

- self-introductions — *"I'm Marina"*, *"Ravi here"*
- greetings and thanks — *"hey John"*, *"thanks, Priya"*
- handovers — *"over to you Tom"*
- vocatives — *"what do you think, Dave?"*, *"Nikhil, can you share?"*

If you left the speaker box empty, a second dismissible dialog appears with
what was found, pre-filled and editable. It never applies anything silently.

This is pattern matching, not a model, and tuned for precision over recall: a
wrong suggestion costs you a correction every meeting until you stop trusting
the box, while a missed name costs you one typed word. It only finds Title Case
first names, skips prefixed surnames like McKenzie rather than mangling them,
and deliberately sacrifices names that collide with common words (April, May).
Whisper's spelling of names is unreliable regardless, which is why these are
only ever suggestions.

`ma names <meeting-id>` shows what was detected and the phrase that triggered
each one.

### Speaker identification

Once transcription and diarization finish, a second optional dialog asks who
each `Speaker N` was. It includes speaking duration, first timestamp, and a
short verbatim excerpt selected from that voice's transcript. Self-
introductions and handoffs such as “I'll pass to John” may pre-fill a suggested
name, but suggestions are never applied without your confirmation.

The excerpts are selected locally with deterministic scoring rather than a
generative model, so the dialog cannot invent a quote. Dismissing it leaves the
transcript searchable with honest `Speaker N` labels. You can name voices later
with `ma speakers`.

## Audio retention

Audio is kept **7 days** after a meeting is transcribed, then deleted by
`ma prune`. Transcripts are kept forever.

Not deleted on successful embed, because the first few weeks are when wrong
input devices, bad gain and half-captured meetings get discovered, and none of
that is recoverable from a transcript. Audio for a meeting that *failed* to
transcribe is never deleted at any age.

Schedule the prune however you like:

```bash
0 9 * * * cd /path/to/meeting-archive && ma prune
```

## Layout

```
~/meeting-archive/
  recordings/
    in-progress/   currently recording
    inbox/         waiting to be processed
    processing/    claimed by the watcher
    archive/       processed, inside the retention window
    failed/        something went wrong; audio kept
  transcripts/2026/09/   `{id}.json` (source of truth) + `{date} {title}.md` (readable)
  notes/         popup output
  chroma/         the derived index
  logs/
```

## Design notes

**One recording device, not two.** Two capture streams on two devices drift
apart over an hour. Since the transcript is merged on timestamps, drift would
slowly scramble who said what. An aggregate device gives one clock and
sample-aligned channels.

**Listed voices constrain one meeting; blank remains unlimited.** Automatic
speaker clustering has no global cap, but a threshold cannot reliably tell
eight people from one voice whose sound changes with distance, a phone, or
background music. Names entered in "Who spoke?" provide the expected count for
that recording only. If exact clustering spends one slot on a sub-two-second
outlier, Yarn retries with one spare cluster and discards it only when exactly
one unambiguous tiny artifact is isolated. Speaker identity is still confirmed
afterward; a count never assigns a name.

**Chroma's default embedding function, on both sides.** Not a quality
preference — `chroma-mcp` only offers default, cohere, openai, jina, voyageai
and roboflow. No BGE, no Nomic. If this pipeline wrote BGE vectors and the MCP
server queried with its default embedder, every search would return confident
nonsense and nothing would look broken. Changing embedder later is a config
edit plus `ma rebuild`.

**Manifest written last.** The watcher triggers only on manifest files, so it
can never pick up a recording whose audio is still being written.

**Polling, not filesystem events.** A folder that gets a file a few times a day
does not need FSEvents, and polling has no failure mode where a dropped event
silently costs a meeting.

**Flattened metadata.** Chroma accepts list-valued metadata and then silently
fails to filter on it — `{"speakers": {"$eq": "Jacob"}}` matches nothing. Lists
are joined to strings on the way in, and there is a test asserting the field
matches its own value.

**Aggressive VAD.** The far-side channel is mostly silence whenever you are
talking, and long silence is where Whisper invents text. faster-whisper's
default only strips silence over two seconds, which is far too permissive here.

**Channel ids carry the knowledge of who is speaking**, rather than a mode flag
threaded through the pipeline. `mic` is known to be you, `sys` is known to be
everyone else, `room` is known to be unidentifiable. A mode flag set wrongly
would mislabel a whole meeting; a channel id travels with the audio it came
from and is checked by the same tests either way.

**Capture never goes through ffmpeg.** `ffmpeg -f avfoundation` drops sample
buffers without saying so. A tone beeping once a second came back beeping every
0.80 seconds at an unchanged pitch — a fifth of the audio simply missing, not
slowed down. ffmpeg logs no warning and its own progress counter reports the
full elapsed time, so the only visible symptom is a transcript with a fifth of
the words gone. Recording goes through CoreAudio instead, which returns 41.05
seconds of audio in 41.26 seconds and puts those beeps back 1.002 seconds
apart. ffmpeg still does the probing and channel splitting, where it is
completely reliable.

**A refused recording has to be visible.** `rec stop` usually runs from the menu
bar, which throws stderr away, so a refusal used to vanish completely: no inbox
entry, no progress, no error, and a meeting left in `in-progress` with nothing
pointing at it. Stop now writes to `logs/rec.log` and raises an alert, and both
`ma status` and the menu report stranded captures until `ma recover` deals with
them.

**The recorder selects the device by name and preflights its channel count.**
AVFoundation renumbers audio devices as things connect and disconnect — during
testing, AirPods moved the aggregate device from index 0 to index 6. A stable
name avoids that entire class of failure, and the short preflight prevents a
misbuilt aggregate device from costing a meeting.

## Tests

```bash
pip install pytest && python -m pytest
```

The channel split runs against real ffmpeg on a hand-built multi-channel wav,
and the store tests run against real ChromaDB with a deterministic offline
embedding function. Whisper and sherpa-onnx inference are stubbed; their
orchestration is covered end to end.

## Deferred

- **Calendar lookup** — needs admin consent for Workspace/M365 from a local
  script, which can outlast the rest of the build. The no-approvals fallback is
  reading the local calendar store or a periodic `.ics` export.
- **Cross-meeting voice recognition.** Diarization distinguishes voices inside
  one recording only. The system deliberately does not infer that a voice is
  the same person across different meetings.
