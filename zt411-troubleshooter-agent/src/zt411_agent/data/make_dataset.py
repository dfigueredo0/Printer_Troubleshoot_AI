"""
make_dataset.py — Document ingestion pipeline for the ZT411 RAG store.

What it does
------------
1. Scans `data/raw/` for source documents:
   - Plain text (.txt), Markdown (.md) — read directly.
   - PDF (.pdf) — extracted via PyMuPDF (fitz) if available, else pdfminer.
2. Chunks each document with configurable size + overlap.
3. Embeds chunks using sentence-transformers/all-MiniLM-L6-v2.
4. Builds a FAISS IndexFlatIP (inner-product on normalised vectors = cosine).
5. Persists chunk metadata to an SQLite docstore.
6. Writes a `data/rag/snapshot/` directory containing frozen copies of the
   FAISS index + docstore for tier-0 (fully offline) operation.

Usage
-----
    # From the zt411-troubleshooter-agent/ directory:
    python -m zt411_agent.data.make_dataset

    # With custom source dir and output paths:
    python -m zt411_agent.data.make_dataset \\
        --source data/raw \\
        --faiss  data/rag/faiss.index \\
        --db     data/rag/docstore.sqlite \\
        --snapshot data/rag/snapshot

Source document conventions
----------------------------
Place files under `data/raw/`:
  data/raw/zebra/          — ZT411 product manuals, ZPL reference, etc.
  data/raw/internal_kb/    — Internal KB articles, SOPs, field notes.

The subdirectory name becomes the `source` tag on every chunk from that directory.
Files in the root of data/raw/ are tagged as source="general".
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Iterator

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

_DEFAULT_CHUNK_SIZE = 400      # words
_DEFAULT_CHUNK_OVERLAP = 80    # words


def _iter_words(text: str) -> list[str]:
    return text.split()


def chunk_text(
    text: str,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    overlap: int = _DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """
    Split text into overlapping word-count windows.

    Returns a list of chunk strings.  Empty input → empty list.
    """
    words = _iter_words(text)
    if not words:
        return []

    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunks.append(" ".join(words[start:end]))
        if end == len(words):
            break
        start += chunk_size - overlap

    return chunks


# ---------------------------------------------------------------------------
# Document readers
# ---------------------------------------------------------------------------

def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _read_pdf(path: Path) -> str:
    """Extract plain text from a PDF.  Tries PyMuPDF first, falls back to pdfminer."""
    try:
        import pymupdf  # PyMuPDF
        doc = pymupdf.open(str(path))
        pages = []
        for page in doc:
            text = page.get_text()
            if text:
                pages.append(text)
        return "\n".join(pages)
    except ImportError:
        pass

    try:
        from pdfminer.high_level import extract_text as pdfminer_extract
        return pdfminer_extract(str(path))
    except ImportError:
        logger.warning(
            "PDF reading skipped for %s — install pymupdf or pdfminer.six: pip install pymupdf",
            path,
        )
        return ""


def read_document(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".rst"}:
        return _read_text(path)
    if suffix == ".pdf":
        return _read_pdf(path)
    logger.debug("Skipping unsupported file type: %s", path)
    return ""


# ---------------------------------------------------------------------------
# Source tag from directory structure
# ---------------------------------------------------------------------------

def _source_tag(path: Path, source_root: Path) -> str:
    """
    Map a file path to a human-readable source tag.

    data/raw/zebra/manual.pdf      → "zebra"
    data/raw/internal_kb/sop.txt   → "internal_kb"
    data/raw/notes.txt             → "general"
    """
    try:
        relative = path.relative_to(source_root)
        parts = relative.parts
        if len(parts) > 1:
            return parts[0]
    except ValueError:
        pass
    return "general"


def _section_hint(path: Path) -> str:
    """Best-effort section name from filename (without extension)."""
    return path.stem.replace("_", " ").replace("-", " ")


# ---------------------------------------------------------------------------
# SQLite docstore schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    faiss_id    INTEGER PRIMARY KEY,
    snippet_id  TEXT    NOT NULL UNIQUE,
    source      TEXT    NOT NULL,
    section     TEXT    NOT NULL,
    file_path   TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    char_offset INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_source ON chunks(source);
CREATE INDEX IF NOT EXISTS idx_snippet_id ON chunks(snippet_id);
"""


