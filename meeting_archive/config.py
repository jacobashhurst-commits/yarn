"""Configuration loading.

Config lives at ~/.config/meeting-archive/config.toml by default, overridable
with the MEETING_ARCHIVE_CONFIG env var. Everything has a default, so the tool
runs with an empty config file except for the audio device indices, which are
machine-specific and must be set (see `ma devices`).
"""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .glossary import GlossaryConfig
from .models import speaker_index

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "meeting-archive" / "config.toml"
DEFAULT_ROOT = Path.home() / "meeting-archive"


@dataclass
class AudioConfig:
    # Index of the macOS aggregate device, from `ma devices`.
    device_index: int = -1
    # Prefer the stable device name. AVFoundation indices change whenever
    # hardware such as AirPods connects or disconnects.
    device_name: str = ""
    # Which channels of that device belong to whom. Channel indices are
    # 0-based and refer to the aggregate device's channel layout.
    mic_channels: list[int] = field(default_factory=lambda: [0])
    sys_channels: list[int] = field(default_factory=lambda: [1, 2])
    sample_rate: int = 16000


@dataclass
class WhisperConfig:
    model: str = "large-v3-turbo"
    # whisper.cpp uses Metal on Apple Silicon. faster-whisper remains available
    # for CUDA servers and as a compatibility fallback.
    engine: str = "whisper_cpp"
    command: str = "/opt/homebrew/bin/whisper-cli"
    model_path: str = (
        "~/yarn/meeting-archive/models/whisper-cpp/ggml-model.bin"
    )
    num_threads: int = 8
    device: str = "cpu"  # "cpu", "cuda", or "auto"
    compute_type: str = "int8"
    # Model weights are installed once, then normal processing never checks a
    # model host or sends audio over the network.
    local_files_only: bool = True
    language: str | None = "en"
    beam_size: int = 5
    vad_filter: bool = True
    # Whisper's default VAD only strips silence longer than 2s. The "everyone
    # else" channel is mostly silence whenever Jacob is talking, and long
    # silences are where Whisper hallucinates, so be more aggressive.
    vad_min_silence_ms: int = 700
    # Suppresses the repeat-loop failure mode on sparse channels.
    condition_on_previous_text: bool = False
    # Biases whisper.cpp toward product and company names you say often.
    initial_prompt: str = ""


@dataclass
class ChunkConfig:
    target_chars: int = 1200
    overlap_turns: int = 1
    max_turn_chars: int = 1500
    coalesce_gap_s: float = 2.0
    max_turn_s: float = 90.0
    # Prefix each chunk's embedded text with a one-line meeting header
    # (date, attendees, notes). Costs a little embedding precision, buys a lot
    # of recall on queries like "what did John say about pricing".
    include_context_header: bool = True


@dataclass
class DiarizeConfig:
    enabled: bool = True
    model: str = "sherpa-onnx-pyannote-segmentation-3.0"
    segmentation_model: str = (
        "~/meeting-archive/models/diarization/"
        "sherpa-onnx-pyannote-segmentation-3-0/model.onnx"
    )
    embedding_model: str = (
        "~/meeting-archive/models/diarization/speaker-embedding.onnx"
    )
    num_threads: int = 8
    cluster_threshold: float = 0.9
    # The mic channel is absent on purpose: it is one known person, and
    # splitting it could only ever invent a second Jacob.
    diarize_channels: list[str] = field(default_factory=lambda: ["sys", "room"])
    # 0 means "let the model decide", which is the honest default when the
    # number of people on a call genuinely isn't known in advance.
    min_speakers: int = 0
    max_speakers: int = 0
    # Spans shorter than this are dropped before speakers are numbered, so a
    # crosstalk blip can't take the "Speaker 1" slot from whoever opened the
    # meeting.
    min_span_s: float = 0.6
    # Ask who each unidentified voice was, once the transcript exists.
    identify_popup: bool = True
    # pyannote is the default and still experimental. sherpa is the stable
    # fallback. compare runs both.
    engine: str = "pyannote"


@dataclass
class CleanupConfig:
    enabled: bool = False


@dataclass
class SpeakersMemoryConfig:
    recognise_known: bool = False
    # Cosine floor for suggesting a known speaker by voice. The original 0.75
    # was aspirational and unreachable, which made the whole feature dead
    # code: measured against real cross-meeting audio with this 512-d sherpa
    # extractor on a 3.5s snippet, genuine same-speaker scores came in at
    # 0.407 / 0.503 / 0.528, and the highest score ever seen against
    # known-wrong audio was 0.546. Anything at or above 0.6 never fires.
    min_confidence: float = 0.35
    # A near-tie is not an identification. The winner must beat the runner-up
    # by this much, or no name is offered at all. Genuine margins measured:
    # 0.114 / 0.238 / 0.356. This matters far more once the floor is low
    # enough to be reachable.
    min_margin: float = 0.08


