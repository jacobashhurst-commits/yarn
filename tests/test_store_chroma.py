"""Store tests against the real chromadb library.

A deterministic stand-in embedding function is injected, because Chroma's
default embedder downloads an ONNX model on first use and these tests should
not need the network. The embeddings themselves are meaningless here -- what is
being tested is the API usage: that metadata survives the round trip, that
reindexing a meeting genuinely removes the old chunks, and that a query comes
back in the shape the CLI and MCP layers expect.

Skipped entirely when chromadb isn't installed, so the suite still runs on a
fresh checkout before `pip install -r requirements.txt`.
"""

from __future__ import annotations

import pytest

from meeting_archive.store import MeetingStore, sanitize_metadata

chromadb = pytest.importorskip("chromadb", reason="chromadb not installed")

from chromadb.api.types import Documents, EmbeddingFunction, Embeddings  # noqa: E402


class LengthEF(EmbeddingFunction):
    """Deterministic, offline, and good enough to exercise the plumbing.

    Documents sharing a word land near each other, which is all the query test
    needs. It is not a semantic model and is not asserted on as one.
    """

    def __init__(self) -> None:
        pass

    @staticmethod
    def name() -> str:
        return "length_test_ef"

    def get_config(self) -> dict:
        return {}

    @classmethod
    def build_from_config(cls, config: dict) -> "LengthEF":
        return cls()

    def __call__(self, input: Documents) -> Embeddings:
        vectors = []
        for document in input:
            words = set(document.lower().split())
            vectors.append(
                [
                    1.0 if "pricing" in words else 0.0,
                    1.0 if "hiring" in words else 0.0,
                    1.0 if "roadmap" in words else 0.0,
                    float(len(words) % 5) / 10.0,
                ]
            )
        return vectors


@pytest.fixture
def store(tmp_path) -> MeetingStore:
    return MeetingStore(
        tmp_path / "chroma", collection_name="meetings", embedding_function=LengthEF()
    )


def meta(meeting_id: str, index: int, **extra) -> dict:
    base = {
        "meeting_id": meeting_id,
        "chunk_index": index,
        "date": "2026-09-11",
        "epoch": 1789000000,
        "start_s": 0.0,
        "end_s": 30.0,
        "speakers": ["Jacob", "Other"],
        "attendees": "",
        "notes": "",
        "has_notes": False,
    }
    base.update(extra)
    return base


def test_replace_meeting_adds_chunks(store):
    n = store.replace_meeting(
        "m1",
        documents=["pricing talk", "hiring talk"],
        metadatas=[meta("m1", 0), meta("m1", 1)],
    )
    assert n == 2
    assert store.count() == 2


def test_metadata_survives_the_round_trip_as_scalars(store):
    store.replace_meeting("m1", ["pricing talk"], [meta("m1", 0, notes=None)])
    stored = store.collection.get(include=["metadatas"])["metadatas"][0]

    # The list became a filterable string, and None became "".
    assert stored["speakers"] == "Jacob, Other"
    assert stored["notes"] == ""
    assert stored["has_notes"] is False
    assert stored["chunk_index"] == 0
    for key, value in stored.items():
        assert isinstance(value, (str, int, float, bool)), f"{key} came back as {type(value)}"


def test_flattened_metadata_is_actually_filterable(store):
    # This is the whole point of sanitizing. A list-valued field is accepted by
    # Chroma but matches nothing, so the flattening has to be verified, not
    # assumed.
    store.replace_meeting("m1", ["pricing talk"], [meta("m1", 0)])
    hits = store.collection.get(where={"speakers": {"$eq": "Jacob, Other"}})
    assert hits["ids"], "flattened speakers field did not match its own value"


def test_none_metadata_would_raise_without_sanitizing(store):
    # Documents the real chromadb behaviour that sanitize_metadata exists for.
    store.replace_meeting("m1", ["x"], [meta("m1", 0)])
    with pytest.raises(Exception):
        store.collection.add(ids=["raw"], documents=["x"], metadatas=[{"notes": None}])


