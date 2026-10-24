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

# A cough or a clipped "yeah" still comes back as its own cluster. Under a
# second in total is not another person: naming it, and listing it, makes the
# meeting look more crowded than it was. The words stay on the channel.
MIN_LISTED_SPEAKER_S = 1.0

# A voice that spoke for less than this, in a meeting where someone else spoke
# for longer, is a fragment of a person already in the room. The Seek SOW
# meeting (six people, three and a half hours) came back as 96 voices; 55 of
# them had under ten seconds of speech. Asking who each one was is the part
# that makes a long meeting feel broken. Those fragments are attached to the
# longer voice they sound like, or to whoever was speaking just before or
# after, and they are not listed as their own speaker.
MIN_NOTABLE_SPEAKER_S = 10.0

# Same person, split into several clusters, scores about 0.45-0.7 with this
# embedding model. Different people on the same call sit around 0.1-0.35.
# 0.58 is the high end of "same voice" so a chain of merely-similar tracks
# cannot pull a whole meeting into one speaker. Matches also have to be
# mutual: A’s closest voice is B and B’s closest voice is A.
SAME_VOICE_COSINE = 0.58

# sherpa's distance cut. Smaller means more clusters. 0.9, the cautious
# setting, still glued two different people on a 47s headphone recording
# into one voice: their embeddings sat just under that distance. 0.5 is
# sherpa's own default, and it separated them. It is only used when the
# cautious cut came back with a single voice.
RETRY_CLUSTER_THRESHOLD = 0.5

# A single returned voice shorter than this is a short remark, not evidence
# that two people were merged. Below it, the second pass is not worth running.
SINGLE_VOICE_RETRY_S = 8.0


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
        self._pipelines: dict[tuple[int, float], object] = {}
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

    def _pipeline_for(
        self,
        expected_speakers: int | None,
        threshold: float | None = None,
    ):
        if self._unavailable:
            raise DiarizationUnavailable(self._unavailable)
        distance = (
            self.config.cluster_threshold if threshold is None else float(threshold)
        )
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
        key = (cluster_count, round(distance, 3))
        if key in self._pipelines:
            return self._pipelines[key]

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
                    threshold=distance,
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

        self._pipelines[key] = pipeline
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
        pipeline = self._pipeline_for(None)
        if sample_rate != pipeline.sample_rate:
            raise DiarizationUnavailable(
                f"{path.name} is {sample_rate}Hz; diarizer expects "
                f"{pipeline.sample_rate}Hz"
            )

        import numpy as np

        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        # The cautious distance is the first answer. A listed count is not
        # forced on top of it: asking sherpa for exactly N speakers merged
        # people it could already tell apart. The count is only a reason to
        # look again when that first answer found fewer people than were named.
        spans = self._collapse(
            self._raw_spans(samples, pipeline, channel),
            samples,
            sample_rate,
            channel,
        )
        found = substantive_voices(spans)
        if expected_speakers and found < expected_speakers:
            # One spare cluster. Exact N spent a slot on a sub-2s outlier and
            # hid a real person; N+1 recovers them, and the outlier is dropped
            # only when it is the single tiny extra.
            over = self._collapse(
                self._raw_spans(
                    samples,
                    self._pipeline_for(expected_speakers + 1),
                    channel,
                ),
                samples,
                sample_rate,
                channel,
            )
            cleaned = drop_tiny_extra_cluster(over, expected_speakers)
            spans = choose_richer_split(spans, cleaned if cleaned is not None else over)
        elif (
            found <= 1
            and sum(span.duration for span in spans) >= SINGLE_VOICE_RETRY_S
            and self.config.cluster_threshold > RETRY_CLUSTER_THRESHOLD
        ):
            # No list of names. Two people through the headphones still have
            # to come back as two voices. The cautious cut merges them when
            # they sit just inside it, which is what a short clip did.
            alt = self._collapse(
                self._raw_spans(
                    samples,
                    self._pipeline_for(None, threshold=RETRY_CLUSTER_THRESHOLD),
                    channel,
                ),
                samples,
                sample_rate,
                channel,
            )
            spans = choose_richer_split(spans, alt)

        log.info(
            "channel=%s speakers=%d spans=%d",
            channel,
            len({s.speaker for s in spans}),
            len(spans),
        )
        return spans

    def _raw_spans(self, samples, pipeline, channel: str) -> list[SpeakerSpan]:
        result = pipeline.process(samples)
        raw = [
            (float(segment.start), float(segment.end), str(segment.speaker))
            for segment in result.sort_by_start_time()
        ]
        return to_spans(raw, channel, min_span_s=self.config.min_span_s)

    def _collapse(
        self,
        spans: list[SpeakerSpan],
        samples,
        sample_rate: int,
        channel: str,
    ) -> list[SpeakerSpan]:
        before = len({span.speaker for span in spans})
        spans = merge_same_voices(spans, samples, sample_rate, self.config)
        after = len({span.speaker for span in spans})
        if after < before:
            log.info(
                "merged same-voice clusters %d -> %d on channel=%s",
                before,
                after,
                channel,
            )
        before_short = len({span.speaker for span in spans})
        spans = absorb_short_voices(spans, samples, sample_rate, self.config)
        after_short = len({span.speaker for span in spans})
        if after_short < before_short:
            log.info(
                "folded voices under %ss %d -> %d on channel=%s",
                int(MIN_NOTABLE_SPEAKER_S),
                before_short,
                after_short,
                channel,
            )
        before_brief = len({span.speaker for span in spans})
        spans = drop_brief_voices(spans)
        after_brief = len({span.speaker for span in spans})
        if after_brief < before_brief:
            log.info(
                "dropped voices under one second %d -> %d on channel=%s",
                before_brief,
                after_brief,
                channel,
            )
        return spans


