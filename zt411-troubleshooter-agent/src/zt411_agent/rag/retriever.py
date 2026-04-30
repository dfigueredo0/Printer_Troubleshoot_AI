"""
RAG retriever for the ZT411 troubleshooter.

Single class, single function — designed to be cheap to instantiate but
to defer all heavy I/O (model load, FAISS index load) to the first
``retrieve()`` call. The orchestrator caches one Retriever per session,
so the model + index load once and stay in memory.

If the index does not exist yet (e.g. CI / unit tests with no built
corpus), every ``retrieve()`` call returns ``[]`` cleanly without
raising. The orchestrator's contract is that an empty snippet list is
the same as "no RAG grounding this turn" — the planner already handles
that path.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..planner import RagSnippet

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults — match index_builder
# ---------------------------------------------------------------------------

DEFAULT_INDEX_PATH = "data/rag_corpus/index.faiss"
DEFAULT_CHUNKS_PATH = "data/rag_corpus/chunks.jsonl"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------


class Retriever:
    """Loads a FAISS index + chunks.jsonl on first use, then answers
    cosine-similarity top-k queries.

    Instantiation is cheap; the model and index are lazily loaded on the
    first ``retrieve()`` call. Subsequent calls reuse the cached objects.

    Graceful degradation: if either the FAISS index or chunks.jsonl is
    missing, ``retrieve()`` logs a single warning per Retriever instance
    and returns ``[]`` from then on.
    """

    def __init__(
        self,
        index_path: str | Path = DEFAULT_INDEX_PATH,
        chunks_path: str | Path = DEFAULT_CHUNKS_PATH,
        *,
        embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    ) -> None:
        self.index_path = Path(index_path)
        self.chunks_path = Path(chunks_path)
        self.embedding_model = embedding_model

        self._encoder: Any = None
        self._index: Any = None
        self._chunks: list[dict] | None = None
        self._unavailable: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def retrieve(self, query: str, k: int = 5) -> list[RagSnippet]:
        """Return up to ``k`` snippets ranked by cosine similarity.

        Returns ``[]`` (no exception) when the index or chunks file is
        missing — the orchestrator treats that as "no RAG context this
        turn", not as a fatal error.
        """
        if self._unavailable:
            return []
        if not query or not query.strip():
            return []

        try:
            self._ensure_loaded()
        except FileNotFoundError as exc:
            logger.warning(
                "RAG retriever disabled — index/chunks missing: %s "
                "(returning [] for all subsequent queries)",
                exc,
            )
            self._unavailable = True
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "RAG retriever disabled after load error: %s "
                "(returning [] for all subsequent queries)",
                exc,
            )
            self._unavailable = True
            return []

        if self._chunks is None or self._index is None or not self._chunks:
            return []

        import numpy as np  # local import — only needed at query time

        clamped_k = max(1, min(k, len(self._chunks)))
        vec = self._encoder.encode(
            [query], normalize_embeddings=True, show_progress_bar=False
        )
        vec = np.asarray(vec, dtype="float32")
        scores, ids = self._index.search(vec, clamped_k)

        snippets: list[RagSnippet] = []
        for score, idx in zip(scores[0].tolist(), ids[0].tolist()):
            if idx < 0 or idx >= len(self._chunks):
                continue
            row = self._chunks[idx]
            snippets.append(
                RagSnippet(
                    snippet_id=row.get("chunk_id", f"chunk_{idx}"),
                    source=row.get("source", "unknown"),
                    section=row.get("section", ""),
                    text=row.get("text", ""),
                    score=float(score),
                )
            )
        return snippets

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if self._encoder is not None and self._index is not None and self._chunks is not None:
            return

        if not self.index_path.exists():
            raise FileNotFoundError(f"FAISS index not found at {self.index_path}")
        if not self.chunks_path.exists():
            raise FileNotFoundError(f"Chunks file not found at {self.chunks_path}")

        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import]
            import faiss  # type: ignore[import]
        except ImportError as exc:  # pragma: no cover - environmental
            raise RuntimeError(
                "RAG runtime requires sentence-transformers and faiss-cpu."
            ) from exc

        if self._encoder is None:
            logger.info("Loading embedding model: %s", self.embedding_model)
            self._encoder = SentenceTransformer(self.embedding_model)

        if self._index is None:
            logger.info("Loading FAISS index from %s", self.index_path)
            self._index = faiss.read_index(str(self.index_path))

        if self._chunks is None:
            chunks: list[dict] = []
            with self.chunks_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    chunks.append(json.loads(line))
            self._chunks = chunks


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------


_DEFAULT_RETRIEVER: Retriever | None = None


def retrieve(query: str, k: int = 5) -> list[RagSnippet]:
    """Default-configured one-shot retrieval against the project index.

    Useful for ad-hoc / scripted lookups; production code should
    instantiate ``Retriever`` once and reuse it (the orchestrator does
    this).
    """
    global _DEFAULT_RETRIEVER
    if _DEFAULT_RETRIEVER is None:
        _DEFAULT_RETRIEVER = Retriever()
    return _DEFAULT_RETRIEVER.retrieve(query, k=k)