@dataclass
class ProcessingConfig:
    # When a recording is live and the machine is under memory pressure, wait
    # to start Whisper/Ollama. An in-flight stage still finishes.
    pause_on_pressure: bool = True


@dataclass
class NamesConfig:
    enabled: bool = True
    # Jacob's own name(s), so self-introductions and people addressing him
    # don't get suggested as attendees.
    own_names: list[str] = field(default_factory=lambda: ["Jacob", "Jake"])
    # Show a follow-up popup with detected names when the on-stop popup was
    # dismissed without an attendee list.
    suggest_popup: bool = True
    min_count: int = 1


@dataclass
class SummaryConfig:
    # Summaries are generated by an already-installed Ollama model. The
    # summarizer verifies the model exists locally before invoking it, so a
    # typo can never trigger an implicit download.
    enabled: bool = True
    command: str = "/usr/local/bin/ollama"
    # Used only to detect whether Ollama is already running. `ollama list`
    # would start the daemon; a short HTTP probe will not.
    host: str = "http://127.0.0.1:11434"
    model: str = "qwen3.5:9b"
    timeout_s: int = 900
    # Longer transcripts are summarized in local chunks, then combined.
    # 12k keeps a 9B model inside 8 GB unified memory; 40k was one prompt
    # that timed out on an 18-minute meeting.
    chunk_chars: int = 12000
    # default | sales | technical | short | actions | detailed
    profile: str = "default"
    # after_meeting: try once the transcript exists (and retry after sleep).
    # evening: hold summaries until evening_hour, or the next day leftover.
    # manual: only the menu button / `ma summarize`.
    when: str = "after_meeting"
    evening_hour: int = 18