def _speaker_durations(spans: list[SpeakerSpan]) -> dict[str, float]:
    totals: dict[str, float] = {}
    for span in spans:
        totals[span.speaker] = totals.get(span.speaker, 0.0) + span.duration
    return totals


def substantive_voices(
    spans: list[SpeakerSpan],
    min_speaking_s: float = MIN_SUBSTANTIVE_SPEAKER_S,
) -> int:
    """How many voices spoke for long enough to be a person, not a blip."""
    totals = _speaker_durations(spans)
    return sum(1 for total in totals.values() if total >= min_speaking_s)


def choose_richer_split(
    automatic: list[SpeakerSpan],
    retry: list[SpeakerSpan],
) -> list[SpeakerSpan]:
    """Keep a second clustering only when it separated real voices.

    The first pass stays when the retry found the same people, or only an
    extra blip. A headphone recording of two people has to be allowed to
    replace a single merged voice.
    """
    if substantive_voices(retry) > substantive_voices(automatic):
        return retry
    return automatic


def _smallest_speaker_duration(spans: list[SpeakerSpan]) -> float:
    totals = _speaker_durations(spans)
    return min(totals.values(), default=0.0)


def absorb_short_voices(
    spans: list[SpeakerSpan],
    samples=None,
    sample_rate: int | None = None,
    config: DiarizeConfig | None = None,
    *,
    min_speaking_s: float = MIN_NOTABLE_SPEAKER_S,
    voice_scores: dict[tuple[str, str], float] | None = None,
) -> list[SpeakerSpan]:
    """Attach every voice under ``min_speaking_s`` to a longer voice.

    Does nothing when nobody spoke that long: a short meeting of people who
    each said a little is still those people. When a longer voice exists, a
    fragment is not listed on its own. Preference is the longer voice it
    sounds most like, then whoever was speaking nearest to it in time.
    Speaker keys of the longer voices are kept, so a name already stored
    against one of them still points at that voice.
    """
    if not spans:
        return spans
    totals = _speaker_durations(spans)
    anchors = {speaker for speaker, total in totals.items() if total >= min_speaking_s}
    short = [speaker for speaker, total in totals.items() if total < min_speaking_s]
    if not anchors or not short:
        return spans

    vectors: dict[str, list[float]] = {}
    if (
        voice_scores is None
        and samples is not None
        and sample_rate
        and config is not None
    ):
        for speaker, total in totals.items():
            if total < 0.8:
                continue
            clip = _speaker_waveform(
                samples, sample_rate, spans, speaker, max_s=12.0
            )
            if clip is None or len(clip) < int(sample_rate * 0.8):
                continue
            vector = _embed_samples(config, clip, sample_rate)
            if vector:
                vectors[speaker] = vector

    mapping: dict[str, str] = {}
    for speaker in short:
        target = _best_anchor(speaker, anchors, voice_scores, vectors)
        if target is None:
            target = _nearest_anchor(spans, speaker, anchors)
        if target and target != speaker:
            mapping[speaker] = target
    if not mapping:
        return spans

    rewritten: list[SpeakerSpan] = []
    for span in spans:
        speaker = mapping.get(span.speaker, span.speaker)
        rewritten.append(
            SpeakerSpan(
                channel=span.channel,
                start=span.start,
                end=span.end,
                speaker=speaker,
            )
        )
    return rewritten


