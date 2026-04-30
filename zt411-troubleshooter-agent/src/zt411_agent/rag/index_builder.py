"""
RAG index builder for the ZT411 troubleshooter.

Inputs
------
* PDFs under ``data/raw/zebra/`` (and any other directories passed via
  ``--source-dir``).
* Plain-text or markdown files under ``tests/fixtures/rag_corpus/`` for
  the test suite (used via the ``--source-dir`` flag).

Outputs
-------
* ``data/rag_corpus/index.faiss``  — FAISS IndexFlatIP (inner-product on
  normalised vectors == cosine similarity).
* ``data/rag_corpus/chunks.jsonl`` — one JSON line per chunk with
  ``{chunk_id, source, section, page, text}`` keys. Line index of a
  chunk corresponds 1:1 to its position in the FAISS index.
* ``data/rag_corpus/MANIFEST.md``  — human-readable inventory of source
  files and chunk counts produced.

Embedding model
---------------
sentence-transformers ``all-MiniLM-L6-v2`` — 384 dim, ~80MB, runs offline
on CPU in <100ms per chunk. Selected because: (1) project already depends
on sentence-transformers; (2) it's the smallest model with adequate
recall for technical English without GPU; (3) cosine similarity is well-
calibrated for short technical snippets vs. user queries phrased as
symptoms.

CLI
---
::

    python -m zt411_agent.rag.index_builder --rebuild

The build is idempotent: if ``index.faiss`` and ``chunks.jsonl`` already
exist and ``--rebuild`` is not passed, the call is a no-op.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_SOURCE_DIRS: tuple[str, ...] = (
    "data/raw/zebra",
    "data/raw/internal_kb",
)
DEFAULT_OUTPUT_DIR = "data/rag_corpus"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# Roughly 2000 chars per chunk at ~4 chars per token ≈ 500 tokens, the
# planner prompt's preferred snippet size.
DEFAULT_CHUNK_CHARS = 2000
DEFAULT_CHUNK_OVERLAP = 200


@dataclass
class Chunk:
    chunk_id: str
    source: str
    section: str
    page: int
    text: str


# ---------------------------------------------------------------------------
# PDF and plain-text loaders
# ---------------------------------------------------------------------------


def _iter_source_files(source_dirs: Iterable[Path]) -> list[Path]:
    """Return PDF + .md + .txt files under each source dir, sorted."""
    suffixes = {".pdf", ".md", ".txt"}
    files: list[Path] = []
    for d in source_dirs:
        if not d.exists():
            logger.debug("Source dir %s does not exist; skipping.", d)
            continue
        for path in sorted(d.rglob("*")):
            if path.is_file() and path.suffix.lower() in suffixes:
                files.append(path)
    return files


def _extract_pdf_pages(path: Path) -> list[tuple[int, str]]:
    """Yield (page_number, text) for each page of a PDF.

    pymupdf is the project default. If it's missing we fail loudly —
    not silently — because the index won't be useful without text.
    """
    try:
        import fitz  # type: ignore[import]
    except ImportError as exc:  # pragma: no cover - environmental
        raise RuntimeError(
            "pymupdf is required to build the RAG index. "
            "Install it with: pip install pymupdf"
        ) from exc

    pages: list[tuple[int, str]] = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc):
            text = page.get_text("text") or ""
            pages.append((i + 1, text))
    return pages


def _extract_text_pages(path: Path) -> list[tuple[int, str]]:
    """Plain text / markdown is a single 'page' for chunking purposes."""
    return [(1, path.read_text(encoding="utf-8", errors="replace"))]


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

_PARAGRAPH_BOUNDARY = re.compile(r"\n\s*\n+")


def _chunk_text(
    text: str,
    *,
    chunk_chars: int,
    overlap: int,
) -> list[str]:
    """Greedy paragraph-aware chunker targeting ``chunk_chars`` per chunk.

    We split on paragraph boundaries first (so chunks rarely cleave a
    sentence), then concatenate paragraphs until the chunk is at least
    ``chunk_chars`` long. ``overlap`` characters from the end of one
    chunk are prepended to the next so context isn't lost at boundaries.
    Empty paragraphs are dropped.
    """
    if not text or not text.strip():
        return []

    paragraphs = [p.strip() for p in _PARAGRAPH_BOUNDARY.split(text) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if not current:
            current = para
        elif len(current) + len(para) + 2 <= chunk_chars:
            current = f"{current}\n\n{para}"
        else:
            chunks.append(current)
            tail = current[-overlap:] if overlap > 0 and len(current) > overlap else ""
            current = f"{tail}\n\n{para}" if tail else para

    if current:
        chunks.append(current)

    # Final pass: split any chunk that ended up larger than chunk_chars
    # because a single paragraph was huge. Hard split on chunk_chars.
    final: list[str] = []
    for c in chunks:
        if len(c) <= chunk_chars:
            final.append(c)
            continue
        for i in range(0, len(c), chunk_chars - overlap):
            final.append(c[i : i + chunk_chars])
    return final


# ---------------------------------------------------------------------------
# Source-aware metadata
# ---------------------------------------------------------------------------


def _describe_source(path: Path, project_root: Path) -> tuple[str, str]:
    """Return (source, section) suitable for the MANIFEST and snippets.

    ``source`` is the family the chunk belongs to (e.g. ``zebra`` for the
    user-facing manuals, ``internal_kb`` for engineer notes), so the
    planner can weight allowlists appropriately.
    ``section`` is the file stem.
    """
    try:
        rel = path.relative_to(project_root)
    except ValueError:
        rel = path
    parts = rel.parts
    if "raw" in parts:
        idx = parts.index("raw")
        source = parts[idx + 1] if idx + 1 < len(parts) else "unknown"
    elif "rag_corpus" in parts:
        idx = parts.index("rag_corpus")
        source = parts[idx + 1] if idx + 1 < len(parts) else "fixtures"
    else:
        source = path.parent.name or "unknown"
    section = path.stem
    return source, section


def _chunk_id(source: str, section: str, ordinal: int) -> str:
    """Stable id of the form ``<source>_<section>_<ordinal>`` (4-digit
    zero-padded ordinal). Stable across re-runs of the builder so cached
    citations from earlier sessions stay meaningful unless the source
    text changes.
    """
    safe_section = re.sub(r"[^A-Za-z0-9_-]+", "-", section).strip("-")
    return f"{source}_{safe_section}_{ordinal:04d}"


# ---------------------------------------------------------------------------
# Build pipeline
# ---------------------------------------------------------------------------


def _project_root(start: Path | None = None) -> Path:
    """Best-effort location of the project root.

    Defaults to the repo's checked-in layout: this file is at
    ``zt411-troubleshooter-agent/src/zt411_agent/rag/index_builder.py``
    so the root is four levels up. Falls back to CWD.
    """
    here = (start or Path(__file__)).resolve()
    candidate = here.parent.parent.parent.parent  # → zt411-troubleshooter-agent/
    if (candidate / "src" / "zt411_agent").exists():
        return candidate
    return Path.cwd()


def collect_chunks(
    source_dirs: list[Path],
    *,
    project_root: Path,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[Chunk]:
    """Walk ``source_dirs``, extract text, chunk, and return ``Chunk`` records.

    Exposed as a public helper so tests can drive the pipeline against a
    small fixture corpus without invoking the CLI.
    """
    files = _iter_source_files(source_dirs)
    if not files:
        logger.warning("No source files found under: %s", source_dirs)
        return []

    chunks: list[Chunk] = []
    for path in files:
        source, section = _describe_source(path, project_root)
        suffix = path.suffix.lower()
        try:
            pages = (
                _extract_pdf_pages(path)
                if suffix == ".pdf"
                else _extract_text_pages(path)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read %s: %s", path, exc)
            continue

        for page_no, page_text in pages:
            for chunk_text in _chunk_text(
                page_text, chunk_chars=chunk_chars, overlap=overlap
            ):
                ordinal = len(chunks)
                chunks.append(
                    Chunk(
                        chunk_id=_chunk_id(source, section, ordinal),
                        source=source,
                        section=section,
                        page=page_no,
                        text=chunk_text,
                    )
                )

    logger.info("Collected %d chunks from %d files", len(chunks), len(files))
    return chunks


def build_index(
    chunks: list[Chunk],
    output_dir: Path,
    *,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
) -> None:
    """Embed chunks and write the FAISS index + chunks.jsonl + MANIFEST.

    The FAISS index is ``IndexFlatIP`` over L2-normalised vectors,
    so inner-product == cosine similarity. With <100k chunks this is
    fast enough on CPU; switch to ``IndexHNSWFlat`` only if the corpus
    grows by an order of magnitude.
    """
    if not chunks:
        raise ValueError("No chunks to index — refusing to write empty index.")

    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import]
        import faiss  # type: ignore[import]
        import numpy as np
    except ImportError as exc:  # pragma: no cover - environmental
        raise RuntimeError(
            "sentence-transformers, faiss-cpu, and numpy are required. "
            f"Original error: {exc}"
        ) from exc

    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Loading embedding model: %s", embedding_model)
    encoder = SentenceTransformer(embedding_model)
    texts = [c.text for c in chunks]

    logger.info("Embedding %d chunks ...", len(texts))
    vectors = encoder.encode(
        texts,
        batch_size=32,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    vectors = np.asarray(vectors, dtype="float32")
    if vectors.ndim != 2:
        raise RuntimeError(f"Unexpected embedding shape: {vectors.shape}")

    dim = vectors.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(vectors)

    index_path = output_dir / "index.faiss"
    chunks_path = output_dir / "chunks.jsonl"
    manifest_path = output_dir / "MANIFEST.md"

    faiss.write_index(index, str(index_path))
    with chunks_path.open("w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(asdict(c), ensure_ascii=False) + "\n")

    _write_manifest(chunks, manifest_path, embedding_model=embedding_model, dim=dim)

    logger.info(
        "Index built: %d chunks, dim=%d → %s",
        len(chunks), dim, output_dir,
    )


def _write_manifest(
    chunks: list[Chunk],
    path: Path,
    *,
    embedding_model: str,
    dim: int,
) -> None:
    """One-line-per-source roll-up with chunk counts. Useful for humans
    diagnosing why a query failed to retrieve relevant context.
    """
    sources: dict[str, dict[str, int]] = {}
    for c in chunks:
        bucket = sources.setdefault(c.source, {})
        bucket[c.section] = bucket.get(c.section, 0) + 1

    lines = [
        "# RAG Corpus Manifest",
        "",
        f"Embedding model: `{embedding_model}` (dim={dim})",
        f"Total chunks   : {len(chunks)}",
        "",
        "## Sources",
        "",
    ]
    for source, sections in sorted(sources.items()):
        total = sum(sections.values())
        lines.append(f"- **{source}** — {total} chunks across {len(sections)} file(s)")
        for section, count in sorted(sections.items()):
            lines.append(f"  - `{section}` — {count} chunks")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the ZT411 RAG corpus index."
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Force a rebuild even if an index already exists.",
    )
    parser.add_argument(
        "--source-dir",
        action="append",
        default=None,
        help=(
            "Add a source directory (relative to project root or absolute). "
            "Can be passed multiple times. Defaults to "
            "data/raw/zebra and data/raw/internal_kb."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUTPUT_DIR,
        help="Where to write index.faiss + chunks.jsonl + MANIFEST.md.",
    )
    parser.add_argument(
        "--embedding-model",
        default=DEFAULT_EMBEDDING_MODEL,
        help="sentence-transformers model id.",
    )
    parser.add_argument(
        "--chunk-chars",
        type=int,
        default=DEFAULT_CHUNK_CHARS,
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=DEFAULT_CHUNK_OVERLAP,
    )
    parser.add_argument(
        "--project-root",
        default=None,
        help="Override project root (defaults to autodetect).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)

    root = Path(args.project_root).resolve() if args.project_root else _project_root()
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = root / output_dir

    src_dirs_raw = args.source_dir or list(DEFAULT_SOURCE_DIRS)
    source_dirs = [
        Path(d) if Path(d).is_absolute() else root / d
        for d in src_dirs_raw
    ]

    index_path = output_dir / "index.faiss"
    chunks_path = output_dir / "chunks.jsonl"
    if index_path.exists() and chunks_path.exists() and not args.rebuild:
        logger.info(
            "Index already exists at %s — pass --rebuild to overwrite.",
            output_dir,
        )
        return 0

    chunks = collect_chunks(
        source_dirs,
        project_root=root,
        chunk_chars=args.chunk_chars,
        overlap=args.chunk_overlap,
    )
    if not chunks:
        logger.error(
            "No chunks collected from %s; refusing to write empty index.",
            source_dirs,
        )
        return 1

    build_index(
        chunks,
        output_dir,
        embedding_model=args.embedding_model,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