@dataclass
class Config:
    root: Path = DEFAULT_ROOT
    own_label: str = "Jacob"
    other_label: str = "Other"
    # In-room recordings have one mic and several speakers. "Unknown" is the
    # honest label; anything else implies knowledge we don't have.
    room_label: str = "Unknown"
    collection: str = "meetings"
    audio_retention_days: int = 7
    audio_retention: str = "1_week"
    audio: AudioConfig = field(default_factory=AudioConfig)
    whisper: WhisperConfig = field(default_factory=WhisperConfig)
    diarize: DiarizeConfig = field(default_factory=DiarizeConfig)
    chunk: ChunkConfig = field(default_factory=ChunkConfig)
    names: NamesConfig = field(default_factory=NamesConfig)
    summary: SummaryConfig = field(default_factory=SummaryConfig)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    speakers_memory: SpeakersMemoryConfig = field(default_factory=SpeakersMemoryConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    glossary: GlossaryConfig = field(default_factory=GlossaryConfig)

    # --- derived paths ---
    @property
    def in_progress_dir(self) -> Path:
        return self.root / "recordings" / "in-progress"

    @property
    def inbox_dir(self) -> Path:
        return self.root / "recordings" / "inbox"

    @property
    def processing_dir(self) -> Path:
        return self.root / "recordings" / "processing"

    @property
    def audio_archive_dir(self) -> Path:
        return self.root / "recordings" / "archive"

    @property
    def failed_dir(self) -> Path:
        return self.root / "recordings" / "failed"

    @property
    def transcripts_dir(self) -> Path:
        return self.root / "transcripts"

    @property
    def notes_dir(self) -> Path:
        return self.root / "notes"

    @property
    def chroma_dir(self) -> Path:
        return self.root / "chroma"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def jobs_dir(self) -> Path:
        return self.root / "jobs"

    @property
    def processing_status(self) -> Path:
        return self.root / ".processing-status.json"

    @property
    def briefs_dir(self) -> Path:
        return self.root / "briefs"

    def all_dirs(self) -> list[Path]:
        return [
            self.in_progress_dir,
            self.inbox_dir,
            self.processing_dir,
            self.audio_archive_dir,
            self.failed_dir,
            self.transcripts_dir,
            self.notes_dir,
            self.chroma_dir,
            self.logs_dir,
            self.briefs_dir,
            self.jobs_dir,
        ]

    def ensure_dirs(self) -> None:
        for d in self.all_dirs():
            d.mkdir(parents=True, exist_ok=True)

    def label(self, channel: str) -> str:
        return {
            "mic": self.own_label,
            "sys": self.other_label,
            "room": self.room_label,
        }.get(channel, channel)

    def speaker_label(
        self,
        channel: str,
        speaker: str | None,
        roster: dict | None = None,
        display: dict[str, int] | None = None,
    ) -> str:
        """Human-facing label for one turn.

        This is where "never label speech with a speaker you can't identify"
        is actually enforced, so the precedence matters:

          1. A name Jacob confirmed for this voice. Only he can put one here.
          2. "Speaker N" when the voice was distinguished but not named. True
             and useful: the transcript shows three people without claiming to
             know which three.
          3. The channel label when there is no speaker at all -- Jacob on the
             mic, "Other"/"Unknown" otherwise, exactly as before diarization.

        No step infers a name from the transcript. Inference produces a
        *suggestion* for the dialog; it never reaches this function.
        """
        if speaker:
            named = (roster or {}).get(speaker)
            if named and str(named).strip():
                return str(named).strip()
            index = (display or {}).get(speaker)
            if index is None:
                index = speaker_index(speaker)
            if index is not None:
                # The mic is one person, so a numbered mic voice would be a
                # bug upstream. Fall back to his name rather than "Speaker 1".
                if channel == "mic":
                    return self.own_label
                return f"Speaker {index}"
        return self.label(channel)

    def turn_labeller(
        self,
        roster: dict | None = None,
        display: dict[str, int] | None = None,
    ):
        """A one-argument labeller for a Turn, for chunking and rendering."""
        def label_turn(turn) -> str:
            return self.speaker_label(
                turn.channel, turn.speaker, roster, display=display
            )

        # Distinguishes this from the channel-only `label`, which takes a str.
        label_turn.takes_turn = True
        return label_turn

    # --- transcript paths ---
    def transcript_json(self, meeting_id: str) -> Path:
        return self._transcript_base(meeting_id).with_suffix(".json")

    def transcript_md(self, meeting_id: str) -> Path:
        """Readable markdown. Named from notes or a short title, not the id.

        The JSON next to it stays `{meeting_id}.json` so every other path in
        the pipeline can keep using the timestamp id.
        """
        json_path = self.transcript_json(meeting_id)
        if json_path.exists():
            try:
                name = json.loads(json_path.read_text(encoding="utf-8")).get("md_name")
            except (OSError, ValueError):
                name = ""
            if name:
                return json_path.parent / name
        return self._transcript_base(meeting_id).with_suffix(".md")

    def transcript_html(self, meeting_id: str) -> Path:
        """The readable page. Same name as the markdown, so a retitle moves both."""
        return self.transcript_md(meeting_id).with_suffix(".html")

    def _transcript_base(self, meeting_id: str) -> Path:
        # meeting_id looks like 2026-09-11T14-03-22; shard by year/month so the
        # folder stays browsable after a couple of years of meetings.
        year, month = meeting_id[0:4], meeting_id[5:7]
        return self.transcripts_dir / year / month / meeting_id

    def notes_json(self, meeting_id: str) -> Path:
        return self.notes_dir / f"{meeting_id}.json"


def _coerce_int_list(value) -> list[int]:
    if isinstance(value, str):
        return [int(p.strip()) for p in value.split(",") if p.strip()]
    if isinstance(value, int):
        return [value]
    return [int(v) for v in value]


def load_config(path: Path | None = None) -> Config:
    if path is None:
        env = os.environ.get("MEETING_ARCHIVE_CONFIG")
        path = Path(env) if env else DEFAULT_CONFIG_PATH

    data: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)

    cfg = Config()

    if root := data.get("root"):
        cfg.root = Path(root).expanduser()
    for key in ("own_label", "other_label", "room_label", "collection"):
        if key in data:
            setattr(cfg, key, data[key])
    if "audio_retention_days" in data:
        cfg.audio_retention_days = int(data["audio_retention_days"])
        cfg.audio_retention = retention_name(cfg.audio_retention_days)
    if "audio_retention" in data:
        cfg.audio_retention = str(data["audio_retention"])
        cfg.audio_retention_days = retention_days(cfg.audio_retention)

    a = data.get("audio", {})
    if "device_index" in a:
        cfg.audio.device_index = int(a["device_index"])
    if "device_name" in a:
        cfg.audio.device_name = str(a["device_name"])
    if "mic_channels" in a:
        cfg.audio.mic_channels = _coerce_int_list(a["mic_channels"])
    if "sys_channels" in a:
        cfg.audio.sys_channels = _coerce_int_list(a["sys_channels"])
    if "sample_rate" in a:
        cfg.audio.sample_rate = int(a["sample_rate"])

    w = data.get("whisper", {})
    for key in (
        "model",
        "engine",
        "command",
        "model_path",
        "num_threads",
        "device",
        "compute_type",
        "local_files_only",
        "language",
        "beam_size",
        "vad_filter",
        "vad_min_silence_ms",
        "condition_on_previous_text",
        "initial_prompt",
    ):
        if key in w:
            setattr(cfg.whisper, key, w[key])

    d = data.get("diarize", {})
    for key in (
        "enabled",
        "model",
        "segmentation_model",
        "embedding_model",
        "num_threads",
        "cluster_threshold",
        "diarize_channels",
        "min_speakers",
        "max_speakers",
        "min_span_s",
        "identify_popup",
        "engine",
    ):
        if key in d:
            setattr(cfg.diarize, key, d[key])

    c = data.get("chunk", {})
    for key in (
        "target_chars",
        "overlap_turns",
        "max_turn_chars",
        "coalesce_gap_s",
        "max_turn_s",
        "include_context_header",
    ):
        if key in c:
            setattr(cfg.chunk, key, c[key])

    n = data.get("names", {})
    for key in ("enabled", "own_names", "suggest_popup", "min_count"):
        if key in n:
            setattr(cfg.names, key, n[key])

    s = data.get("summary", {})
    for key in (
        "enabled",
        "command",
        "host",
        "model",
        "timeout_s",
        "chunk_chars",
        "profile",
        "when",
        "evening_hour",
    ):
        if key in s:
            setattr(cfg.summary, key, s[key])

    c2 = data.get("cleanup", {})
    if "enabled" in c2:
        cfg.cleanup.enabled = bool(c2["enabled"])

    sm = data.get("speakers_memory", {}) or data.get("speakers", {})
    if "recognise_known" in sm:
        cfg.speakers_memory.recognise_known = bool(sm["recognise_known"])
    for key in ("min_confidence", "min_margin"):
        if key in sm:
            setattr(cfg.speakers_memory, key, float(sm[key]))

    p = data.get("processing", {})
    if "pause_on_pressure" in p:
        cfg.processing.pause_on_pressure = bool(p["pause_on_pressure"])

    g = data.get("glossary", {})
    if isinstance(g, dict):
        reps = {
            str(key): str(value)
            for key, value in g.items()
            if str(key).strip() and str(value).strip()
        }
        if reps:
            cfg.glossary.replacements = reps

    _apply_settings_overlay(cfg)
    return cfg


