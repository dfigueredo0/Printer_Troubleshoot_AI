"""
rag.py — RAG pipeline for ZT411 Troubleshooter Agent.

Responsibilities
----------------
- Embed a query and retrieve top-k chunks from a FAISS index.
- Fetch full chunk metadata from an SQLite docstore.
- Deduplicate overlapping or near-identical snippets.
- Reranking stub (cross-encoder ready when enabled).
- Query-level cache (hash → list[RagSnippet]) to avoid redundant inference.
- Prompt injection protection: strip instruction-like patterns from snippet text
  before returning to the planner.

Public API
----------
    rag = RAGPipeline.from_config(cfg)
    snippets: list[RagSnippet] = rag.retrieve(query)

The returned snippets are safe to pass directly to the planner prompt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Re-export RagSnippet so callers only need to import from rag
# ---------------------------------------------------------------------------
from ..planner import RagSnippet  # noqa: E402 (after stdlib imports)


# ---------------------------------------------------------------------------
# Prompt injection protection
# ---------------------------------------------------------------------------

# Base patterns — expanded by config at runtime
_BASE_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    # "ignore / disregard / forget previous instructions"
    re.compile(r"(ignore|disregard|forget).{0,40}(previous|above|prior|all).{0,40}instruction", re.I),
    # "system prompt", "system message"
    re.compile(r"\bsystem\s*(prompt|message)\b", re.I),
    # Jailbreak openers
    re.compile(r"\b(you are now|act as|pretend (to be|you are)|your new role)\b", re.I),
    # Markdown/code fences that could embed sub-instructions
    re.compile(r"```[\s\S]*?```"),
    # Explicit override attempts
    re.compile(r"\b(override|bypass|disable).{0,30}(filter|guard|rule|check|protection)\b", re.I),
]

# Dangerous action verbs in retrieved text that should be redacted
_DANGEROUS_ACTION_RE = re.compile(
    r"\b(delete|format|shutdown|rm\s+-rf|del\s+/[sq]|erase|wipe|factory.?reset)\b",
    re.I,
)

# Allowlist: matched text is kept even if it looks instruction-like
_ALLOWLIST_SOURCES = {"zebra", "internal_kb"}
_ALLOWLIST_CONTENT_RE = re.compile(
    r"\b(restart|resume|clear|reset\s+queue|calibrate|feed|cancel\s+job)\b", re.I
)


def _sanitise_snippet(text: str, source: str, extra_patterns: list[re.Pattern[str]] | None = None) -> str:
    """
    Strip prompt-injection patterns from a retrieved snippet.

    Allowlisted sources bypass *dangerous action* removal but still have
    instruction-override patterns stripped — a doc can tell the printer to
    restart without being allowed to tell the LLM to ignore its rules.
    """
    # Always strip override / jailbreak attempts
    all_patterns = _BASE_INJECTION_PATTERNS + (extra_patterns or [])
    for pat in all_patterns:
        text = pat.sub("[redacted]", text)

    # Strip dangerous action verbs unless the source is allowlisted
    if source not in _ALLOWLIST_SOURCES:
        text = _DANGEROUS_ACTION_RE.sub("[redacted]", text)

    return text


# ---------------------------------------------------------------------------
# In-memory query cache
# ---------------------------------------------------------------------------

@dataclass
class _CacheEntry:
    snippets: list[RagSnippet]


class _QueryCache:
    """Simple LRU-ish dict cache keyed on (query_hash, top_k)."""

    def __init__(self, max_size: int = 256) -> None:
        self._store: dict[str, _CacheEntry] = {}
        self._max_size = max_size

    @staticmethod
    def _key(query: str, top_k: int) -> str:
        digest = hashlib.sha256(query.encode()).hexdigest()[:16]
        return f"{digest}:{top_k}"

    def get(self, query: str, top_k: int) -> list[RagSnippet] | None:
        entry = self._store.get(self._key(query, top_k))
        return entry.snippets if entry else None

    def put(self, query: str, top_k: int, snippets: list[RagSnippet]) -> None:
        if len(self._store) >= self._max_size:
            # Evict oldest key (insertion-order in Python 3.7+)
            oldest = next(iter(self._store))
            del self._store[oldest]
        self._store[self._key(query, top_k)] = _CacheEntry(snippets=snippets)

    def invalidate(self) -> None:
        self._store.clear()


# ---------------------------------------------------------------------------
# Disk cache (pre-built snapshots for tier-0)
# ---------------------------------------------------------------------------

def _disk_cache_path(cache_dir: Path, query: str, top_k: int) -> Path:
    digest = hashlib.sha256(query.encode()).hexdigest()[:24]
    return cache_dir / f"{digest}_{top_k}.json"


def _load_disk_cache(path: Path) -> list[RagSnippet] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return [RagSnippet(**item) for item in data]
    except Exception as exc:
        logger.debug("Disk cache read failed (%s): %s", path, exc)
        return None


def _save_disk_cache(path: Path, snippets: list[RagSnippet]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([s.__dict__ for s in snippets], indent=2))
    except Exception as exc:
        logger.debug("Disk cache write failed (%s): %s", path, exc)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _dedup(snippets: list[RagSnippet], sim_threshold: float = 0.92) -> list[RagSnippet]:
    """
    Remove near-duplicate snippets using token-set overlap (Jaccard).

    More expensive cross-encoder dedup can replace this when reranking
    is enabled; for now this is fast and good enough.
    """
    seen: list[set[str]] = []
    unique: list[RagSnippet] = []

    for s in snippets:
        tokens = set(s.text.lower().split())
        is_dup = any(
            len(tokens & prev) / max(len(tokens | prev), 1) >= sim_threshold
            for prev in seen
        )
        if not is_dup:
            seen.append(tokens)
            unique.append(s)

    return unique


# ---------------------------------------------------------------------------
# Reranking stub
# ---------------------------------------------------------------------------

def _rerank(snippets: list[RagSnippet], query: str) -> list[RagSnippet]:
    """
    Reranking stub — returns snippets unchanged.

    Drop-in replacement: swap for a cross-encoder (e.g. cross-encoder/ms-marco-MiniLM-L-6-v2)
    once `rerank.enable: true` is set in config.  The cross-encoder should score
    (query, snippet.text) pairs and re-sort by that score.
    """
    return snippets


# ---------------------------------------------------------------------------
# RAGPipeline
# ---------------------------------------------------------------------------

class RAGPipeline:
    """
    Retrieval-Augmented Generation pipeline.

    Parameters
    ----------
    faiss_index_path : Path
    docstore_path    : Path
    embedding_model  : str   (sentence-transformers model name)
    top_k            : int   how many candidates to fetch from FAISS
    max_snippets     : int   max returned after dedup + rerank
    min_score        : float minimum cosine similarity to include
    cache_dir        : Path | None  disk cache root; None disables disk cache
    enable_cache     : bool  enable/disable in-memory + disk cache
    rerank_enable    : bool  reserved for future cross-encoder
    injection_extra_patterns : list[str] extra regex patterns from config
    """

    def __init__(
        self,
        faiss_index_path: Path,
        docstore_path: Path,
        embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2",
        top_k: int = 6,
        max_snippets: int = 8,
        min_score: float = 0.25,
        cache_dir: Path | None = None,
        enable_cache: bool = True,
        rerank_enable: bool = False,
        injection_extra_patterns: list[str] | None = None,
    ) -> None:
        self.faiss_index_path = faiss_index_path
        self.docstore_path = docstore_path
        self.embedding_model_name = embedding_model
        self.top_k = top_k
        self.max_snippets = max_snippets
        self.min_score = min_score
        self.cache_dir = cache_dir
        self.enable_cache = enable_cache
        self.rerank_enable = rerank_enable

        # Compile extra injection patterns from config
        self._extra_patterns: list[re.Pattern[str]] = []
        for raw in injection_extra_patterns or []:
            try:
                self._extra_patterns.append(re.compile(raw, re.I))
            except re.error as exc:
                logger.warning("Invalid injection pattern '%s': %s", raw, exc)

        self._mem_cache = _QueryCache()
        self._encoder: Any = None   # lazy-loaded
        self._index: Any = None     # lazy-loaded faiss index
        self._db_conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------
    # Lazy resource loading
    # ------------------------------------------------------------------

    def _get_encoder(self) -> Any:
        if self._encoder is None:
            try:
                from sentence_transformers import SentenceTransformer
                logger.info("Loading embedding model: %s", self.embedding_model_name)
                self._encoder = SentenceTransformer(self.embedding_model_name)
            except ImportError as exc:
                raise RuntimeError(
                    "sentence-transformers is required for RAG. "
                    "Install it with: pip install sentence-transformers"
                ) from exc
        return self._encoder

    def _get_index(self) -> Any:
        if self._index is None:
            import faiss  # type: ignore[import]
            if not self.faiss_index_path.exists():
                raise FileNotFoundError(
                    f"FAISS index not found at {self.faiss_index_path}. "
                    "Run `python -m zt411_agent.data.make_dataset` to build it."
                )
            logger.info("Loading FAISS index from %s", self.faiss_index_path)
            self._index = faiss.read_index(str(self.faiss_index_path))
        return self._index

    def _get_db(self) -> sqlite3.Connection:
        if self._db_conn is None:
            if not self.docstore_path.exists():
                raise FileNotFoundError(
                    f"Docstore not found at {self.docstore_path}. "
                    "Run `python -m zt411_agent.data.make_dataset` to build it."
                )
            self._db_conn = sqlite3.connect(str(self.docstore_path), check_same_thread=False)
            self._db_conn.row_factory = sqlite3.Row
        return self._db_conn

    # ------------------------------------------------------------------
    # Core retrieval
    # ------------------------------------------------------------------

    def _embed(self, text: str) -> np.ndarray:
        enc = self._get_encoder()
        vec = enc.encode([text], normalize_embeddings=True)
        return vec.astype("float32")

    def _faiss_search(self, query_vec: np.ndarray) -> tuple[list[float], list[int]]:
        index = self._get_index()
        k = min(self.top_k * 2, index.ntotal)  # fetch extra for dedup headroom
        if k == 0:
            return [], []
        distances, indices = index.search(query_vec, k)
        return distances[0].tolist(), indices[0].tolist()

    def _fetch_from_docstore(self, faiss_ids: list[int]) -> list[dict]:
        """Fetch rows from docstore by FAISS row index."""
        if not faiss_ids:
            return []
        db = self._get_db()
        placeholders = ",".join("?" * len(faiss_ids))
        rows = db.execute(
            f"SELECT * FROM chunks WHERE faiss_id IN ({placeholders})",
            faiss_ids,
        ).fetchall()
        # Return as dicts keyed by faiss_id for score merge
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Public retrieve
    # ------------------------------------------------------------------

    def retrieve(self, query: str) -> list[RagSnippet]:
        """
        Retrieve, deduplicate, (re)rank, and sanitise relevant snippets for query.

        Returns at most `max_snippets` RagSnippet objects with score >= min_score.
        Results are served from cache when available.
        """
        # 1. Memory cache
        if self.enable_cache:
            cached = self._mem_cache.get(query, self.top_k)
            if cached is not None:
                logger.debug("RAG memory cache hit for query (len=%d)", len(query))
                return cached

        # 2. Disk cache (tier-0 pre-built snapshots)
        if self.enable_cache and self.cache_dir:
            disk_path = _disk_cache_path(self.cache_dir, query, self.top_k)
            from_disk = _load_disk_cache(disk_path)
            if from_disk is not None:
                logger.debug("RAG disk cache hit: %s", disk_path.name)
                self._mem_cache.put(query, self.top_k, from_disk)
                return from_disk

        # 3. Live retrieval
        try:
            snippets = self._live_retrieve(query)
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("RAG live retrieval unavailable: %s — returning empty snippets.", exc)
            return []

        # 4. Populate caches
        if self.enable_cache:
            self._mem_cache.put(query, self.top_k, snippets)
            if self.cache_dir:
                disk_path = _disk_cache_path(self.cache_dir, query, self.top_k)
                _save_disk_cache(disk_path, snippets)

        return snippets

    def _live_retrieve(self, query: str) -> list[RagSnippet]:
        query_vec = self._embed(query)
        distances, faiss_ids = self._faiss_search(query_vec)

        if not faiss_ids or faiss_ids[0] == -1:
            return []

        # FAISS inner-product on normalised vectors == cosine similarity
        score_by_id = {fid: float(dist) for fid, dist in zip(faiss_ids, distances) if fid != -1}
        valid_ids = [fid for fid in faiss_ids if fid != -1]

        rows = self._fetch_from_docstore(valid_ids)
        row_map = {r["faiss_id"]: r for r in rows}

        snippets: list[RagSnippet] = []
        for fid in valid_ids:
            score = score_by_id.get(fid, 0.0)
            if score < self.min_score:
                continue
            row = row_map.get(fid)
            if row is None:
                continue

            source = row.get("source", "unknown")
            clean_text = _sanitise_snippet(
                row.get("text", ""),
                source=source,
                extra_patterns=self._extra_patterns,
            )

            snippets.append(RagSnippet(
                snippet_id=row.get("snippet_id", f"chunk_{fid}"),
                source=source,
                section=row.get("section", ""),
                text=clean_text,
                score=score,
            ))

        # Deduplicate
        snippets = _dedup(snippets)

        # Optional reranking (stub — no-op until cross-encoder is wired)
        if self.rerank_enable:
            snippets = _rerank(snippets, query)

        # Cap and sort by score
        snippets.sort(key=lambda s: s.score, reverse=True)
        return snippets[: self.max_snippets]

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: Any) -> "RAGPipeline":
        """
        Construct a RAGPipeline from the loaded Settings / runtime config object.

        Handles both the Pydantic settings model and a plain dict.
        """
        def _get(obj: Any, *keys: str, default: Any = None) -> Any:
            for key in keys:
                if isinstance(obj, dict):
                    obj = obj.get(key, default)
                else:
                    obj = getattr(obj, key, default)
                if obj is None:
                    return default
            return obj

        rag_cfg = _get(cfg, "rag") or {}

        base_dir = Path("zt411-troubleshooter-agent")  # project root convention
        faiss_path = Path(_get(rag_cfg, "faiss_index_path", default="data/rag/faiss.index"))
        docstore_path = Path(_get(rag_cfg, "docstore_path", default="data/rag/docstore.sqlite"))
        embedding_model = _get(rag_cfg, "embedding_model", default="sentence-transformers/all-MiniLM-L6-v2")
        top_k = int(_get(rag_cfg, "top_k", default=6))
        max_snippets = int(_get(rag_cfg, "max_snippets", default=8))
        min_score = float(_get(rag_cfg, "min_score", default=0.25))

        cache_cfg = _get(rag_cfg, "cache") or {}
        enable_cache = bool(_get(cache_cfg, "enable", default=True))
        cache_dir_str = _get(cache_cfg, "dir", default="data/rag/cache/")
        cache_dir = Path(cache_dir_str) if enable_cache else None

        rerank_cfg = _get(rag_cfg, "rerank") or {}
        rerank_enable = bool(_get(rerank_cfg, "enable", default=False))

        inj_cfg = _get(rag_cfg, "prompt_injection_protection") or {}
        extra_patterns = _get(inj_cfg, "patterns", default=[])
        if not isinstance(extra_patterns, list):
            extra_patterns = []

        return cls(
            faiss_index_path=faiss_path,
            docstore_path=docstore_path,
            embedding_model=embedding_model,
            top_k=top_k,
            max_snippets=max_snippets,
            min_score=min_score,
            cache_dir=cache_dir,
            enable_cache=enable_cache,
            rerank_enable=rerank_enable,
            injection_extra_patterns=extra_patterns,
        )

    def close(self) -> None:
        """Release database connection."""
        if self._db_conn:
            self._db_conn.close()
            self._db_conn = None