def test_reindexing_replaces_rather_than_accumulates(store):
    store.replace_meeting("m1", ["a", "b", "c"], [meta("m1", i) for i in range(3)])
    assert store.count() == 3

    # Notes arriving late can change the chunk count. The old chunks must go,
    # or stale text stays searchable forever.
    store.replace_meeting("m1", ["a+notes"], [meta("m1", 0, notes="pushback", has_notes=True)])
    assert store.count() == 1
    stored = store.collection.get(include=["metadatas"])["metadatas"][0]
    assert stored["notes"] == "pushback"


def test_reindexing_one_meeting_leaves_others_alone(store):
    store.replace_meeting("m1", ["a", "b"], [meta("m1", i) for i in range(2)])
    store.replace_meeting("m2", ["c"], [meta("m2", 0)])
    store.replace_meeting("m1", ["a2"], [meta("m1", 0)])
    assert store.count() == 2
    assert store.meeting_ids() == ["m1", "m2"]


def test_delete_meeting_that_was_never_indexed_is_harmless(store):
    store.replace_meeting("m1", ["a"], [meta("m1", 0)])
    store.delete_meeting("never-existed")
    assert store.count() == 1


def test_replace_with_no_chunks_clears_the_meeting(store):
    store.replace_meeting("m1", ["a"], [meta("m1", 0)])
    assert store.replace_meeting("m1", [], []) == 0
    assert store.count() == 0


def test_clear_drops_all_derived_meetings(store):
    store.replace_meeting("m1", ["a"], [meta("m1", 0)])
    store.replace_meeting("m2", ["b"], [meta("m2", 0)])
    store.clear()
    assert store.count() == 0


def test_a_store_survives_another_process_rebuilding(tmp_path):
    """`ma rebuild` from a terminal must not break the running watcher.

    Chroma pins a collection handle to the uuid it had when it was opened, so
    clear() in one process leaves every other process holding a handle to a
    collection that no longer exists. This is not a rare race: it is what
    happens every time a rebuild runs while the watcher is up.
    """
    path = tmp_path / "chroma"
    watcher_store = MeetingStore(path, embedding_function=LengthEF())
    rebuilder = MeetingStore(path, embedding_function=LengthEF())

    watcher_store.replace_meeting("m1", ["a"], [meta("m1", 0)])
    rebuilder.clear()

    watcher_store.replace_meeting("m2", ["b"], [meta("m2", 0)])
    assert watcher_store.count() == 1
    assert watcher_store.meeting_ids() == ["m2"]
    assert watcher_store.search("b", n_results=1)


def test_search_returns_document_metadata_and_distance(store):
    store.replace_meeting(
        "m1",
        ["the pricing model is per seat", "we discussed hiring two engineers"],
        [meta("m1", 0), meta("m1", 1)],
    )
    hits = store.search("pricing", n_results=2)
    assert len(hits) == 2
    assert set(hits[0]) == {"document", "metadata", "distance"}
    assert hits[0]["metadata"]["meeting_id"] == "m1"
    assert hits[0]["distance"] is not None


def test_search_accepts_a_metadata_filter(store):
    store.replace_meeting("m1", ["pricing"], [meta("m1", 0, date="2026-09-11")])
    store.replace_meeting("m2", ["pricing"], [meta("m2", 0, date="2026-08-01")])
    hits = store.search("pricing", n_results=5, where={"date": {"$eq": "2026-08-01"}})
    assert [h["metadata"]["meeting_id"] for h in hits] == ["m2"]


def test_search_on_an_empty_collection(store):
    assert store.search("anything") == []


def test_mismatched_documents_and_metadatas_is_caught_before_chroma(store):
    with pytest.raises(ValueError):
        store.replace_meeting("m1", ["a", "b"], [meta("m1", 0)])