def _open_docstore(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _insert_chunk(
    conn: sqlite3.Connection,
    faiss_id: int,
    snippet_id: str,
    source: str,
    section: str,
    file_path: str,
    text: str,
    char_offset: int,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO chunks
            (faiss_id, snippet_id, source, section, file_path, text, char_offset)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (faiss_id, snippet_id, source, section, file_path, text, char_offset),
    )


# ---------------------------------------------------------------------------
# Embedding helper
# ---------------------------------------------------------------------------

def _load_encoder(model_name: str) -> Any:
    try:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model: %s", model_name)
        return SentenceTransformer(model_name)
    except ImportError as exc:
        raise SystemExit(
            "sentence-transformers is required. Install: pip install sentence-transformers"
        ) from exc


from typing import Any  # already in stdlib; placed here to satisfy forward ref above


def _embed_batch(encoder: Any, texts: list[str], batch_size: int = 64) -> np.ndarray:
    vecs = encoder.encode(texts, batch_size=batch_size, normalize_embeddings=True, show_progress_bar=True)
    return vecs.astype("float32")


# ---------------------------------------------------------------------------
# FAISS builder
# ---------------------------------------------------------------------------

def _build_faiss_index(vectors: np.ndarray) -> Any:
    try:
        import faiss  # type: ignore[import]
    except ImportError as exc:
        raise SystemExit(
            "faiss-cpu is required. Install: pip install faiss-cpu"
        ) from exc

    dim = vectors.shape[1]
    index = faiss.IndexFlatIP(dim)   # inner-product on unit vectors == cosine
    index.add(vectors) # type: ignore[arg-type]
    logger.info("FAISS index built: %d vectors, dim=%d", index.ntotal, dim)
    return index


# ---------------------------------------------------------------------------
# Main ingestion pipeline
# ---------------------------------------------------------------------------

def ingest(
    source_dir: Path,
    faiss_path: Path,
    db_path: Path,
    snapshot_dir: Path | None = None,
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2",
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = _DEFAULT_CHUNK_OVERLAP,
) -> int:
    """
    Full ingestion pipeline.

    Returns
    -------
    int
        Number of chunks written.
    """
    if not source_dir.exists():
        logger.warning("Source directory does not exist: %s — creating empty index.", source_dir)
        source_dir.mkdir(parents=True, exist_ok=True)

    # Collect all supported files
    doc_files: list[Path] = sorted(
        p for p in source_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in {".txt", ".md", ".rst", ".pdf"}
    )

    if not doc_files:
        logger.warning("No documents found in %s — writing empty index.", source_dir)

    encoder = _load_encoder(embedding_model)

    all_chunks: list[dict] = []   # {text, source, section, file_path, char_offset}

    for doc_path in doc_files:
        logger.info("Processing: %s", doc_path)
        raw_text = read_document(doc_path)
        if not raw_text.strip():
            logger.debug("  Skipped (empty text): %s", doc_path)
            continue

        source = _source_tag(doc_path, source_dir)
        section = _section_hint(doc_path)
        chunks = chunk_text(raw_text, chunk_size=chunk_size, overlap=chunk_overlap)

        char_offset = 0
        for chunk in chunks:
            all_chunks.append({
                "text": chunk,
                "source": source,
                "section": section,
                "file_path": str(doc_path),
                "char_offset": char_offset,
            })
            char_offset += len(chunk)

        logger.info("  %d chunks from %s", len(chunks), doc_path.name)

    if not all_chunks:
        logger.warning("No chunks to embed — writing empty FAISS index and docstore.")
        # Write empty artefacts so the pipeline doesn't crash at runtime
        _write_empty_artefacts(faiss_path, db_path, embedding_model, encoder)
        if snapshot_dir:
            _write_snapshot(faiss_path, db_path, snapshot_dir)
        return 0

    # Embed all chunks
    texts = [c["text"] for c in all_chunks]
    logger.info("Embedding %d chunks…", len(texts))
    vectors = _embed_batch(encoder, texts)

    # Build FAISS
    index = _build_faiss_index(vectors)

    # Persist FAISS index
    faiss_path.parent.mkdir(parents=True, exist_ok=True)
    import faiss as _faiss
    _faiss.write_index(index, str(faiss_path))
    logger.info("FAISS index written to %s", faiss_path)

    # Persist docstore
    conn = _open_docstore(db_path)
    for faiss_id, chunk in enumerate(all_chunks):
        # Stable snippet_id based on content hash
        content_hash = hashlib.sha256(chunk["text"].encode()).hexdigest()[:12]
        snippet_id = f"{chunk['source']}_{content_hash}"
        _insert_chunk(
            conn,
            faiss_id=faiss_id,
            snippet_id=snippet_id,
            source=chunk["source"],
            section=chunk["section"],
            file_path=chunk["file_path"],
            text=chunk["text"],
            char_offset=chunk["char_offset"],
        )
    conn.commit()
    conn.close()
    logger.info("Docstore written to %s (%d rows)", db_path, len(all_chunks))

    # Write offline snapshot
    if snapshot_dir:
        _write_snapshot(faiss_path, db_path, snapshot_dir)

    return len(all_chunks)


def _write_empty_artefacts(
    faiss_path: Path,
    db_path: Path,
    embedding_model: str,
    encoder: Any,
) -> None:
    """Write zero-vector FAISS index and empty docstore so imports don't fail."""
    import faiss as _faiss
    dim = encoder.get_sentence_embedding_dimension()
    index = _faiss.IndexFlatIP(dim)
    faiss_path.parent.mkdir(parents=True, exist_ok=True)
    _faiss.write_index(index, str(faiss_path))
    conn = _open_docstore(db_path)
    conn.commit()
    conn.close()


def _write_snapshot(faiss_path: Path, db_path: Path, snapshot_dir: Path) -> None:
    """
    Copy FAISS index + docstore into snapshot_dir for tier-0 offline shipping.

    The snapshot directory is intended to be bundled with the installer so
    field kits can run without internet access.
    """
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(faiss_path, snapshot_dir / faiss_path.name)
    shutil.copy2(db_path, snapshot_dir / db_path.name)
    logger.info("Offline snapshot written to %s", snapshot_dir)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build RAG FAISS index + SQLite docstore from raw documents."
    )
    p.add_argument(
        "--source",
        type=Path,
        default=Path("data/raw"),
        help="Root directory containing source documents (default: data/raw)",
    )
    p.add_argument(
        "--faiss",
        type=Path,
        default=Path("data/rag/faiss.index"),
        help="Output path for the FAISS index (default: data/rag/faiss.index)",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=Path("data/rag/docstore.sqlite"),
        help="Output path for the SQLite docstore (default: data/rag/docstore.sqlite)",
    )
    p.add_argument(
        "--snapshot",
        type=Path,
        default=Path("data/rag/snapshot"),
        help="Output directory for tier-0 offline snapshot (default: data/rag/snapshot)",
    )
    p.add_argument(
        "--model",
        default="sentence-transformers/all-MiniLM-L6-v2",
        help="Sentence-transformers model name",
    )
    p.add_argument(
        "--chunk-size",
        type=int,
        default=_DEFAULT_CHUNK_SIZE,
        help=f"Chunk size in words (default: {_DEFAULT_CHUNK_SIZE})",
    )
    p.add_argument(
        "--chunk-overlap",
        type=int,
        default=_DEFAULT_CHUNK_OVERLAP,
        help=f"Overlap between consecutive chunks in words (default: {_DEFAULT_CHUNK_OVERLAP})",
    )
    p.add_argument(
        "--no-snapshot",
        action="store_true",
        help="Skip writing the offline snapshot",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    snapshot_dir = None if args.no_snapshot else args.snapshot

    total = ingest(
        source_dir=args.source,
        faiss_path=args.faiss,
        db_path=args.db,
        snapshot_dir=snapshot_dir,
        embedding_model=args.model,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
    )

    print(f"Done — {total} chunks indexed.")


if __name__ == "__main__":
    main()
