"""
RAG pipeline tests (Phase 3 — Session C).

The fixture corpus lives at ``tests/fixtures/rag_corpus/`` (four small
markdown files covering pause/resume, head_open, media_out, ribbon_out).
We build a fresh index against that corpus into a tmp_path per test, so
tests are independent of any prior real index under ``data/rag_corpus/``.

Coverage:
  * The index builder turns a small corpus into a valid FAISS index +
    chunks.jsonl.
  * The Retriever loads the freshly built index and returns k snippets
    in cosine-similarity order.
  * The Retriever degrades gracefully (returns ``[]``, no exception)
    when the index does not exist.
  * The orchestrator's per-iteration retrieval reaches the planner: a
    monkeypatched Retriever feeds the planner a known snippet list and
    we observe it via the ``planner_citations`` evidence path.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from zt411_agent.planner import RagSnippet
from zt411_agent.rag.index_builder import build_index, collect_chunks
from zt411_agent.rag.retriever import Retriever


FIXTURE_CORPUS = Path(__file__).parent / "fixtures" / "rag_corpus"


# ---------------------------------------------------------------------------
# Index builder
# ---------------------------------------------------------------------------


class TestIndexBuilder:
    def test_collect_chunks_reads_fixture_corpus(self):
        chunks = collect_chunks(
            [FIXTURE_CORPUS],
            project_root=FIXTURE_CORPUS.parent.parent.parent,
            chunk_chars=2000,
            overlap=100,
        )
        # Four markdown files, each small enough to be a single chunk.
        assert len(chunks) >= 4, (
            f"expected at least 4 chunks from the fixture corpus, got {len(chunks)}"
        )
        sources = {c.section for c in chunks}
        assert {"pause_resume", "head_open", "media_out", "ribbon_out"} <= sources

        # Every chunk has a stable id and non-empty text.
        for c in chunks:
            assert c.chunk_id
            assert c.text.strip()
            assert c.page == 1  # markdown is single-page

    def test_build_index_writes_artifacts(self, tmp_path):
        chunks = collect_chunks(
            [FIXTURE_CORPUS],
            project_root=FIXTURE_CORPUS.parent.parent.parent,
        )
        build_index(chunks, tmp_path)

        assert (tmp_path / "index.faiss").exists()
        assert (tmp_path / "chunks.jsonl").exists()
        assert (tmp_path / "MANIFEST.md").exists()

        # Sanity: chunks.jsonl is one JSON record per chunk.
        with (tmp_path / "chunks.jsonl").open("r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        assert len(lines) == len(chunks)


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------


class TestRetriever:
    def test_returns_top_k_for_a_query(self, tmp_path):
        chunks = collect_chunks(
            [FIXTURE_CORPUS],
            project_root=FIXTURE_CORPUS.parent.parent.parent,
        )
        build_index(chunks, tmp_path)

        retriever = Retriever(
            index_path=tmp_path / "index.faiss",
            chunks_path=tmp_path / "chunks.jsonl",
        )
        snippets = retriever.retrieve("printer is paused, how do I resume", k=3)

        assert 1 <= len(snippets) <= 3
        # The pause/resume document should rank above the fault docs for
        # a pause-themed query — strong sanity check on the embedding +
        # cosine path.
        top_sections = [s.section for s in snippets]
        assert "pause_resume" in top_sections

        # snippet shape contract
        for s in snippets:
            assert isinstance(s, RagSnippet)
            assert s.snippet_id
            assert s.text
            assert -1.0 <= s.score <= 1.0001  # cosine on normalised vectors

    def test_returns_empty_when_index_missing(self, tmp_path):
        # Point at paths that don't exist; should not raise.
        retriever = Retriever(
            index_path=tmp_path / "nope.faiss",
            chunks_path=tmp_path / "nope.jsonl",
        )
        result = retriever.retrieve("anything at all", k=5)
        assert result == []

        # Subsequent calls also return [] without re-trying — ensures
        # we don't pay the load-attempt cost on every loop iteration.
        again = retriever.retrieve("another query", k=5)
        assert again == []

    def test_empty_query_returns_empty(self, tmp_path):
        # Even with a valid index, an empty query yields []; no inference cost.
        chunks = collect_chunks(
            [FIXTURE_CORPUS],
            project_root=FIXTURE_CORPUS.parent.parent.parent,
        )
        build_index(chunks, tmp_path)

        retriever = Retriever(
            index_path=tmp_path / "index.faiss",
            chunks_path=tmp_path / "chunks.jsonl",
        )
        assert retriever.retrieve("", k=5) == []
        assert retriever.retrieve("   ", k=5) == []


# ---------------------------------------------------------------------------
# Orchestrator integration — snippets reach the planner
# ---------------------------------------------------------------------------


class TestOrchestratorReceivesSnippets:
    """Verify the orchestrator pulls snippets from the Retriever each
    iteration and forwards them to the planner.

    We don't replicate the full hermetic loop fixture from
    ``test_agent_loop_pause_fixture.py`` here — just the slice that
    proves the pipe is connected. We construct an orchestrator with:
      * a stub Retriever that returns one canned snippet
      * a stub planner that captures the snippets passed in and returns
        an immediate-escalate response so the loop exits after one step.
    """

    def test_planner_sees_retriever_snippets(self):
        from zt411_agent.agent.orchestrator import Orchestrator
        from zt411_agent.agent.validation_specialist import ValidationSpecialist
        from zt411_agent.planner import PlannerResponse, RuntimeTier
        from zt411_agent.state import AgentState, OSPlatform

        canned = RagSnippet(
            snippet_id="rag_test_canned_001",
            source="zebra",
            section="pause_resume",
            text="Press PAUSE to resume printing.",
            score=0.91,
        )

        stub_retriever = MagicMock(spec=Retriever)
        stub_retriever.retrieve.return_value = [canned]

        captured: list[list[RagSnippet]] = []

        def stub_planner(_state, snippets):
            captured.append(list(snippets))
            return PlannerResponse(
                ranked_specialists=["validation_specialist"],
                rationale="stub",
                citation_ids=[canned.snippet_id],
                risk_level="safe",
                success_criteria_met=False,
                escalate=True,
                escalation_reason="stub-end",
                tier_used=RuntimeTier.OFFLINE,
                raw_response="",
            )

        # Build the orchestrator with the validation specialist (required)
        # and inject our stubs.
        cfg = MagicMock()
        cfg.runtime.tier = "tier0"
        cfg.llm.planner_backend = "claude"
        cfg.llm.model = "stub"
        cfg.llm.temperature = 0.0
        cfg.llm.max_tokens = 16
        cfg.llm.timeout = 1.0
        cfg.llm.require_citations = False
        cfg.llm.json_schema.retries = 1
        cfg.ollama.host = "http://localhost:11434"
        cfg.ollama.model = "granite4"
        cfg.ollama.temperature = 0.0
        cfg.ollama.num_ctx = 1024

        orch = Orchestrator(
            specialists=[ValidationSpecialist()],
            cfg=cfg,
            max_loop_steps=1,
            retriever=stub_retriever,
        )
        # Override the planner with the capturing stub.
        orch._planner = stub_planner

        state = AgentState(os_platform=OSPlatform.LINUX, symptoms=["printer paused"])
        state.device.ip = "192.168.99.10"

        orch.run(state)

        assert stub_retriever.retrieve.called, (
            "orchestrator must invoke retriever.retrieve() each iteration"
        )
        assert captured, "planner stub never called"
        # The planner received the canned snippet from the retriever.
        forwarded_ids = {s.snippet_id for s in captured[0]}
        assert canned.snippet_id in forwarded_ids