RETENTION_DAYS = {
    "1_week": 7,
    "week": 7,
    "1_month": 30,
    "month": 30,
    "never": 0,
}


def retention_days(name: str) -> int:
    if str(name).isdigit() or (str(name).startswith("-") and str(name)[1:].isdigit()):
        return int(name)
    return RETENTION_DAYS.get(str(name), 7)


def retention_name(days: int) -> str:
    if days <= 0:
        return "never"
    if days >= 28:
        return "1_month"
    return "1_week"


def _apply_settings_overlay(cfg: Config) -> None:
    """Machine-local toggles written by `ma setting` / SwiftBar.

    Stored next to the archive so they do not require rewriting config.toml.
    """
    path = cfg.root / ".yarn-settings.json"
    if not path.exists():
        return
    try:
        overlay = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if "audio_retention" in overlay:
        cfg.audio_retention = str(overlay["audio_retention"])
        cfg.audio_retention_days = retention_days(cfg.audio_retention)
    if "recognise_known" in overlay:
        cfg.speakers_memory.recognise_known = bool(overlay["recognise_known"])
    if "speaker_engine" in overlay:
        cfg.diarize.engine = str(overlay["speaker_engine"])
    if "cleanup_enabled" in overlay:
        cfg.cleanup.enabled = bool(overlay["cleanup_enabled"])
    if "pause_on_pressure" in overlay:
        cfg.processing.pause_on_pressure = bool(overlay["pause_on_pressure"])
    if "summarize_when" in overlay:
        cfg.summary.when = str(overlay["summarize_when"])
    if "summary_model" in overlay:
        cfg.summary.model = str(overlay["summary_model"])
    if "evening_hour" in overlay:
        try:
            hour = int(overlay["evening_hour"])
            if 0 <= hour <= 23:
                cfg.summary.evening_hour = hour
        except (TypeError, ValueError):
            pass


def write_setting(cfg: Config, key: str, value) -> dict:
    path = cfg.root / ".yarn-settings.json"
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    data[key] = value
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    _apply_settings_overlay(cfg)
    return data
