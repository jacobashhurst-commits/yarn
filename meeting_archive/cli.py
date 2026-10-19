"""Command line interface.

    ma doctor                 check the machine is set up correctly
    ma devices                list audio devices and their channel counts
    ma rec-config             emit shell vars for bin/rec (internal)
    ma finalize <raw.wav>     split channels, publish to inbox (internal)
    ma recover [<id>]         publish or discard a capture that was refused
    ma notes <id> ...         save popup output (internal)
    ma watch                  the long-running processor
    ma process [<id>]         process the inbox once, then exit
    ma queue                  show the persistent processing queue
    ma pause / resume         pause or resume background processing
    ma import-audio PATH      convert a recording and enqueue it
    ma import-transcript PATH archive a coworker transcript without Whisper
    ma library                list meetings; --html writes a local viewer
    ma keep-audio / delete-audio
    ma setting KEY VALUE      recognise_known, speaker_engine, audio_retention
    ma rediarize <id>         rebuild speaker labels without rerunning Whisper
    ma ingest <id>            reindex one meeting from its transcript
    ma rebuild                reindex every transcript on disk
    ma summarize [--today]    fill in deferred summaries once Ollama is open
    ma brief [--open]         write a local daily HTML brief from recent meetings
    ma prune                  delete archived audio past the retention window
    ma search <query>         sanity-check search without going through MCP
    ma names <id>             show detected names and the evidence for each
    ma speakers <id>          show diarized voices; --set key=name to name one
    ma voices                 known-speaker library; --html / --play NAME
    ma benchmark              run local benchmark_audio/ files if present
    ma status                 counts and recent meetings
    ma setup                  open the Mac installer window
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import audio, capture, jobs, models, pipeline, records, retention, watcher
from .config import Config, load_config, write_setting
from .diarize import Diarizer
from .models import format_timestamp
from .records import Transcript
from .store import MeetingStore
from .summarize import Summarizer
from .transcribe import Transcriber


def invoked_ma() -> str:
    """The `ma` binary that is running, not whatever happens to be on PATH.

    SwiftBar's PATH does not include the venv, so `which ma` misses and a
    login job was written for /opt/homebrew/bin/ma, which is not installed.
    """
    candidate = Path(sys.argv[0]).resolve()
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("ma")
    return found or str(candidate)


def _tell(message: str, *, alert: bool = False) -> None:
    """Surface a line the menu bar would otherwise throw away."""
    if alert:
        script = 'display alert "Yarn" message (item 1 of argv) as warning'
    else:
        script = 'display notification (item 1 of argv) with title "Yarn"'
    subprocess.Popen(
        [
            "/usr/bin/osascript",
            "-e",
            "on run argv",
            "-e",
            script,
            "-e",
            "end run",
            "--",
            message,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    handlers.append(logging.FileHandler(cfg.logs_dir / "meeting-archive.log"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_doctor(cfg: Config, args) -> int:
    ok = True

    def check(label: str, passed: bool, detail: str = "", remedy: str = "") -> None:
        """A remedy is only useful when the check failed, so only show it then."""
        nonlocal ok
        mark = "ok  " if passed else "FAIL"
        note = detail if passed else (remedy or detail)
        print(f"[{mark}] {label}" + (f" -- {note}" if note else ""))
        ok = ok and passed

    for tool in ("ffmpeg", "ffprobe"):
        found = shutil.which(tool)
        check(tool, found is not None, detail=found or "", remedy="brew install ffmpeg")

    cfg.ensure_dirs()
    check("directories", all(d.exists() for d in cfg.all_dirs()), str(cfg.root))

    if cfg.audio.device_name:
        current_index = audio.audio_device_index(cfg.audio.device_name)
        check(
            "audio.device_name",
            current_index is not None,
            detail=f'"{cfg.audio.device_name}" -> current index {current_index} '
                   f"(mic={cfg.audio.mic_channels}, sys={cfg.audio.sys_channels})",
            remedy=f'"{cfg.audio.device_name}" is not currently available; '
                   "run `ma devices` and check the configured name",
        )
        name = cfg.audio.device_name if current_index is not None else None
    elif cfg.audio.device_index < 0:
        check(
            "audio.device_index",
            False,
            remedy="unset -- run `ma devices` and set audio.device_name in the config",
        )
        name = None
    else:
        # Resolve the index to a name. avfoundation renumbers devices as things
        # connect and disconnect, so a stale index would otherwise record the
        # wrong device without any visible sign.
        name = audio.audio_device_name(cfg.audio.device_index)
        check(
            "audio.device_index",
            name is not None,
            detail=f"index {cfg.audio.device_index} -> \"{name}\" "
                   f"(mic={cfg.audio.mic_channels}, sys={cfg.audio.sys_channels})",
            remedy=f"index {cfg.audio.device_index} matches no current audio device; "
                   f"run `ma devices` -- indices shift when devices connect",
        )
        if name is not None and "aggregate" not in name.lower():
            print(
                f"[warn] \"{name}\" does not look like an aggregate device. "
                f"Recording it will capture one source, not both sides."
            )

    # ffmpeg's view of the device above is only used for listing. Recording goes
    # through CoreAudio, and the two do not always agree on what exists, so the
    # path that actually captures audio has to be checked on its own.
    if name is not None:
        required = max(cfg.audio.mic_channels + cfg.audio.sys_channels) + 1
        try:
            device = capture.resolve_device(name, required)
            check(
                "recorder (CoreAudio)",
                True,
                detail=f'"{device["name"]}" with {device["max_input_channels"]} '
                       f'channel(s) at {int(device["default_samplerate"])} Hz',
            )
        except capture.CaptureError as exc:
            check("recorder (CoreAudio)", False, remedy=str(exc))
        except ImportError:
            check("recorder (CoreAudio)", False,
                  remedy="sounddevice is missing; run 'pip install -e .'")

    if cfg.whisper.engine == "whisper_cpp":
        command = shutil.which(cfg.whisper.command) or cfg.whisper.command
        check(
            "whisper.cpp binary",
            Path(command).exists(),
            detail=command,
            remedy=f"no whisper.cpp binary at {cfg.whisper.command}; install it "
                   "with `brew install whisper-cpp`, or set "
                   'whisper.engine = "faster_whisper" in config',
        )
        model_path = Path(cfg.whisper.model_path).expanduser()
        model_present = model_path.is_file()
        size = f" ({model_path.stat().st_size / 1e9:.1f} GB)" if model_present else ""
        check(
            "whisper.cpp model",
            model_present,
            detail=f"{cfg.whisper.model} at {model_path}{size}",
            remedy=f"no converted model at {model_path}; see the model setup "
                   "section of the README",
        )

    # faster-whisper is needed either as the engine itself or, under
    # whisper.cpp, for the Silero VAD that trims the silent channel.
    needs_faster_whisper = (
        cfg.whisper.engine == "faster_whisper" or cfg.whisper.vad_filter
    )
    if needs_faster_whisper:
        reason = (
            f"model={cfg.whisper.model}"
            if cfg.whisper.engine == "faster_whisper"
            else "used for Silero VAD"
        )
        try:
            import faster_whisper  # noqa: F401
            check("faster-whisper", True, detail=reason)
        except ImportError:
            check("faster-whisper", False, remedy="pip install -r requirements.txt")

    # Diarization is reported but never fails the check: without it the archive
    # still works, it just cannot tell two people apart.
    if not cfg.diarize.enabled:
        print("[info] diarization disabled in config")
    else:
        try:
            import sherpa_onnx  # noqa: F401

            segmentation = Path(cfg.diarize.segmentation_model).expanduser()
            embedding = Path(cfg.diarize.embedding_model).expanduser()
            if segmentation.is_file() and embedding.is_file():
                print(
                    f"[ok  ] diarization -- model={cfg.diarize.model}, local models found, "
                    f"channels={cfg.diarize.diarize_channels}"
                )
            else:
                missing = [
                    str(path)
                    for path in (segmentation, embedding)
                    if not path.is_file()
                ]
                print(
                    f"[warn] diarization -- missing local model(s): "
                    f"{', '.join(missing)}. Without them every voice on a call "
                    f"stays '{cfg.other_label}' and a room stays '{cfg.room_label}'."
                )
        except ImportError as exc:
            print(f"[warn] diarization -- sherpa-onnx could not load: {exc}")

    if cfg.summary.enabled:
        command = Path(cfg.summary.command).expanduser()
        try:
            proc = subprocess.run(
                [str(command), "list"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            models = {
                line.split()[0]
                for line in proc.stdout.splitlines()[1:]
                if line.split()
            }
            from .resources import SUMMARY_MODEL_FULL, summary_model_choice

            if proc.returncode == 0 and cfg.summary.model in models:
                print(
                    f"[ok  ] local summary -- Ollama model={cfg.summary.model}"
                )
            else:
                print(
                    f"[warn] local summary -- model {cfg.summary.model!r} is not "
                    "installed locally; summaries will be skipped"
                )
            _, notice = summary_model_choice()
            if notice and cfg.summary.model == SUMMARY_MODEL_FULL:
                print(f"[warn] local summary -- {notice}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[warn] local summary -- Ollama unavailable: {exc}")
    else:
        print("[info] local summary disabled in config")

    # Only the menu bar needs this. A recording started from a terminal runs
    # ffmpeg directly, because the terminal can hold the microphone permission
    # itself; SwiftBar cannot. See bin/rec-capture.
    if subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/com.meetingarchive.recorder"],
        capture_output=True,
    ).returncode == 0:
        print("[ok  ] recorder agent -- loaded (menu bar recording will work)")
    else:
        print(
            "[warn] recorder agent -- not loaded. Recording from the menu bar "
            "would capture silence, because macOS will not give SwiftBar "
            "microphone access. Run ui/install-ui.sh."
        )

    try:
        import chromadb  # noqa: F401
        store = MeetingStore(cfg.chroma_dir, cfg.collection)
        check("chromadb", True, detail=f"collection '{cfg.collection}', {store.count()} chunks")
    except ImportError:
        check("chromadb", False, remedy="pip install -r requirements.txt")
    except Exception as exc:
        check("chromadb", False, remedy=str(exc))

    transcripts = list(cfg.transcripts_dir.rglob("*.json"))
    print(f"[info] {len(transcripts)} transcript(s) on disk")
    pending = list(cfg.inbox_dir.glob("*.json"))
    if pending:
        print(f"[info] {len(pending)} recording(s) waiting in the inbox")
    failed = list(cfg.failed_dir.glob("*.json"))
    if failed:
        print(f"[warn] {len(failed)} meeting(s) in failed/ -- see logs")

    print()
    print("Archive root:", cfg.root)
    print("Chroma dir:  ", cfg.chroma_dir)
    print()
    # Prefer the repo launcher - bare `uvx chroma-mcp` re-resolves deps and
    # breaks when pydantic>=2.14 drops eval_type_backport (mcp still imports it).
    launcher = Path(__file__).resolve().parents[1] / "bin" / "chroma-mcp"
    print("Point Claude/Cursor at the archive with this MCP config:")
    print(f'  "chroma": {{ "command": "{launcher}", "args": [')
    print(
        f'    "--client-type", "persistent", "--data-dir", "{cfg.chroma_dir}"],'
    )
    print('    "env": {"ANONYMIZED_TELEMETRY": "false"} }')
    return 0 if ok else 1


def cmd_setup(cfg: Config, args) -> int:
    """Open the Setup window, or run the same steps in this terminal."""
    if not getattr(args, "cli", False):
        script = Path(__file__).resolve().parents[1] / "bin" / "yarn-setup"
        if script.is_file():
            os.execv(str(script), [str(script)])
    from . import setup as yarn_setup

    return yarn_setup.run_setup()


def cmd_devices(cfg: Config, args) -> int:
    print(audio.list_avfoundation_devices())
    print(
        "Pick the index of your aggregate device (mic + BlackHole), then set\n"
        "audio.device_index in the config. To confirm the channel layout,\n"
        "record a few seconds and run:  ffprobe -v error -show_streams <file>"
    )
    return 0


def cmd_rec_config(cfg: Config, args) -> int:
    """Emit shell variables for bin/rec, so config lives in one place only."""
    print(f'MA_DEVICE_INDEX="{cfg.audio.device_index}"')
    print(f'MA_DEVICE_SELECTOR="{cfg.audio.device_name or cfg.audio.device_index}"')
    print(f'MA_SAMPLE_RATE="{cfg.audio.sample_rate}"')
    print(f'MA_MIC_REQUIRED_CHANNELS="{max(cfg.audio.mic_channels) + 1}"')
    print(
        f'MA_CALL_REQUIRED_CHANNELS="'
        f'{max(cfg.audio.mic_channels + cfg.audio.sys_channels) + 1}"'
    )
    print(f'MA_IN_PROGRESS="{cfg.in_progress_dir}"')
    print(f'MA_ROOT="{cfg.root}"')
    print(f'MA_LOGS="{cfg.logs_dir}"')
    # Derived from the running interpreter rather than from PATH: `ma` is
    # usually a symlink into the virtualenv, and the sibling of the symlink is
    # not where ma-capture lives.
    print(f'MA_CAPTURE_BIN="{Path(sys.executable).parent / "ma-capture"}"')
    print(
        f'MA_DIARIZE_SEGMENTATION_MODEL="'
        f'{Path(cfg.diarize.segmentation_model).expanduser()}"'
    )
    print(
        f'MA_DIARIZE_EMBEDDING_MODEL="'
        f'{Path(cfg.diarize.embedding_model).expanduser()}"'
    )
    return 0


def cmd_finalize(cfg: Config, args) -> int:
    manifest = pipeline.finalize(
        cfg,
        Path(args.raw),
        started_at=args.started_at or "",
        mode=args.mode,
        expected_duration_s=args.expected_duration,
    )
    # bin/rec reads this to know which meeting to open the popup for.
    print(manifest.meeting_id)
    return 0


def cmd_recover(cfg: Config, args) -> int:
    """Deal with a capture that finalize refused and left in in-progress."""
    if getattr(args, "discard_aborted", False):
        removed = pipeline.discard_aborted_starts(cfg)
        if not removed:
            print("no aborted starts to discard")
            return 0
        for name in removed:
            print(f"discarded aborted start {name}")
        return 0

    stranded = pipeline.stranded_captures(cfg)
    if not stranded:
        print("nothing stranded; every capture has been published or discarded")
        return 0

    aborted = {p.name for p in pipeline.aborted_start_captures(cfg)}

    if args.meeting_id:
        wanted = [p for p in stranded if p.name.startswith(args.meeting_id)]
        if not wanted:
            print(f"no stranded capture for {args.meeting_id}", file=sys.stderr)
            return 1
    else:
        wanted = stranded

    if not (args.publish or args.discard):
        for path in wanted:
            info = audio.probe(path)
            kind = "aborted start" if path.name in aborted else "unpublished"
            print(
                f"{path.name}  {info['duration']:.0f}s  "
                f"{path.stat().st_size / (1024 * 1024):.0f} MB  ({kind})"
            )
        if aborted and not args.meeting_id:
            print("\nshort leftovers are failed Start clicks, not lost meetings.")
            print("discard them with: ma recover --discard-aborted")
        print("\nchoose one: --publish (transcribe it anyway) or --discard")
        return 0

    for path in wanted:
        if args.discard:
            path.unlink()
            print(f"discarded {path.name}")
            continue
        # No expected duration: the wall-clock time this ran for is long gone,
        # so the length check cannot be repeated and must not be guessed.
        manifest = pipeline.finalize(cfg, path, mode=args.mode)
        print(f"published {manifest.meeting_id}")
    return 0


def cmd_notes(cfg: Config, args) -> int:
    cfg.ensure_dirs()
    records.write_notes(
        cfg.notes_json(args.meeting_id),
        meeting_id=args.meeting_id,
        notes=args.notes or "",
        attendees=args.attendees or "",
        applied=False,
    )
    print(f"saved notes for {args.meeting_id}")
    return 0


def cmd_watch(cfg: Config, args) -> int:
    watcher.run(cfg, interval=args.interval, once=False)
    return 0


def cmd_process(cfg: Config, args) -> int:
    transcriber = Transcriber(cfg.whisper)
    diarizer = Diarizer(cfg.diarize)
    summarizer = Summarizer(cfg.summary)
    store = MeetingStore(cfg.chroma_dir, cfg.collection)
    if args.meeting_id:
        manifest_path = cfg.inbox_dir / f"{args.meeting_id}.json"
        if not manifest_path.exists():
            print(f"no manifest in the inbox for {args.meeting_id}", file=sys.stderr)
            return 1
        pipeline.process(
            cfg,
            manifest_path,
            transcriber,
            store,
            diarizer,
            summarizer,
            force=True,
        )
        return 0
    count = watcher.sweep_inbox(
        cfg, transcriber, store, diarizer, summarizer, force=True
    )
    transcriber.unload()
    diarizer.unload()
    count += watcher.sweep_summaries(cfg, summarizer, store, force=True)
    from . import queue as queue_mod

    waiting = queue_mod.refresh(cfg)["remaining"]
    print(f"processed {count} meeting(s); {waiting} still waiting")
    return 0


def _tsv_field(value: str) -> str:
    """Collapse whitespace so a quote cannot break the speaker-popup TSV."""
    return " ".join(str(value or "").split())


def cmd_speakers(cfg: Config, args) -> int:
    """Show or name the diarized voices in one meeting.

    `--set key=name` is the same write the dialog performs, so naming a voice
    from the terminal and naming it from the popup are the same operation, and
    both cost one reindex rather than a re-transcription.
    """
    from . import speakers as speaker_memory

    if getattr(args, "meeting_banner", False):
        print(speaker_memory.meeting_banner(cfg, args.meeting_id))
        return 0

    if getattr(args, "play", None):
        try:
            path = speaker_memory.play_meeting_voice(
                cfg, args.meeting_id, args.play
            )
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"played {path.name}")
        return 0

    if getattr(args, "clip_path", None):
        try:
            path = speaker_memory.meeting_clip_path(
                cfg, args.meeting_id, args.clip_path, args.clip_index or 0
            )
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(path)
        return 0

    if getattr(args, "identify_json", False):
        if not cfg.transcript_json(args.meeting_id).exists():
            print(f"no transcript for {args.meeting_id}", file=sys.stderr)
            return 1
        print(json.dumps(speaker_memory.identify_payload(cfg, args.meeting_id)))
        return 0

    transcript_path = cfg.transcript_json(args.meeting_id)
    if not transcript_path.exists():
        print(f"no transcript for {args.meeting_id}", file=sys.stderr)
        return 1

    transcript = Transcript.read(transcript_path)
    notes_path = cfg.notes_json(args.meeting_id)
    notes = records.read_notes(notes_path)
    roster = records.speaker_roster(notes)

    if args.review_warning:
        attendees = [
            name.strip()
            for name in str(notes.get("attendees", "")).split(",")
            if name.strip()
        ]
        voices = len(transcript.speaker_profiles)
        if attendees and voices < len(attendees):
            print(
                f"You entered {len(attendees)} attendees "
                f"({', '.join(attendees)}), but the audio model detected only "
                f"{voices} distinct voice{'s' if voices != 1 else ''}. "
                "Review the detected voices next. Names will not be assigned "
                "automatically; anyone the model failed to separate will "
                "remain listed as an attendee rather than being attached to "
                "the wrong speech."
            )
        return 0

    if args.set:
        updates: dict[str, str] = {}
        titles: dict[str, str] = {}
        companies: dict[str, str] = {}
        distinct: set[str] = set()
        cleared: list[str] = []
        valid = {p.speaker for p in transcript.speaker_profiles}
        for item in args.set:
            key, _, name = item.partition("=")
            key, name = key.strip(), name.strip()
            if not key or not name:
                print(f"ignoring malformed --set {item!r}", file=sys.stderr)
                continue
            if key not in valid:
                print(
                    f"{key!r} is not a voice in this meeting "
                    f"(known: {', '.join(sorted(valid)) or 'none'})",
                    file=sys.stderr,
                )
                return 1
            if not speaker_memory.is_real_name(name):
                # "unsure" means "leave this voice unnamed", not a person.
                cleared.append(key)
                continue
            updates[key] = name
        for item in getattr(args, "title", None) or []:
            key, _, title = item.partition("=")
            key, title = key.strip(), title.strip()
            if key and title:
                titles[key] = title
        for item in getattr(args, "company", None) or []:
            key, _, company = item.partition("=")
            key, company = key.strip(), company.strip()
            if key and company:
                companies[key] = company
        for key in getattr(args, "distinct", None) or []:
            if key.strip():
                distinct.add(key.strip())
        if cleared:
            records.clear_speaker_names(notes_path, cleared)
            roster = {k: v for k, v in roster.items() if k not in cleared}
            for key in cleared:
                print(f"{key} left unnamed")
        if not updates:
            return 0 if cleared else 1
        leftover = speaker_memory.leftover_assignment(
            transcript.speaker_profiles,
            {**roster, **updates},
            records.listed_speakers(notes),
            cfg.names.own_names,
        )
        if leftover and leftover[0] not in updates:
            updates[leftover[0]] = leftover[1]
            assumed = leftover
        else:
            assumed = None
        cfg.ensure_dirs()
        records.write_notes(
            notes_path,
            meeting_id=args.meeting_id,
            speakers=updates,
            applied=False,
        )
        try:
            for key, name in updates.items():
                clip = ""
                embedding: list[float] = []
                profile = next(
                    (p for p in transcript.speaker_profiles if p.speaker == key),
                    None,
                )
                if profile is not None:
                    clip, embedding = speaker_memory.capture_identity(
                        cfg, transcript, profile, name
                    )
                speaker_memory.remember_confirmation(
                    cfg,
                    name=name,
                    meeting_id=args.meeting_id,
                    embedding=embedding,
                    clip=clip,
                    title=titles.get(key, ""),
                    company=companies.get(key, ""),
                    distinct=key in distinct,
                )
        except Exception:
            logging.getLogger("meeting_archive").exception(
                "could not update speaker library for %s", args.meeting_id
            )
        display = models.speaker_display_indices(transcript.speaker_profiles)
        for key, name in sorted(updates.items(), key=lambda item: display.get(item[0], 0)):
            tag = " (assumed last remaining name)" if assumed and key == assumed[0] else ""
            index = display.get(key) or models.speaker_index(key)
            who = f"Speaker {index}" if index else key
            print(f"{who} -> {name}{tag}")
        if assumed:
            print(f"ASSUMED\t{assumed[0]}\t{assumed[1]}")
        print("run `ma ingest` or let the watcher pick it up to relabel the transcript")
        return 0

    profiles = transcript.speaker_profiles
    if not profiles:
        if transcript.diarize_error:
            print(f"no voices: {transcript.diarize_error}")
        else:
            print("no diarized voices in this meeting")
        return 0

    display = models.speaker_display_indices(transcript.speaker_profiles)
    if args.pending_tsv:
        # Machine-readable, for bin/speaker-popup. Tabs are safe: quips are
        # whitespace-collapsed when they are built.
        for profile in transcript.unnamed_speakers(roster):
            index = display.get(profile.speaker) or models.speaker_index(profile.speaker) or 0
            detail = (
                f"spoke {models.format_duration(profile.speaking_s)} "
                f"across {profile.turns} turn(s), from "
                f"{models.format_timestamp(profile.first_at)}"
            )
            guess = speaker_memory.usable_dialog_guess(
                profile.guess,
                attendees=records.listed_speakers(notes),
                known=[person.name for person in speaker_memory.load_library(cfg)],
            )
            print(
                "\t".join(
                    [
                        profile.speaker,
                        f"Speaker {index}",
                        detail,
                        _tsv_field(profile.quip),
                        _tsv_field(guess),
                        _tsv_field(speaker_memory.identity_clips(transcript, profile, limit=1)[0].text),
                    ]
                )
            )
        return 0

    known = ", ".join(s.name for s in speaker_memory.load_library(cfg))
    if known:
        print(f"known speakers: {known}")
    print(f"{len(profiles)} voice(s) in {args.meeting_id}:")
    for profile in profiles:
        index = display.get(profile.speaker) or models.speaker_index(profile.speaker) or 0
        named = roster.get(profile.speaker, "")
        who = named or f"Speaker {index} (unidentified)"
        print(
            f"  [{profile.speaker}] {who} -- "
            f"{models.format_duration(profile.speaking_s)}, {profile.turns} turn(s), "
            f"from {models.format_timestamp(profile.first_at)}"
        )
        if profile.quip:
            print(f'      said: "{profile.quip}"')
        if profile.guess and not named:
            print(f"      suggested name: {profile.guess}")
    for guess in transcript.speaker_guesses:
        print(
            f"  guess {guess.speaker} = {guess.name} "
            f"({guess.basis}, confidence {guess.confidence:.2f}): {guess.evidence}"
        )
    return 0


def cmd_voices(cfg: Config, args) -> int:
    from . import speakers as speaker_memory

    if args.forget:
        if not speaker_memory.forget_speaker(cfg, args.forget):
            print(f"no known speaker named {args.forget!r}", file=sys.stderr)
            return 1
        print(f"forgot {args.forget}")
        return 0
    if args.play:
        try:
            path = speaker_memory.play_known_voice(cfg, args.play)
        except FileNotFoundError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"played {path.name}")
        return 0
    people = speaker_memory.load_library(cfg)
    if args.names:
        print(", ".join(p.name for p in people))
        return 0
    if args.html:
        path = speaker_memory.render_voices_html(cfg, people)
        print(path)
        return 0
    if not people:
        print("no known speakers yet")
        return 0
    for person in people:
        clip = "clip" if person.clip else "no-clip"
        print(
            f"{person.name}  x{person.confirmation_count}  "
            f"{person.last_seen or 'not seen'}  {clip}"
        )
    return 0


def cmd_summarize(cfg: Config, args) -> int:
    """Fill summaries for meetings that were processed while Ollama was off,
    the lid closed, or evening-batch was waiting.

    Ignores the summarise-when schedule: this is the explicit button.
    Does not start Ollama. Open the app from the menu first, then run this.
    """
    from datetime import date

    summarizer = Summarizer(cfg.summary)
    if not summarizer.is_running():
        print(
            "Ollama is not running. Choose Open Ollama from the menu, wait "
            "until the app is up, then try again.",
            file=sys.stderr,
        )
        return 1

    store = MeetingStore(cfg.chroma_dir, cfg.collection)
    today = date.today().isoformat()
    done = 0
    skipped = 0
    failed = 0
    for path in sorted(cfg.transcripts_dir.rglob("*.json")):
        meeting_id = path.stem
        if args.today and not meeting_id.startswith(today):
            continue
        try:
            transcript = Transcript.read(path)
        except (OSError, ValueError):
            continue
        if not pipeline.needs_summary(transcript):
            skipped += 1
            continue
        try:
            if pipeline.apply_summary(cfg, meeting_id, summarizer, store):
                print(f"summarized {meeting_id}")
                done += 1
            else:
                skipped += 1
        except Exception as exc:
            failed += 1
            print(f"  failed {meeting_id}: {exc}", file=sys.stderr)
    print(f"summarized {done} meeting(s), {skipped} already done, {failed} failed")
    return 1 if failed else 0


def cmd_ingest(cfg: Config, args) -> int:
    store = MeetingStore(cfg.chroma_dir, cfg.collection)
    n = pipeline.ingest(cfg, args.meeting_id, store)
    print(f"indexed {n} chunk(s) for {args.meeting_id}")
    return 0


def cmd_rediarize(cfg: Config, args) -> int:
    diarizer = Diarizer(cfg.diarize)
    store = MeetingStore(cfg.chroma_dir, cfg.collection)
    summarizer = Summarizer(cfg.summary)
    transcript = pipeline.rediarize(
        cfg,
        args.meeting_id,
        diarizer,
        store,
        summarizer,
    )
    print(
        f"rediarized {args.meeting_id}: "
        f"{len(transcript.speaker_profiles)} voice(s)"
    )
    return 0


def cmd_rebuild(cfg: Config, args) -> int:
    """Reindex everything from the transcripts on disk.

    This is the escape hatch that makes the embedding choice reversible: point
    the config at a different model, run this, and the whole archive is
    re-embedded without needing a single audio file back.
    """
    store = MeetingStore(cfg.chroma_dir, cfg.collection)
    paths = sorted(cfg.transcripts_dir.rglob("*.json"))
    # Chroma is derived state. Clear it first so meetings whose transcript was
    # deliberately removed cannot survive as stale search results.
    store.clear()
    total_chunks = 0
    failures = 0
    for path in paths:
        meeting_id = path.stem
        try:
            total_chunks += pipeline.ingest(cfg, meeting_id, store)
        except Exception as exc:
            failures += 1
            print(f"  failed {meeting_id}: {exc}", file=sys.stderr)
    print(f"reindexed {len(paths) - failures}/{len(paths)} meeting(s), {total_chunks} chunks")
    return 1 if failures else 0


def cmd_prune(cfg: Config, args) -> int:
    result = retention.prune(cfg, dry_run=args.dry_run)
    print(("dry run -- " if args.dry_run else "") + result.summary())
    for name in result.kept_no_transcript:
        print(f"  kept (no transcript): {name}")
    for name in result.kept_override:
        print(f"  kept (keep_audio): {name}")
    return 0


def cmd_queue(cfg: Config, args) -> int:
    if getattr(args, "reconcile", False):
        repaired = jobs.reconcile(cfg)
        if repaired:
            for meeting_id in repaired:
                print(f"{meeting_id}  queued -> complete (transcript already on disk)")
            print(f"repaired {len(repaired)} stale job record(s)")
        else:
            print("nothing to reconcile; every queued job is genuinely outstanding")
        return 0

    items = jobs.list_jobs(cfg)
    if not items:
        print("queue is empty")
        return 0
    for job in items:
        print(
            f"{job.meeting_id}  {job.state:<10}  {job.current_stage:<12}  "
            f"{job.source}  {job.error}".rstrip()
        )
    return 0


def cmd_pause(cfg: Config, args) -> int:
    jobs.set_paused(cfg, True, reason="manual")
    print("processing paused; recording is unaffected")
    return 0


def cmd_resume(cfg: Config, args) -> int:
    jobs.set_paused(cfg, False)
    print("processing resumed")
    return 0


def cmd_import_audio(cfg: Config, args) -> int:
    from .imports import ImportFailed, import_audio

    try:
        manifest = import_audio(cfg, args.path, title=args.title)
    except ImportFailed as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"queued {manifest.meeting_id} ({manifest.duration_s:.1f}s)")
    return 0


def cmd_import_transcript(cfg: Config, args) -> int:
    from .imports import ImportFailed, import_transcript

    try:
        store = MeetingStore(cfg.chroma_dir, cfg.collection)
        transcript = import_transcript(cfg, args.path, title=args.title, store=store)
    except ImportFailed as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"imported {transcript.meeting_id} ({len(transcript.turns)} turn(s))")
    return 0


def cmd_transcripts(cfg: Config, args) -> int:
    from . import library

    path = library.render_transcript_index(cfg)
    print(path)
    if getattr(args, "open", False):
        import webbrowser

        webbrowser.open(path.as_uri())
    return 0


def cmd_library(cfg: Config, args) -> int:
    from . import library

    rows = library.list_meetings(cfg)
    if args.html:
        path = library.render_html(cfg, rows)
        print(path)
        return 0
    if not rows:
        print("no meetings")
        return 0
    for row in rows:
        mins = int(row.duration_s // 60)
        secs = int(row.duration_s % 60)
        print(
            f"{row.date}  {row.state:<10}  {mins}m{secs:02d}s  "
            f"{row.title}  speakers={row.speakers}  "
            f"{'summary' if row.has_summary else 'no-summary'}"
        )
    return 0


def cmd_keep_audio(cfg: Config, args) -> int:
    retention.set_keep_audio(cfg, args.meeting_id, True)
    print(f"{args.meeting_id}: keep_audio on")
    return 0


def cmd_delete_audio(cfg: Config, args) -> int:
    try:
        removed = retention.delete_meeting_audio(cfg, args.meeting_id)
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"deleted {len(removed)} audio file(s)")
    return 0


def cmd_setting(cfg: Config, args) -> int:
    raw = args.value.strip()
    lowered = raw.lower()
    if args.key in {
        "recognise_known",
        "cleanup_enabled",
        "pause_on_pressure",
        "brief_on_login",
    }:
        if lowered not in {"on", "off", "true", "false", "1", "0"}:
            print("value must be on or off", file=sys.stderr)
            return 1
        value: bool | str = lowered in {"on", "true", "1"}
    elif args.key == "summarize_when":
        if lowered not in {"after_meeting", "evening", "manual"}:
            print(
                "value must be after_meeting, evening, or manual",
                file=sys.stderr,
            )
            return 1
        value = lowered
    elif args.key == "summary_model":
        from .resources import (
            SUMMARY_MODEL_FULL,
            can_run_accurate,
            model_for_quality,
        )

        value = model_for_quality(lowered)
        if value == SUMMARY_MODEL_FULL and not can_run_accurate():
            print("this Mac can only run Fast summaries", file=sys.stderr)
            return 1
    else:
        value = raw
    write_setting(cfg, args.key, value)
    if args.key == "brief_on_login":
        from . import brief as brief_mod

        ma_bin = invoked_ma()
        if value:
            if not Path(ma_bin).is_file():
                _tell(
                    f"Open brief at login is on, but Yarn could not find {ma_bin} "
                    "to run it at login.",
                    alert=True,
                )
                return 1
            path = brief_mod.install_login_agent(cfg, ma_bin)
            print(f"login brief enabled ({path})")
        else:
            brief_mod.uninstall_login_agent()
            print("login brief disabled")
    print(f"{args.key} = {value}")
    return 0


def cmd_brief(cfg: Config, args) -> int:
    """Write a read-only daily HTML brief from recent meetings."""
    from . import brief as brief_mod
    from .summarize import SummaryUnavailable

    # The menu runs this with the terminal hidden, and a brief takes about a
    # minute. Without a note on screen the click looks like it did nothing.
    if not args.login:
        _tell("Writing today's brief on this Mac. It usually takes about a minute.")
    try:
        result = brief_mod.generate_brief(
            cfg,
            open_after=bool(args.open),
            login=bool(args.login),
            force=bool(args.force),
        )
    except SummaryUnavailable as exc:
        if args.login and "on login is off" in str(exc).lower():
            return 0
        print(exc, file=sys.stderr)
        if not args.login:
            _tell(str(exc), alert=True)
        return 1
    if result.path is None:
        return 0
    if result.overview == "already written":
        print(f"brief already exists: {result.path}")
        if args.open:
            brief_mod._open_path(result.path)  # noqa: SLF001
        return 0
    print(f"wrote {result.path}")
    return 0


def cmd_ask(cfg: Config, args) -> int:
    """Ask the local model about meetings. No Claude or ChatGPT."""
    from .ask import AskError, ask, open_answer, request_chat
    from .summarize import SummaryUnavailable

    question = " ".join(args.question or []).strip()
    if not question:
        request_chat(cfg)
        return 0
    # The Dock chat calls --text and shows the reply itself. A bare ask still
    # opens the page. Neither path uses AppleScript, which was launching
    # Script Editor just to collect one sentence.
    if not args.text:
        _tell("Looking through your meetings.")
    try:
        result = ask(cfg, question, remember=not args.text)
    except (AskError, SummaryUnavailable) as exc:
        print(exc, file=sys.stderr)
        if not args.text:
            _tell(str(exc), alert=True)
        return 1
    print(result.answer)
    if args.text:
        return 0
    print(result.path)
    open_answer(result.path)
    return 0


def cmd_connect(cfg: Config, args) -> int:
    """Point a desktop chat app at the local meeting search index."""
    from .connect import ConnectError, connect_target

    try:
        text = connect_target(cfg, args.target, notify=_tell)
    except (OSError, ConnectError) as exc:
        print(exc, file=sys.stderr)
        _tell(str(exc), alert=True)
        return 1
    print(text)
    _tell(text, alert=True)
    return 0


def cmd_benchmark(cfg: Config, args) -> int:
    from .benchmark import run_benchmark

    return run_benchmark(cfg)


def cmd_search(cfg: Config, args) -> int:
    store = MeetingStore(cfg.chroma_dir, cfg.collection)
    hits = store.search(args.query, n_results=args.limit)
    if not hits:
        print("no results")
        return 0
    for hit in hits:
        meta = hit["metadata"]
        stamp = format_timestamp(float(meta.get("start_s") or 0))
        print(f"--- {meta.get('date_label', meta.get('meeting_id'))} @ {stamp}")
        if meta.get("attendees"):
            print(f"    attendees: {meta['attendees']}")
        body = hit["document"].strip().replace("\n", "\n    ")
        print(f"    {body[:600]}")
        print()
    return 0


def cmd_names(cfg: Config, args) -> int:
    path = cfg.transcript_json(args.meeting_id)
    if not path.exists():
        print(f"no transcript for {args.meeting_id}", file=sys.stderr)
        return 1
    transcript = Transcript.read(path)
    if not transcript.detected_names:
        print("no names detected")
        return 0
    for candidate in transcript.detected_names:
        print(
            f"{candidate.name:<15} x{candidate.count:<3} "
            f"{candidate.attributed_to:<6} {','.join(sorted(candidate.patterns))}"
        )
        print(f"                {candidate.evidence}")
    return 0


def cmd_status(cfg: Config, args) -> int:
    transcripts = sorted(cfg.transcripts_dir.rglob("*.json"))
    print(f"root:        {cfg.root}")
    print(f"transcripts: {len(transcripts)}")
    print(f"inbox:       {len(list(cfg.inbox_dir.glob('*.json')))} waiting")
    queued = jobs.queued(cfg)
    print(f"queue:       {len(queued)} job(s)")
    for job in queued[:5]:
        print(f"  {job.meeting_id}  {job.state}  {job.current_stage}")
    print(f"failed:      {len(list(cfg.failed_dir.glob('*.json')))}")
    # A raw capture left here while nothing is recording is a meeting that was
    # never published: finalize refused it, or the recorder died. It would
    # otherwise sit there unmentioned, which is how two meetings were lost.
    stranded = pipeline.stranded_captures(cfg)
    aborted = {p.name for p in pipeline.aborted_start_captures(cfg)}
    if stranded:
        print(f"stranded:    {len(stranded)} unpublished capture(s)")
        for path in stranded:
            kind = "aborted start" if path.name in aborted else "unpublished"
            print(f"  {path.name}  ({kind}; ma recover {path.name.split('__')[0]})")
        if aborted:
            print("  discard short leftovers: ma recover --discard-aborted")
    audio_files = [p for p in cfg.audio_archive_dir.glob("*") if p.is_file()]
    size_mb = sum(p.stat().st_size for p in audio_files) / (1024 * 1024)
    print(f"audio kept:  {len(audio_files)} file(s), {size_mb:.1f} MB "
          f"(retention {cfg.audio_retention})")
    try:
        store = MeetingStore(cfg.chroma_dir, cfg.collection)
        print(f"indexed:     {store.count()} chunks")
    except Exception as exc:
        print(f"indexed:     unavailable ({exc})")
    for path in transcripts[-5:]:
        print(f"  {path.stem}")
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ma",
        description="Local meeting transcription and search archive.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="install Yarn on this Mac")
    p.add_argument(
        "--cli",
        action="store_true",
        help="run the installer in this terminal instead of a window",
    )
    p.set_defaults(func=cmd_setup)

    sub.add_parser("doctor", help="check the setup").set_defaults(func=cmd_doctor)
    sub.add_parser("devices", help="list audio devices").set_defaults(func=cmd_devices)
    sub.add_parser("rec-config", help="shell vars for bin/rec").set_defaults(
        func=cmd_rec_config
    )

    p = sub.add_parser("finalize", help="split channels and publish to the inbox")
    p.add_argument("raw", help="path to the raw multi-channel capture")
    p.add_argument("--started-at", default="")
    p.add_argument(
        "--expected-duration",
        type=float,
        default=0.0,
        help="elapsed wall time; reject a capture whose audio clock disagrees",
    )
    p.add_argument(
        "--mode",
        choices=["call", "room"],
        default="room",
        help="call: mic is Jacob, system audio is everyone else. "
             "room: one mic, several unidentified people. Default is room.",
    )
    p.set_defaults(func=cmd_finalize)

    p = sub.add_parser("recover", help="publish or discard a stranded capture")
    p.add_argument("meeting_id", nargs="?", help="default: every stranded capture")
    p.add_argument("--publish", action="store_true",
                   help="transcribe it, skipping the length check")
    p.add_argument("--discard", action="store_true", help="delete the raw audio")
    p.add_argument(
        "--discard-aborted",
        action="store_true",
        help="delete short leftovers from a failed Start click",
    )
    p.add_argument("--mode", choices=("call", "room"), default="room")
    p.set_defaults(func=cmd_recover)

    p = sub.add_parser("notes", help="save notes for a meeting")
    p.add_argument("meeting_id")
    p.add_argument("--notes", default="")
    p.add_argument("--attendees", default="")
    p.set_defaults(func=cmd_notes)

    p = sub.add_parser("watch", help="run the processor continuously")
    p.add_argument("--interval", type=float, default=5.0)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser(
        "process",
        help="process waiting meetings now, ignoring the schedule",
    )
    p.add_argument("meeting_id", nargs="?")
    p.set_defaults(func=cmd_process)

    p = sub.add_parser(
        "summarize",
        help="fill deferred summaries once Ollama is open",
    )
    p.add_argument(
        "--today",
        action="store_true",
        help="only meetings whose id starts with today's date",
    )
    p.set_defaults(func=cmd_summarize)

    p = sub.add_parser("ingest", help="reindex one meeting")
    p.add_argument("meeting_id")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser(
        "rediarize",
        help="rebuild speaker labels from retained audio without rerunning Whisper",
    )
    p.add_argument("meeting_id")
    p.set_defaults(func=cmd_rediarize)

    sub.add_parser("rebuild", help="reindex every transcript").set_defaults(
        func=cmd_rebuild
    )

    p = sub.add_parser("prune", help="delete archived audio past retention")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("search", help="search the archive")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("speakers", help="show or name the diarized voices")
    p.add_argument("meeting_id")
    p.add_argument(
        "--set",
        action="append",
        metavar="KEY=NAME",
        help="name a voice, e.g. --set room-2=Marina (repeatable)",
    )
    p.add_argument(
        "--title",
        action="append",
        metavar="KEY=TITLE",
        help="optional job title for a voice named in the same command",
    )
    p.add_argument(
        "--company",
        action="append",
        metavar="KEY=COMPANY",
        help="optional company for a voice named in the same command",
    )
    p.add_argument(
        "--distinct",
        action="append",
        metavar="KEY",
        help="this voice shares a first name with someone else",
    )
    p.add_argument(
        "--pending-tsv",
        action="store_true",
        help="tab-separated unidentified voices, for bin/speaker-popup",
    )
    p.add_argument(
        "--review-warning",
        action="store_true",
        help="describe an attendee/voice count mismatch for the speaker dialog",
    )
    p.add_argument(
        "--meeting-banner",
        action="store_true",
        help="print date, title and notes for the identification dialog",
    )
    p.add_argument(
        "--play",
        metavar="KEY",
        help="play a short clip of one diarized voice, e.g. room-2",
    )
    p.add_argument(
        "--clip-path",
        metavar="KEY",
        help="extract one single-voice clip and print its path (for the identify window)",
    )
    p.add_argument("--clip-index", type=int, default=0, help="which clip, 0 = best")
    p.add_argument(
        "--identify-json",
        action="store_true",
        help="voices, clips and predicted names as JSON, for the identify window",
    )
    p.set_defaults(func=cmd_speakers)

    p = sub.add_parser("voices", help="show or review the known-speaker library")
    p.add_argument("--html", action="store_true")
    p.add_argument("--names", action="store_true")
    p.add_argument("--play", metavar="NAME")
    p.add_argument("--forget", metavar="NAME")
    p.set_defaults(func=cmd_voices)

    p = sub.add_parser("names", help="show detected names for a meeting")
    p.add_argument("meeting_id")
    p.set_defaults(func=cmd_names)

    sub.add_parser("status", help="archive summary").set_defaults(func=cmd_status)

    p = sub.add_parser("queue", help="show the processing queue")
    p.add_argument(
        "--reconcile",
        action="store_true",
        help="mark queued jobs complete when the transcript is already on disk",
    )
    p.set_defaults(func=cmd_queue)
    sub.add_parser("pause", help="pause background processing").set_defaults(
        func=cmd_pause
    )
    sub.add_parser("resume", help="resume background processing").set_defaults(
        func=cmd_resume
    )

    p = sub.add_parser("import-audio", help="import a recording into the archive")
    p.add_argument("path", type=Path)
    p.add_argument("--title", default="")
    p.set_defaults(func=cmd_import_audio)

    p = sub.add_parser(
        "import-transcript", help="import a coworker transcript without Whisper"
    )
    p.add_argument("path", type=Path)
    p.add_argument("--title", default="")
    p.set_defaults(func=cmd_import_transcript)

    p = sub.add_parser("transcripts", help="open the transcript pages")
    p.add_argument("--html", action="store_true", help="write the transcript index")
    p.add_argument("--open", action="store_true", help="open the index in the browser")
    p.set_defaults(func=cmd_transcripts)

    p = sub.add_parser("library", help="list meetings in the archive")
    p.add_argument("--html", action="store_true")
    p.set_defaults(func=cmd_library)

    p = sub.add_parser("keep-audio", help="exempt a meeting from audio pruning")
    p.add_argument("meeting_id")
    p.set_defaults(func=cmd_keep_audio)

    p = sub.add_parser(
        "delete-audio", help="delete archived audio for a transcribed meeting"
    )
    p.add_argument("meeting_id")
    p.set_defaults(func=cmd_delete_audio)

    p = sub.add_parser("setting", help="write a machine-local Yarn setting")
    p.add_argument(
        "key",
        choices=(
            "recognise_known",
            "speaker_engine",
            "audio_retention",
            "cleanup_enabled",
            "pause_on_pressure",
            "summarize_when",
            "brief_on_login",
            "summary_model",
        ),
    )
    p.add_argument("value")
    p.set_defaults(func=cmd_setting)

    p = sub.add_parser(
        "brief",
        help="write a read-only daily HTML brief from recent meetings",
    )
    p.add_argument(
        "--open",
        action="store_true",
        help="open the brief in the browser when done",
    )
    p.add_argument(
        "--login",
        action="store_true",
        help="login-agent mode: no-op unless brief_on_login is on",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="rewrite today's brief even if it already exists",
    )
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser(
        "ask",
        help="ask Ollama about meetings on this Mac",
    )
    p.add_argument("question", nargs="*", help="question")
    p.add_argument(
        "--text",
        action="store_true",
        help="print the reply only, for the Yarn chat window",
    )
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser(
        "connect",
        help="let Claude, Cursor, or ChatGPT search this Mac's meetings",
    )
    p.add_argument("target", choices=("claude", "ollama", "cursor", "chatgpt"))
    p.set_defaults(func=cmd_connect)

    sub.add_parser(
        "benchmark", help="time local benchmark_audio/ files if present"
    ).set_defaults(func=cmd_benchmark)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    # rec-config is eval'd by the shell, so it must emit nothing but variables.
    if args.command != "rec-config":
        setup_logging(cfg, args.verbose)
    try:
        return args.func(cfg, args)
    except (audio.AudioError, FileNotFoundError) as exc:
        # Things to tell Jacob about, not bugs to show a traceback for. A
        # refused recording is the important one: bin/rec puts the last line of
        # this on screen, and a traceback there would say nothing useful.
        logging.getLogger("meeting_archive").error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
