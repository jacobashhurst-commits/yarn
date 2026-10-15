"""Local speaker diarization via sherpa-onnx.

Whisper tells us *what* was said and when. It has no idea *who* said it. On the
mic channel that doesn't matter -- it is Jacob by construction. On the other two
it matters a lot: `sys` carries everyone else on a call, and `room` carries a
whole table of people on one microphone. Without this module both collapse into
a single undifferentiated voice.

The runtime and model files are downloaded from the open-source sherpa-onnx
GitHub releases. There is no account, token, API, or network access at
inference time.

Two deliberate choices:

  Whisper's segmentation is authoritative, not the diarizer's. We ask it only
  "who is speaking during this stretch of audio", then attach each existing
  Whisper segment to whichever speaker overlaps it most. Re-cutting Whisper's
  text on diarizer boundaries would split sentences mid-clause, and the text is
  what Jacob actually reads and searches.

  Speakers are renumbered by first appearance. Clustering IDs have no relation
  to who spoke
  first. "Speaker 1" has to mean "the first voice you heard" or Jacob cannot
  check it against his memory of the meeting.

Diarization failing must never cost a transcript. A missing runtime or model
file degrades to "no speaker labels" with a loud log line -- the transcript,
archive and search index remain correct, just less specific.
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path

from .config import DiarizeConfig
from .models import Segment, SpeakerSpan, speaker_key

log = logging.getLogger(__name__)

# A listed participant represented by less than this much speech is usually an
# extreme embedding outlier, not a real voice. Do not simply reduce the
# requested count: over-cluster once, then discard only the tiny artifact. On
# the Bert/Teddy/Leanne fixture, exact 3-way clustering produced 265s, 316s,
# 0.6s; 4-way clustering produced 264s, 287s, 30s, 0.6s and recovered Leanne.
MIN_SUBSTANTIVE_SPEAKER_S = 2.0


class DiarizationUnavailable(RuntimeError):
    """Diarization cannot run at all: missing package or model files."""


class Diarizer:
    """Holds one loaded sherpa-onnx pipeline and reuses it for every file.

    Loading costs seconds and a few hundred MB, so the watcher builds one and
    keeps it for the life of the process, exactly like the Transcriber.
    """

    def __init__(self, config: DiarizeConfig):
        self.config = config
        # A known speaker count changes only the cheap clustering stage, but
        # sherpa stores it in the pipeline configuration. Cache one pipeline
        # per count so meetings with 2, 8, or an unknown number of voices all
        # remain supported without reloading models every time.
        self._pipelines: dict[int, object] = {}
        self._unavailable: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def channel_enabled(self, channel: str) -> bool:
        """The mic channel is excluded here, not by the caller forgetting to."""
        return self.enabled and channel in self.config.diarize_channels

    def unload(self) -> None:
        """Drop cached sherpa pipelines so Neo can free a few hundred MB."""
        self._pipelines.clear()
        unload_embedding_extractors()

    @property
    def pipeline(self):
        """The automatic/default pipeline, retained for compatibility."""
        return self._pipeline_for(None)

    def _pipeline_for(self, expected_speakers: int | None):
        if self._unavailable:
            raise DiarizationUnavailable(self._unavailable)
        cluster_count = (
            expected_speakers
            if expected_speakers is not None and expected_speakers > 0
            else (
                self.config.min_speakers
                if self.config.min_speakers > 0
                and self.config.min_speakers == self.config.max_speakers
                else -1
            )
        )
        if cluster_count in self._pipelines:
            return self._pipelines[cluster_count]

        try:
            import sherpa_onnx
        except ImportError as exc:
            self._unavailable = f"sherpa-onnx could not load: {exc}"
            raise DiarizationUnavailable(self._unavailable) from exc

        segmentation = Path(self.config.segmentation_model).expanduser()
        embedding = Path(self.config.embedding_model).expanduser()
        missing = [str(path) for path in (segmentation, embedding) if not path.is_file()]
        if missing:
            self._unavailable = "missing local diarization model(s): " + ", ".join(missing)
            raise DiarizationUnavailable(self._unavailable)

        log.info(
            "loading diarization pipeline model=%s speakers=%s",
            self.config.model,
            cluster_count if cluster_count > 0 else "automatic",
        )
        try:
            config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                    pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                        model=str(segmentation)
                    ),
                    num_threads=self.config.num_threads,
                ),
                embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=str(embedding),
                    num_threads=self.config.num_threads,
                ),
                clustering=sherpa_onnx.FastClusteringConfig(
                    num_clusters=cluster_count,
                    threshold=self.config.cluster_threshold,
                ),
                min_duration_on=self.config.min_span_s,
                min_duration_off=0.5,
            )
            if not config.validate():
                raise ValueError("sherpa-onnx rejected the diarization configuration")
            pipeline = sherpa_onnx.OfflineSpeakerDiarization(config)
        except Exception as exc:
            self._unavailable = f"could not load local diarization models: {exc}"
            raise DiarizationUnavailable(self._unavailable) from exc

        self._pipelines[cluster_count] = pipeline
        return pipeline

    def diarize_file(
        self,
        path: Path,
        channel: str,
        expected_speakers: int | None = None,
    ) -> list[SpeakerSpan]:
        """Return who-spoke-when for one mono wav, keyed within the channel."""
        log.info(
            "diarizing %s (channel=%s expected_speakers=%s)",
            Path(path).name,
            channel,
            expected_speakers if expected_speakers else "automatic",
        )
        with wave.open(str(path), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise DiarizationUnavailable(
                    f"{path.name} must be mono 16-bit PCM for diarization"
                )
            sample_rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
        pipeline = self._pipeline_for(expected_speakers)
        if sample_rate != pipeline.sample_rate:
            raise DiarizationUnavailable(
                f"{path.name} is {sample_rate}Hz; diarizer expects "
                f"{pipeline.sample_rate}Hz"
            )

        import numpy as np

        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        result = pipeline.process(samples)
        raw = [
            (float(segment.start), float(segment.end), str(segment.speaker))
            for segment in result.sort_by_start_time()
        ]
        spans = to_spans(raw, channel, min_span_s=self.config.min_span_s)

        # Hierarchical clustering can spend one of the requested clusters on a
        # sub-second outlier and merge two actual people to compensate. Retry
        # with one spare cluster; when that isolates one tiny artifact and
        # leaves the requested number of substantive voices, keep those voices.
        if (
            expected_speakers
            and len({s.speaker for s in spans}) == expected_speakers
            and _smallest_speaker_duration(spans) < MIN_SUBSTANTIVE_SPEAKER_S
        ):
            retry = self._pipeline_for(expected_speakers + 1).process(samples)
            retry_raw = [
                (float(segment.start), float(segment.end), str(segment.speaker))
                for segment in retry.sort_by_start_time()
            ]
            retry_spans = to_spans(
                retry_raw, channel, min_span_s=self.config.min_span_s
            )
            cleaned = drop_tiny_extra_cluster(
                retry_spans,
                expected_speakers=expected_speakers,
                min_speaking_s=MIN_SUBSTANTIVE_SPEAKER_S,
            )
            if cleaned is not None:
                log.info(
                    "discarded a %.1fs clustering artifact after retrying "
                    "with %d clusters",
                    _smallest_speaker_duration(retry_spans),
                    expected_speakers + 1,
                )
                spans = cleaned

        log.info(
            "channel=%s speakers=%d spans=%d",
            channel,
            len({s.speaker for s in spans}),
            len(spans),
        )
        return spans


def _speaker_durations(spans: list[SpeakerSpan]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for span in spans:
        totals[span.speaker] = totals.get(span.speaker, 0.0) + span.duration
    return totals


def _smallest_speaker_duration(spans: list[SpeakerSpan]) -> float:
    totals = _speaker_durations(spans)
    return min(totals.values(), default=0.0)


def drop_tiny_extra_cluster(
    spans: list[SpeakerSpan],
    expected_speakers: int,
    min_speaking_s: float = MIN_SUBSTANTIVE_SPEAKER_S,
) -> list[SpeakerSpan] | None:
    """Drop exactly one tiny cluster and renumber the real voices.

    Returns ``None`` unless the evidence has the precise safe shape we expect:
    one extra cluster, exactly one of them below the duration floor, and the
    requested number left. Ambiguous output is preserved rather than silently
    deleting somebody who only spoke briefly.
    """
    totals = _speaker_durations(spans)
    if len(totals) != expected_speakers + 1:
        return None
    tiny = [speaker for speaker, total in totals.items() if total < min_speaking_s]
    if len(tiny) != 1:
        return None
    kept = [span for span in spans if span.speaker != tiny[0]]
    if len({span.speaker for span in kept}) != expected_speakers:
        return None
    return to_spans(
        [(span.start, span.end, span.speaker) for span in kept],
        kept[0].channel,
    )


def to_spans(
    raw: list[tuple[float, float, str]],
    channel: str,
    min_span_s: float = 0.0,
) -> list[SpeakerSpan]:
    """Turn (start, end, cluster_label) triples into renumbered SpeakerSpans.

    Clusters are renumbered by first appearance. Very short spans are dropped
    before numbering, so a 200ms crosstalk blip cannot claim the name
    "Speaker 1" and push the person who actually opened the meeting to 2.
    """
    kept = [
        (start, end, label)
        for start, end, label in raw
        if end - start >= min_span_s
    ]
    if not kept:
        return []

    kept.sort(key=lambda r: (r[0], r[1]))

    order: dict[str, int] = {}
    for start, _, label in kept:
        if label not in order:
            order[label] = len(order) + 1

    return [
        SpeakerSpan(
            channel=channel,
            start=start,
            end=end,
            speaker=speaker_key(channel, order[label]),
        )
        for start, end, label in kept
    ]


def assign_speakers(
    segments: list[Segment],
    spans: list[SpeakerSpan],
) -> int:
    """Attach a speaker to each segment by greatest temporal overlap, in place.

    A segment that straddles a speaker change goes to whoever holds more of it.
    That is occasionally wrong at the boundary, and it is still much better than
    the alternatives: splitting Whisper's text on a timestamp cuts sentences in
    half, and leaving the segment unattributed drops a line out of the
    transcript entirely.

    Segments with no overlapping span at all are left alone rather than guessed
    into the nearest speaker. Returns how many were attributed.
    """
    if not spans:
        return 0

    by_channel: dict[str, list[SpeakerSpan]] = {}
    for span in spans:
        by_channel.setdefault(span.channel, []).append(span)
    for channel_spans in by_channel.values():
        channel_spans.sort(key=lambda s: s.start)

    assigned = 0
    for segment in segments:
        candidates = by_channel.get(segment.channel)
        if not candidates:
            continue

        totals: dict[str, float] = {}
        for span in candidates:
            if span.start >= segment.end:
                break  # sorted by start: nothing later can overlap either
            overlap = min(segment.end, span.end) - max(segment.start, span.start)
            if overlap > 0:
                totals[span.speaker] = totals.get(span.speaker, 0.0) + overlap

        if totals:
            # Tie broken by speaker key so the result is deterministic across
            # runs; an arbitrary dict order here would make tests flap.
            segment.speaker = max(sorted(totals), key=lambda k: totals[k])
            assigned += 1

    return assigned


def speaker_keys(segments: list[Segment]) -> list[str]:
    """Distinct speaker keys present, ordered by first appearance in time."""
    seen: dict[str, float] = {}
    for segment in segments:
        if segment.speaker and segment.speaker not in seen:
            seen[segment.speaker] = segment.start
    return sorted(seen, key=lambda k: (seen[k], k))


# Extractors are separate from the diarization pipeline so a confirmation
# dialog can fingerprint a 6-second clip without loading the segmenter.
_embedding_extractors: dict[str, object] = {}


def unload_embedding_extractors() -> None:
    _embedding_extractors.clear()


def embed_file(config: DiarizeConfig, path: Path) -> list[float]:
    """Voice fingerprint from a short wav. Empty if the model or file is missing.

    Stored on the known-speaker record together with a snippet under
    speakers/clips/. Meeting audio can be pruned later; this vector is what
    remains for automatic suggestions.
    """
    model = Path(config.embedding_model).expanduser()
    if not model.is_file():
        log.warning("speaker embedding model missing: %s", model)
        return []
    try:
        samples, sample_rate = _read_mono_wav(path)
    except (OSError, wave.Error, ValueError) as exc:
        log.warning("could not read %s for speaker embedding: %s", path, exc)
        return []
    if samples.size == 0:
        return []
    try:
        extractor = _embedding_extractor(config, model)
        stream = extractor.create_stream()
        _feed_embedding_stream(stream, sample_rate, samples)
        if hasattr(extractor, "is_ready") and not extractor.is_ready(stream):
            log.warning("speaker embedding extractor not ready for %s", path.name)
            return []
        vector = extractor.compute(stream)
    except Exception as exc:
        log.warning("speaker embedding failed for %s: %s", path.name, exc)
        return []
    return [float(x) for x in vector]


def _read_mono_wav(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise ValueError(f"{path.name} is not 16-bit PCM")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, sample_rate


def _embedding_extractor(config: DiarizeConfig, model: Path):
    key = str(model)
    cached = _embedding_extractors.get(key)
    if cached is not None:
        return cached
    import sherpa_onnx

    extractor_config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(
        model=key,
        num_threads=config.num_threads,
    )
    if hasattr(extractor_config, "validate") and not extractor_config.validate():
        raise ValueError("sherpa-onnx rejected the speaker embedding configuration")
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(extractor_config)
    _embedding_extractors[key] = extractor
    return extractor


def _feed_embedding_stream(stream, sample_rate: int, samples) -> None:
    try:
        stream.accept_waveform(sample_rate=sample_rate, waveform=samples)
    except TypeError:
        stream.accept_waveform(sample_rate, samples)
    if hasattr(stream, "input_finished"):
        stream.input_finished()
