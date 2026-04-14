"""
build_offline_cache.py — Pre-build and ship FAISS index + docstore for tier-0 operation.

Run this script before packaging a field kit or offline installer.
It calls make_dataset.ingest() and then warms the query cache for a set of
common diagnostic queries so tier-0 cold starts are instant.

Usage
-----
    python scripts/build_offline_cache.py

Output
------
    data/rag/faiss.index      — FAISS index (live path, also used by cloud/local tiers)
    data/rag/docstore.sqlite  — SQLite docstore
    data/rag/snapshot/        — Frozen copy for shipping in the installer
    data/rag/cache/           — Pre-warmed query cache (JSON files)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Make sure the src package is importable when run from project root
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from zt411_agent.data.make_dataset import ingest
from zt411_agent.agent.rag import RAGPipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths (relative to zt411-troubleshooter-agent/)
# ---------------------------------------------------------------------------
BASE = Path(__file__).parent.parent   # zt411-troubleshooter-agent/
SOURCE_DIR   = BASE / "data" / "raw"
FAISS_PATH   = BASE / "data" / "rag" / "faiss.index"
DB_PATH      = BASE / "data" / "rag" / "docstore.sqlite"
SNAPSHOT_DIR = BASE / "data" / "rag" / "snapshot"
CACHE_DIR    = BASE / "data" / "rag" / "cache"

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

# ---------------------------------------------------------------------------
# Common ZT411 diagnostic queries to pre-warm the cache
# ---------------------------------------------------------------------------
WARMUP_QUERIES = [
    "ZT411 media out error",
    "ZT411 ribbon out sensor",
    "ZT411 printhead open",
    "ZT411 calibration procedure",
    "ZT411 CUPS queue paused",
    "ZT411 network connection refused port 9100",
    "ZT411 ZPL label not printing",
    "ZT411 firmware update procedure",
    "ZT411 Windows driver installation",
    "ZT411 print quality darkness setting",
    "ZT411 head element failure",
    "ZT411 pause button behavior",
    "ZT411 IP address configuration",
    "ZT411 USB connection troubleshoot",
    "ZT411 spooler stuck Windows",
]


def main() -> None:
    # 1. Build / rebuild the index from source documents
    logger.info("=== Step 1: Ingesting documents ===")
    total = ingest(
        source_dir=SOURCE_DIR,
        faiss_path=FAISS_PATH,
        db_path=DB_PATH,
        snapshot_dir=SNAPSHOT_DIR,
        embedding_model=EMBEDDING_MODEL,
    )
    logger.info("Ingested %d chunks.", total)

    if total == 0:
        logger.warning(
            "No source documents found in %s. "
            "Add PDFs/text files before building the offline cache.",
            SOURCE_DIR,
        )

    # 2. Warm the query cache
    logger.info("=== Step 2: Warming query cache ===")
    rag = RAGPipeline(
        faiss_index_path=FAISS_PATH,
        docstore_path=DB_PATH,
        embedding_model=EMBEDDING_MODEL,
        cache_dir=CACHE_DIR,
        enable_cache=True,
    )

    for query in WARMUP_QUERIES:
        snippets = rag.retrieve(query)
        logger.info("  [%d snippets] %s", len(snippets), query)

    rag.close()

    logger.info("=== Offline cache build complete ===")
    logger.info("  FAISS index : %s", FAISS_PATH)
    logger.info("  Docstore    : %s", DB_PATH)
    logger.info("  Snapshot    : %s", SNAPSHOT_DIR)
    logger.info("  Cache       : %s", CACHE_DIR)
    logger.info("")
    logger.info("Ship `data/rag/snapshot/` + `data/rag/cache/` in the installer for tier-0 operation.")


if __name__ == "__main__":
    main()