def _best_anchor(
    speaker: str,
    anchors: set[str],
    voice_scores: dict[tuple[str, str], float] | None,
    vectors: dict[str, list[float]],
) -> str | None:
    ranked: list[tuple[float, str]] = []
    if voice_scores is not None:
        for anchor in anchors:
            score = voice_scores.get((speaker, anchor))
            if score is None:
                score = voice_scores.get((anchor, speaker))
            if score is not None:
                ranked.append((score, anchor))
    elif speaker in vectors:
        for anchor in anchors:
            if anchor in vectors:
                ranked.append((_cosine(vectors[speaker], vectors[anchor]), anchor))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    return ranked[0][1]


def _nearest_anchor(
    spans: list[SpeakerSpan], speaker: str, anchors: set[str]
) -> str | None:
    """The longer voice speaking closest to this fragment, by its own speech."""
    anchor_spans = [span for span in spans if span.speaker in anchors]
    if not anchor_spans:
        return None
    votes: dict[str, float] = {}
    for span in spans:
        if span.speaker != speaker:
            continue
        best: str | None = None
        best_gap = float("inf")
        for other in anchor_spans:
            if other.end <= span.start:
                gap = span.start - other.end
            elif other.start >= span.end:
                gap = other.start - span.end
            else:
                gap = 0.0
            if gap < best_gap:
                best_gap = gap
                best = other.speaker
        if best:
            votes[best] = votes.get(best, 0.0) + span.duration
    if not votes:
        return None
    return max(votes, key=votes.get)


def drop_brief_voices(
    spans: list[SpeakerSpan],
    min_speaking_s: float = MIN_LISTED_SPEAKER_S,
) -> list[SpeakerSpan]:
    """Remove every voice whose speech, added up, is under a second.

    The remaining voices are renumbered by first appearance, so a blip cannot
    hold "Speaker 1" and push the person who opened the meeting to 2.
    """
    if not spans:
        return spans
    totals = _speaker_durations(spans)
    brief = {speaker for speaker, total in totals.items() if total < min_speaking_s}
    if not brief:
        return spans
    kept = [span for span in spans if span.speaker not in brief]
    if not kept:
        return []
    return to_spans(
        [(span.start, span.end, span.speaker) for span in kept],
        kept[0].channel,
    )


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


def group_same_voices(
    speakers: list[str],
    cosine,
    durations: dict[str, float],
    threshold: float = SAME_VOICE_COSINE,
) -> dict[str, str]:
    """Map every speaker to the longer member of a mutual voice match.

    ``cosine(a, b)`` is similarity in ``[0, 1]``. Only pairs who are each
    other's closest voice, and only when that score clears ``threshold``, are
    combined. A chain of moderate scores must not collapse a meeting.
    """
    parent = {speaker: speaker for speaker in speakers}

    def find(speaker: str) -> str:
        while parent[speaker] != speaker:
            parent[speaker] = parent[parent[speaker]]
            speaker = parent[speaker]
        return speaker

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root == right_root:
            return
        if durations.get(left_root, 0.0) < durations.get(right_root, 0.0):
            parent[left_root] = right_root
        else:
            parent[right_root] = left_root

    closest: dict[str, str] = {}
    for left in speakers:
        ranked = sorted(
            ((cosine(left, right), right) for right in speakers if right != left),
            reverse=True,
        )
        if not ranked or ranked[0][0] < threshold:
            continue
        closest[left] = ranked[0][1]
    for left, right in closest.items():
        if closest.get(right) == left:
            union(left, right)
    return {speaker: find(speaker) for speaker in speakers}


def merge_same_voices(
    spans: list[SpeakerSpan],
    samples,
    sample_rate: int,
    config: DiarizeConfig,
) -> list[SpeakerSpan]:
    """Collapse clusters that are the same person, keeping their speaker key.

    Speaker keys are not renumbered, so a name already stored against a track
    still points at that voice. Embedding failure leaves the spans unchanged.
    """
    durations = _speaker_durations(spans)
    speakers = [speaker for speaker, total in durations.items() if total >= 1.5]
    if len(speakers) < 2:
        return spans
    vectors: dict[str, list[float]] = {}
    for speaker in speakers:
        clip = _speaker_waveform(samples, sample_rate, spans, speaker, max_s=8.0)
        if clip is None or len(clip) < int(sample_rate * 0.8):
            continue
        vector = _embed_samples(config, clip, sample_rate)
        if vector:
            vectors[speaker] = vector
    if len(vectors) < 2:
        return spans

    def cosine(left: str, right: str) -> float:
        return _cosine(vectors[left], vectors[right])

    mapping = group_same_voices(list(vectors), cosine, durations)
    if all(speaker == root for speaker, root in mapping.items()):
        return spans
    merged: list[SpeakerSpan] = []
    for span in spans:
        merged.append(
            SpeakerSpan(
                channel=span.channel,
                start=span.start,
                end=span.end,
                speaker=mapping.get(span.speaker, span.speaker),
            )
        )
    return merged


