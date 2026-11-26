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

# The floors for "not a participant" (3s) and "compare with longer voices"
# (10s) are DiarizeConfig.min_participant_s and short_voice_fold_s. They are
# settings because the right value is a judgement about meetings, not a
# measurement of this model.

# Below this there is too little audio to fingerprint at all.
MIN_VOICEPRINT_S = 0.8

# A voice that spoke for less than this, in a meeting where someone else spoke
# for longer, is a fragment of a person already in the room. The Seek SOW
# meeting (six people, three and a half hours) came back as 96 voices; 55 of
# them had under ten seconds of speech. Asking who each one was is the part
# that makes a long meeting feel broken. Those fragments are attached to the
# longer voice they sound like. With no fingerprint, they go to whoever was
# speaking just before or after. A measured different person stays listed.
MIN_NOTABLE_SPEAKER_S = 10.0

# Same person, split into several clusters, scores about 0.45-0.7 with this
# embedding model. On the confirmed room archive the same person sat at a
# median of 0.54 and different people never cleared 0.55. 0.58 is above that
# worst different-person pair, so every pair at or above it can be joined,
# including a chain of strong matches. A merely-similar pair stays apart.
# Requiring each pair to also be mutual nearest neighbours left the same
# person split: a third fragment is rarely anyone's single closest voice.
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

        # The processor, not the graphics chip. CoreML was tried on a
        # MacBook Pro and this stage went from 5 minutes to more than 12.
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
        # Blips before the longer fold. The longer fold sends a voice it
        # could not fingerprint to whoever spoke nearest, which is right for
        # several seconds of speech and wrong for a listener's "mm": the
        # nearest speaker is the one person who certainly did not say it.
        before_brief = len({span.speaker for span in spans})
        spans = settle_brief_voices(
            spans,
            samples,
            sample_rate,
            self.config,
            min_participant_s=self.config.min_participant_s,
        )
        after_brief = len({span.speaker for span in spans})
        if after_brief < before_brief:
            log.info(
                "voices under %.1fs joined or left unnamed %d -> %d on channel=%s",
                self.config.min_participant_s,
                before_brief,
                after_brief,
                channel,
            )
        before_short = len({span.speaker for span in spans})
        spans = absorb_short_voices(
            spans,
            samples,
            sample_rate,
            self.config,
            min_speaking_s=self.config.short_voice_fold_s,
        )
        after_short = len({span.speaker for span in spans})
        if after_short < before_short:
            log.info(
                "folded voices under %ss %d -> %d on channel=%s",
                int(self.config.short_voice_fold_s),
                before_short,
                after_short,
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
    """Attach a short fragment to a longer voice when it is the same person.

    Does nothing when nobody spoke that long: a short meeting of people who
    each said a little is still those people. A fragment joins the longer
    voice it actually sounds like. A measured mismatch stays, however short:
    duration is not identity. Nearest-in-time is only for a fragment with no
    fingerprint, so a cough in a long meeting still has somewhere to go.
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
            if total < MIN_VOICEPRINT_S:
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
        # A fingerprint that lost is a different person. Nearest-in-time is
        # only for a fragment we could not measure. MHTB Room 1 was 6s then
        # 11s at cosine 0.21; folding on duration alone labelled both Arun.
        if target is None and not _voice_was_compared(speaker, voice_scores, vectors):
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
    score, anchor = ranked[0]
    # Same bar as merge_same_voices. Below it, this is not that person.
    if score < SAME_VOICE_COSINE:
        return None
    return anchor


def _voice_was_compared(
    speaker: str,
    voice_scores: dict[tuple[str, str], float] | None,
    vectors: dict[str, list[float]],
) -> bool:
    """True when this voice has a fingerprint against the longer ones."""
    if speaker in vectors:
        return True
    if not voice_scores:
        return False
    return any(speaker in pair for pair in voice_scores)


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


def settle_brief_voices(
    spans: list[SpeakerSpan],
    samples=None,
    sample_rate: int | None = None,
    config: DiarizeConfig | None = None,
    *,
    min_participant_s: float = 3.0,
    voice_scores: dict[tuple[str, str], float] | None = None,
    keep: frozenset[str] | set[str] = frozenset(),
    renumber: bool = True,
) -> list[SpeakerSpan]:
    """Decide what a voice with only a moment of speech is.

    It is one of two things. If its voiceprint matches a participant, it is
    that participant, and its spans take their key. Otherwise its spans are
    removed: the words stay in the transcript under the channel's neutral
    label, and no numbered speaker is created for them.

    The asymmetry is deliberate. A match from two seconds of audio is rare
    by chance, so it can be believed. A mismatch from two seconds is what
    short audio usually produces even for the same person, so it is not
    evidence of a new one. The older rule kept every mismatch as its own
    speaker, and that is where "Speaker 5: yep" came from.

    Nothing here guesses from who was speaking nearby. A back-channel comes
    from the listener, so the neighbour is the wrong answer more often than
    the right one, and it would put words in a named person's mouth.

    `keep` holds keys that must not be touched, such as a voice Jacob has
    already named. Does nothing when no voice reached the floor: a very
    short recording keeps whoever spoke. With `renumber`, the remaining
    voices are numbered by first appearance, so a blip cannot hold
    "Speaker 1". Leave it off when names are already stored against keys.
    """
    if not spans or min_participant_s <= 0:
        return spans
    totals = _speaker_durations(spans)
    participants = {
        speaker for speaker, total in totals.items() if total >= min_participant_s
    }
    brief = [
        speaker
        for speaker, total in totals.items()
        if total < min_participant_s and speaker not in keep
    ]
    if not participants or not brief:
        return spans

    vectors: dict[str, list[float]] = {}
    if (
        voice_scores is None
        and samples is not None
        and sample_rate
        and config is not None
    ):
        measurable = [s for s in brief if totals[s] >= MIN_VOICEPRINT_S]
        if measurable:
            for speaker in [*measurable, *sorted(participants)]:
                clip = _speaker_waveform(
                    samples, sample_rate, spans, speaker, max_s=12.0
                )
                if clip is None or len(clip) < int(sample_rate * MIN_VOICEPRINT_S):
                    continue
                vector = _embed_samples(config, clip, sample_rate)
                if vector:
                    vectors[speaker] = vector

    mapping: dict[str, str | None] = {}
    for speaker in brief:
        mapping[speaker] = _best_anchor(speaker, participants, voice_scores, vectors)

    kept = [
        (span.start, span.end, mapping.get(span.speaker, span.speaker))
        for span in spans
    ]
    kept = [(start, end, speaker) for start, end, speaker in kept if speaker]
    if not kept:
        return []
    if renumber:
        return to_spans(kept, spans[0].channel)
    return [
        SpeakerSpan(channel=spans[0].channel, start=start, end=end, speaker=speaker)
        for start, end, speaker in kept
    ]


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
    """Map every speaker to the longer member of the same voice.

    ``cosine(a, b)`` is similarity in ``[0, 1]``. Every pair that clears
    ``threshold`` is one person, and a chain of those pairs is too. The kept
    key is the longer stretch, so a name already stored on it still applies.
    A score under the threshold never joins, which is what stops two
    different people who both vaguely resemble a third from becoming one.
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

    for index, left in enumerate(speakers):
        for right in speakers[index + 1 :]:
            if cosine(left, right) >= threshold:
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
    """Up to `max_s` of one voice, taken from across the meeting.

    The longest stretch from the early, middle and late third of where this
    voice spoke comes first, then the longest of whatever is left. Taking
    only the longest stretches fingerprinted a person from one moment of a
    long meeting. Someone who leans back, moves seats or tires over three
    hours then failed to match their own earlier voice and was listed twice.
    """
    import numpy as np

    mine = sorted((span for span in spans if span.speaker == speaker), key=lambda s: s.start)
    if not mine:
        return None
    first, last = mine[0].start, mine[-1].end
    third = (last - first) / 3.0
    picked: list[SpeakerSpan] = []
    if third > 0:
        for index in range(3):
            lo = first + index * third
            hi = last if index == 2 else lo + third
            bucket = [s for s in mine if lo <= (s.start + s.end) / 2.0 <= hi]
            if bucket:
                best = max(bucket, key=lambda s: s.duration)
                if best not in picked:
                    picked.append(best)
    for span in sorted(mine, key=lambda s: s.duration, reverse=True):
        if span not in picked:
            picked.append(span)

    pieces = []
    remaining = int(max_s * sample_rate)
    # An equal share for each third first, so one long stretch cannot use
    # the whole allowance before the other two are heard.
    share = max(remaining // 3, int(sample_rate * 2))
    for index, span in enumerate(picked):
        if remaining <= 0:
            break
        start = max(0, int(span.start * sample_rate))
        end = min(len(samples), int(span.end * sample_rate))
        if end <= start:
            continue
        limit = min(remaining, share) if index < 3 else remaining
        piece = samples[start:end][:limit]
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
    pieces: list[tuple[float, float, str, float, float]] = []
    for span in spans:
        if span.start >= segment.end:
            break
        start = max(segment.start, span.start)
        end = min(segment.end, span.end)
        if end > start:
            pieces.append((span.duration, end - start, span.speaker, start, end))
    # Shortest span first, so an interruption is not credited to the voice
    # whose span happens to cover the whole stretch. It has to be the
    # span's own length, not the part inside this segment: Whisper usually
    # cuts a segment exactly around an interruption, and then both voices
    # cover all of it. Measured inside the segment they tie, and the tie
    # went to whichever key sorted first, which was the person interrupted.
    pieces.sort()
    claimed: list[tuple[float, float]] = []
    totals: dict[str, float] = {}
    for _, _, speaker, start, end in pieces:
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
    *,
    min_participant_s: float = 0.0,
    keep: frozenset[str] | set[str] = frozenset(),
) -> int:
    """Attach a speaker to each segment by greatest temporal overlap, in place.

    A segment that straddles a speaker change goes to whoever holds more of it.
    When the model has marked both people at once, the shorter span is the
    reply and keeps that moment. Otherwise a long span swallows the other
    person's line. Splitting Whisper's text on a timestamp would cut sentences
    in half, and leaving the segment unattributed would drop a line.

    Segments with no overlapping span at all are left alone rather than guessed
    into the nearest speaker. Returns how many were attributed.

    With `min_participant_s`, a voice that ended up holding less than that
    much transcript is unattributed again. Spans and sentences do not line
    up: a voice with four seconds of spans can win one two-word segment and
    lose the rest to whoever was talking over it. Without this second look
    that voice is a numbered speaker with a single "right". `keep` protects
    keys Jacob has already named.
    """
    if not spans:
        return 0

    by_channel: dict[str, list[SpeakerSpan]] = {}
    for span in spans:
        by_channel.setdefault(span.channel, []).append(span)
    for channel_spans in by_channel.values():
        channel_spans.sort(key=lambda s: s.start)

    assigned = 0
    held: dict[str, dict[str, float]] = {}
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
            channel_held = held.setdefault(segment.channel, {})
            channel_held[segment.speaker] = (
                channel_held.get(segment.speaker, 0.0) + totals[segment.speaker]
            )

    if min_participant_s > 0:
        minor: set[str] = set()
        for channel_held in held.values():
            # Same exception as settle_brief_voices: with no participant on
            # the channel, the people who spoke briefly are the meeting.
            if not any(total >= min_participant_s for total in channel_held.values()):
                continue
            minor.update(
                speaker
                for speaker, total in channel_held.items()
                if total < min_participant_s and speaker not in keep
            )
        if minor:
            for segment in segments:
                if segment.speaker in minor:
                    segment.speaker = None
                    assigned -= 1

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
