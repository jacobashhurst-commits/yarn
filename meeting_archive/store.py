"""ChromaDB persistence.

Embedding function: we let Chroma own it and use its default. This is not a
quality preference, it is a compatibility requirement. The chroma-mcp server
only offers default, cohere, openai, jina, voyageai and roboflow -- no BGE, no
Nomic. If this pipeline wrote BGE vectors and the MCP server queried the same
collection with its default embedder, every search would return confident
nonsense, and nothing would look broken.

Upgrading the embedder later is cheap because the transcripts on disk are the
source of truth: change the model, run `ma rebuild`, done.

Chroma metadata wants str, int, float and bool. The exact failure modes were
checked against chromadb 1.5.9 rather than assumed, because they are not what
you would guess:

  None   raises TypeError outright ("cannot convert Python object")
  list   is accepted and stored as a list, but is then silently unfilterable
         -- where={"speakers": {"$eq": "Jacob"}} matches nothing at all

The list case is the dangerous one: it looks like it worked. So flattening
metadata on the way in is load-bearing, not defensive tidiness.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SCALARS = (str, int, float, bool)


def sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Flatten a metadata dict into something Chroma will accept.

    Lists become comma-joined strings, None becomes an empty string, and
    anything else is coerced with str(). Keys with a None value are kept as ""
    rather than dropped, so metadata filters behave consistently across
    meetings that do and don't have notes.
    """
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, bool):
            # bool before int: bool is a subclass of int.
            out[key] = value
        elif value is None:
            out[key] = ""
        elif isinstance(value, (list, tuple, set)):
            out[key] = ", ".join(str(v) for v in value)
        elif isinstance(value, _SCALARS):
            out[key] = value
        else:
            out[key] = str(value)
    return out


def chunk_id(meeting_id: str, index: int) -> str:
    """Deterministic chunk id, so re-ingesting a meeting is idempotent."""
    return f"{meeting_id}#{index:04d}"


def _collection_vanished(exc: BaseException) -> bool:
    """True when an error means the collection handle is stale.

    Chroma identifies a collection by a uuid captured when the handle is
    opened, so dropping and recreating it invalidates every handle held
    elsewhere. The watcher is a long-running process and `ma rebuild` deletes
    the collection, so this happens in normal use: a rebuild from the terminal
    would otherwise break background indexing until the watcher was restarted.

    Matched on the exception name rather than the class, so importing chromadb
    stays lazy and the error type can move between releases.
    """
    return type(exc).__name__ in {
        "NotFoundError",
        "InvalidCollectionException",
    } and "does not exist" in str(exc)


class MeetingStore:
    def __init__(
        self,
        path: Path,
        collection_name: str = "meetings",
        embedding_function: Any | None = None,
    ):
        self.path = Path(path)
        self.collection_name = collection_name
        self._embedding_function = embedding_function
        self._client = None
        self._collection = None

    @property
    def collection(self):
        if self._collection is None:
            try:
                import chromadb
            except ImportError as exc:  # pragma: no cover - environment issue
                raise RuntimeError(
                    "chromadb is not installed. Run: pip install -r requirements.txt"
                ) from exc

            self.path.mkdir(parents=True, exist_ok=True)
            from chromadb.config import Settings

            self._client = chromadb.PersistentClient(
                path=str(self.path),
                settings=Settings(anonymized_telemetry=False),
            )
            kwargs = {}
            if self._embedding_function is not None:
                kwargs["embedding_function"] = self._embedding_function
            self._collection = self._client.get_or_create_collection(
                name=self.collection_name, **kwargs
            )
        return self._collection

    def _run(self, operation):
        """Run an operation against the collection, reopening it if it went.

        One retry only: if the collection is missing again immediately after
        being recreated, that is a real fault and should surface.
        """
        try:
            return operation(self.collection)
        except Exception as exc:
            if not _collection_vanished(exc):
                raise
            log.warning("chroma collection was replaced; reopening it")
            self._collection = None
            return operation(self.collection)

    def delete_meeting(self, meeting_id: str) -> None:
        self._run(lambda c: c.delete(where={"meeting_id": meeting_id}))

    def clear(self) -> None:
        """Drop every derived chunk before rebuilding from transcript files."""
        # Force collection creation first so `_client` is available and an
        # empty archive behaves the same as a populated one.
        _ = self.collection
        self._client.delete_collection(self.collection_name)
        self._collection = None

    def replace_meeting(
        self,
        meeting_id: str,
        documents: list[str],
        metadatas: list[dict[str, Any]],
    ) -> int:
        """Delete any existing chunks for this meeting, then add the new ones.

        Delete-then-add rather than upsert, because re-ingesting after notes
        arrive can change the chunk count -- an upsert would leave orphaned
        chunks from the previous run in the collection.
        """
        if len(documents) != len(metadatas):
            raise ValueError("documents and metadatas must be the same length")

        self.delete_meeting(meeting_id)
        if not documents:
            return 0

        self._run(
            lambda c: c.add(
                ids=[chunk_id(meeting_id, i) for i in range(len(documents))],
                documents=documents,
                metadatas=[sanitize_metadata(m) for m in metadatas],
            )
        )
        log.info("indexed meeting=%s chunks=%d", meeting_id, len(documents))
        return len(documents)

    def search(
        self,
        query: str,
        n_results: int = 5,
        where: dict | None = None,
    ) -> list[dict]:
        result = self._run(
            lambda c: c.query(
                query_texts=[query],
                n_results=n_results,
                **({"where": where} if where else {}),
            )
        )
        hits: list[dict] = []
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        for i, document in enumerate(documents):
            hits.append(
                {
                    "document": document,
                    "metadata": metadatas[i] if i < len(metadatas) else {},
                    "distance": distances[i] if i < len(distances) else None,
                }
            )
        return hits

    def count(self) -> int:
        return self._run(lambda c: c.count())

    def meeting_ids(self) -> list[str]:
        result = self._run(lambda c: c.get(include=["metadatas"]))
        ids = {
            (m or {}).get("meeting_id")
            for m in (result.get("metadatas") or [])
        }
        return sorted(i for i in ids if i)