def _speaker_waveform(samples, sample_rate: int, spans: list[SpeakerSpan], speaker: str, max_s: float):
    import numpy as np

    pieces = []
    remaining = int(max_s * sample_rate)
    ordered = sorted(
        (span for span in spans if span.speaker == speaker),
        key=lambda span: span.duration,
        reverse=True,
    )
    for span in ordered:
        if remaining <= 0:
            break
        start = max(0, int(span.start * sample_rate))
        end = min(len(samples), int(span.end * sample_rate))
        if end <= start:
            continue
        piece = samples[start:end][:remaining]
        pieces.append(piece)
        remaining -= len(piece)
    if not pieces:
        return None
    return np.concatenate(pieces)


def _embed_samples(config: DiarizeConfig, samples, sample_rate: int) -> list[float]:
    model = Path(config.embedding_model).expanduser()
    if not model.is_file():
        return []
    try:
        extractor = _embedding_extractor(config, model)
        stream = extractor.create_stream()
        _feed_embedding_stream(stream, sample_rate, samples)
        if hasattr(extractor, "is_ready") and not extractor.is_ready(stream):
            return []
        vector = extractor.compute(stream)
    except Exception as exc:
        log.warning("could not embed a diarized voice: %s", exc)
        return []
    return [float(x) for x in vector]


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = norm_l = norm_r = 0.0
    for a, b in zip(left, right):
        dot += a * b
        norm_l += a * a
        norm_r += b * b
    if norm_l <= 0 or norm_r <= 0:
        return 0.0
    return dot / ((norm_l ** 0.5) * (norm_r ** 0.5))


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


def _exclusive_overlap(
    segment: Segment,
    spans: list[SpeakerSpan],
) -> dict[str, float]:
    """Time each voice holds once a shorter overlapping voice has claimed its own.

    Spans are already sorted by start. A span that begins after the segment
    cannot overlap it, and nothing later can either.
    """
    pieces: list[tuple[float, str, float, float]] = []
    for span in spans:
        if span.start >= segment.end:
            break
        start = max(segment.start, span.start)
        end = min(segment.end, span.end)
        if end > start:
            pieces.append((end - start, span.speaker, start, end))
    # Shortest claim first, so an interruption is not credited to the voice
    # whose span happens to cover the whole stretch.
    pieces.sort()
    claimed: list[tuple[float, float]] = []
    totals: dict[str, float] = {}
    for _, speaker, start, end in pieces:
        amount = 0.0
        for begin, stop in _subtract_intervals([(start, end)], claimed):
            amount += stop - begin
        if amount > 0:
            totals[speaker] = totals.get(speaker, 0.0) + amount
        claimed = _merge_intervals(claimed + [(start, end)])
    return totals


def _subtract_intervals(
    pieces: list[tuple[float, float]],
    claimed: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    free = pieces
    for begin, stop in claimed:
        nxt: list[tuple[float, float]] = []
        for start, end in free:
            if stop <= start or begin >= end:
                nxt.append((start, end))
                continue
            if start < begin:
                nxt.append((start, begin))
            if end > stop:
                nxt.append((stop, end))
        free = nxt
    return free


def _merge_intervals(pieces: list[tuple[float, float]]) -> list[tuple[float, float]]:
    ordered = sorted(pieces)
    merged: list[tuple[float, float]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def assign_speakers(
    segments: list[Segment],
    spans: list[SpeakerSpan],
) -> int:
    """Attach a speaker to each segment by greatest temporal overlap, in place.

    A segment that straddles a speaker change goes to whoever holds more of it.
    When the model has marked both people at once, the shorter span is the
    reply and keeps that moment. Otherwise a long span swallows the other
    person's line. Splitting Whisper's text on a timestamp would cut sentences
    in half, and leaving the segment unattributed would drop a line.

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

        totals = _exclusive_overlap(segment, candidates)
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