def test_store_persists_across_instances(tmp_path):
    path = tmp_path / "chroma"
    first = MeetingStore(path, "meetings", embedding_function=LengthEF())
    first.replace_meeting("m1", ["pricing"], [meta("m1", 0)])

    second = MeetingStore(path, "meetings", embedding_function=LengthEF())
    assert second.count() == 1
    assert second.meeting_ids() == ["m1"]


def test_end_to_end_recording_to_search(tmp_path, monkeypatch):
    """The whole pipeline with only Whisper stubbed out.

    Real ffmpeg splits a real multi-channel wav, the real merge and name
    detection run, the transcript is written to a real filesystem, and a real
    Chroma collection is queried at the end.
    """
    import shutil

    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")

    from meeting_archive import pipeline
    from meeting_archive.config import Config
    from meeting_archive.models import MIC, SYS, Segment
    from meeting_archive.records import Transcript
    from tests.test_pipeline import FakeTranscriber, make_wav

    cfg = Config(root=tmp_path / "archive")
    cfg.audio.device_index = 0
    cfg.ensure_dirs()

    raw = make_wav(cfg.in_progress_dir / "2026-09-11T14-03-22__raw.wav", [True, False, False])
    manifest = pipeline.finalize(cfg, raw)
    meeting_id = manifest.meeting_id

    transcriber = FakeTranscriber({
        MIC: [
            Segment(MIC, 0.0, 4.0, "Hey Marina, can you walk me through the pricing model?"),
            Segment(MIC, 12.0, 14.0, "That makes sense."),
        ],
        SYS: [
            Segment(SYS, 4.5, 11.0, "Sure. It's per seat, billed annually, with a volume tier."),
        ],
    })
    live_store = MeetingStore(cfg.chroma_dir, cfg.collection, embedding_function=LengthEF())

    assert pipeline.process(
        cfg, cfg.inbox_dir / f"{meeting_id}.json", transcriber, live_store
    ) == meeting_id

    # Transcript on disk with the speakers correctly interleaved.
    transcript = Transcript.read(cfg.transcript_json(meeting_id))
    assert [t.channel for t in transcript.turns] == [MIC, SYS, MIC]

    # The name came out of the conversation, not a calendar.
    detected = [c.name for c in transcript.detected_names if c.attributed_to == "other"]
    assert detected == ["Marina"]

    # Indexed and searchable.
    assert live_store.count() >= 1
    hits = live_store.search("pricing", n_results=1)
    assert hits
    assert hits[0]["metadata"]["meeting_id"] == meeting_id
    assert "Marina" in hits[0]["document"]  # context header carries the name

    # Notes typed after the fact reindex that meeting in place.
    from meeting_archive import records, watcher

    records.write_notes(
        cfg.notes_json(meeting_id), meeting_id,
        notes="they were cagey about discounts", attendees="Marina, Wei", applied=False,
    )
    assert watcher.sweep_notes(cfg, live_store) == 1
    hits = live_store.search("pricing", n_results=1)
    assert "cagey about discounts" in hits[0]["document"]
    assert hits[0]["metadata"]["attendees"] == "Marina, Wei"
    assert hits[0]["metadata"]["has_notes"] is True

    # Audio is inside the retention window, not deleted on embed.
    assert list(cfg.audio_archive_dir.glob("*__mic.wav"))


def test_sanitize_is_applied_by_replace_meeting_not_just_available(store):
    # Guards against someone later calling collection.add directly and
    # bypassing the flattening.
    raw = {"speakers": ["Jacob"], "notes": None}
    assert sanitize_metadata(raw) == {"speakers": "Jacob", "notes": ""}
    store.replace_meeting("m1", ["x"], [raw])
    stored = store.collection.get(include=["metadatas"])["metadatas"][0]
    assert stored["speakers"] == "Jacob"
