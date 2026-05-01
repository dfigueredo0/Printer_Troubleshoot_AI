"""
Orchestrator — owns routing, planning, and the agent loop.

Loop contract
-------------
1. PLAN  —  call the configured LLM planner (Claude / Ollama / offline fallback)
           to get a ranked list of specialists + citation IDs.
2. ACT   — invoke that specialist; it mutates state and appends evidence.
3. VALIDATE — validation specialist checks success criteria and guardrails.
4. REPEAT until success, escalation, or max_loop_steps.

Design choices
--------------
* The LLM planner is advisory only: it returns a ranked list of specialists.
  The orchestrator always re-scores via can_handle() and takes the best match,
  so a hallucinating planner can't force a destructive action.
* All LLM calls are handled by planner.py (Claude + Ollama + offline fallback)
  with JSON schema validation, citation enforcement, and retry logic.
* Tier detection (cloud → local LLM → offline) happens once at Orchestrator
  construction and can downgrade dynamically on transient failures.
* Escalation is triggered when: no specialist clears MIN_UTILITY, the planner
  exhausts retries, or a human-confirmation timeout fires.
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Specialist
from ..state import (
    AgentState,
    ActionStatus,
    LoopStatus,
    RiskLevel,
)

from ..planner import build_planner, PlannerFn, PlannerResponse, RagSnippet
from ..rag.retriever import Retriever

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_UTILITY: float = 0.05          # below this score a specialist is skipped
RAG_TOP_K: int = 5                 # snippets per loop iteration


class _NullRetriever:
    """Drop-in no-op retriever used when the planner is forced to
    tier0. Returns ``[]`` for every call — the offline planner ignores
    snippets, so loading the embedding model + FAISS index would be
    wasted work.
    """

    def retrieve(self, query: str, k: int = RAG_TOP_K) -> list[RagSnippet]:  # noqa: ARG002
        return []

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """
    Drives the plan → act → validate loop.
 
    Parameters
    ----------
    specialists : list[Specialist]
        All available Specialist instances.  The validation specialist is
        identified automatically by name == "validation_specialist".
    cfg : Any
        Loaded Settings object forwarded to build_planner().
    max_loop_steps : int
        Hard cutoff — overrides the config value if passed explicitly.
    """
    
    def __init__(
        self,
        specialists: list[Specialist],
        cfg: Any,
        max_loop_steps: int = 10,
        retriever: Retriever | None = None,
    ) -> None:
        self.specialists: dict[str, Specialist] = {s.name: s for s in specialists}
        self.max_loop_steps = max_loop_steps

        if "validation_specialist" not in self.specialists:
            raise ValueError("validation_specialist must be in the specialists list.")
        self.validator: Specialist = self.specialists["validation_specialist"]
        self.worker_specialists: dict[str, Specialist] = {
            name: s
            for name, s in self.specialists.items()
            if name != "validation_specialist"
        }

        self._planner: PlannerFn = build_planner(cfg)
        # One Retriever per orchestrator: model + FAISS index load on
        # first retrieve(), then stay in memory for the session. Tests
        # pass a stub or a fixture-backed Retriever via this hook.
        # When the caller didn't pass one AND the planner is forced to
        # tier0, swap in a no-op retriever — the offline planner does
        # not read snippets, so loading the embedding model + FAISS
        # index would be wasted effort that also breaks the hermetic
        # tests' wall-clock budget.
        forced_tier = getattr(getattr(cfg, "runtime", None), "tier", None)
        if retriever is not None:
            self._retriever: Retriever = retriever
        elif forced_tier == "tier0":
            self._retriever = _NullRetriever()
        else:
            self._retriever = Retriever()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, state: AgentState, rag_snippets: list[RagSnippet] | None = None) -> AgentState:
        """
        Execute the agent loop until resolved, escalated, or max steps hit.
 
        Parameters
        ----------
        state : AgentState
            Initial (or resumed) agent state.
        rag_snippets : list[RagSnippet] | None
            Pre-retrieved knowledge-base snippets to pass to the planner on
            every iteration.  Pass None to let each iteration use an empty set
            (i.e. no RAG grounding) until the RAG pipeline is wired in.
 
        Returns
        -------
        AgentState
            Mutated state with the full audit trail.
        """
        # Caller-supplied snippets are preserved as a base set; live
        # retrieval per iteration is concatenated on top so a caller
        # priming the loop with hand-picked docs (e.g. tests) still works.
        base_snippets = rag_snippets or []

        logger.info(
            "Agent loop starting | session=%s os=%s symptoms=%s",
            state.session_id,
            state.os_platform,
            state.symptoms,
        )

        while state.loop_status == LoopStatus.RUNNING:
            state.increment_loop()
            logger.info("--- Loop step %d ---", state.loop_counter)

            if state.loop_counter > self.max_loop_steps:
                self._escalate(state, "max_loop_steps exceeded")
                break

            if state.is_resolved():
                state.loop_status = LoopStatus.SUCCESS
                logger.info("Success criteria met; exiting loop.")
                break

            # Build a per-iteration RAG query from the symptoms + recent
            # evidence summary, then retrieve top-k snippets. Graceful
            # degradation: missing index → retrieve() returns [].
            iteration_snippets = self._retrieve_for_iteration(state)
            snippets = base_snippets + iteration_snippets

            # 3. PLAN
            plan: PlannerResponse = self._planner(state, snippets)
            logger.debug("Planner [%s]: specialists=%s rationale=%s citations=%s",
                        plan.tier_used.value,
                        plan.ranked_specialists,
                        plan.rationale,
                        plan.citation_ids
                    )

            if plan.citation_ids:
                state.add_evidence(
                    specialist="planner",
                    source="planner_citations",
                    content=f"Planner cited snippets: {plan.citation_ids}",
                    snippet_id=",".join(plan.citation_ids),
                )
 
            if plan.success_criteria_met:
                logger.info("Planner signals success; running validator to confirm.")
                state = self._run_specialist(self.validator, state)
                if state.is_resolved():
                    state.loop_status = LoopStatus.SUCCESS
                break
 
            if plan.escalate:
                self._escalate(
                    state,
                    plan.escalation_reason or "planner requested escalation",
                )
                break

            # 4. ACT — pick best specialist
            specialist = self._select_specialist(state, plan.ranked_specialists)
 
            if specialist is None:
                self._escalate(
                    state,
                    "no specialist with sufficient utility score; unable to make progress",
                )
                break
 
            logger.info("Selected specialist: %s (tier=%s)", specialist.name, plan.tier_used.value)
            state.last_specialist = specialist.name
            state.mark_visited(specialist.name)
            state = self._run_specialist(specialist, state)
 
            # 5. VALIDATE
            state = self._run_specialist(self.validator, state)

            # 5a. SUSPEND if the validator just issued a confirmation token.
            # The loop yields here; the FastAPI service holds the SSE stream
            # open and re-enters run() after the user approves the action.
            if self._has_pending_confirmation(state):
                state.loop_status = LoopStatus.AWAITING_CONFIRMATION
                logger.info(
                    "Loop suspended: PENDING action(s) awaiting confirmation."
                )
                break

            if state.is_resolved():
                state.loop_status = LoopStatus.SUCCESS
                logger.info("Validated success; exiting loop.")
                break
 
        logger.info(
            "Agent loop finished | status=%s steps=%d",
            state.loop_status,
            state.loop_counter,
        )
        return state
    
    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _retrieve_for_iteration(self, state: AgentState) -> list[RagSnippet]:
        """Construct a query from the agent state and retrieve top-k
        snippets for this iteration.

        The query combines the user-supplied symptoms with a short
        summary of the most recent evidence so the retriever sees both
        the framing and the new tool output. The retriever's own
        graceful-degradation path returns [] if the index is missing,
        so this helper never raises.
        """
        try:
            query = self._build_rag_query(state)
            if not query:
                return []
            return self._retriever.retrieve(query, k=RAG_TOP_K)
        except Exception as exc:  # noqa: BLE001
            # Retrieval is advisory — never let a RAG error stop the loop.
            logger.warning("RAG retrieval failed (returning []): %s", exc)
            return []

    def _build_rag_query(self, state: AgentState) -> str:
        """Build a single-string query from symptoms + recent evidence."""
        parts: list[str] = []
        if state.symptoms:
            parts.append(" ".join(state.symptoms))
        if state.user_description:
            parts.append(state.user_description)
        # Last few evidence content snippets — bounded to keep the embedding
        # query short enough to be informative without ballooning.
        for ev in state.evidence[-3:]:
            if ev.content:
                parts.append(ev.content[:200])
        return " ".join(p.strip() for p in parts if p.strip()).strip()

    def _select_specialist(
        self, state: AgentState, planner_ranking: list[str]
    ) -> Specialist | None:
        """
        Merge the LLM ranking with live utility scores.

        Strategy:
        - Score every worker specialist via can_handle().
        - If the planner's top pick has utility >= MIN_UTILITY, use it.
        - Otherwise fall back to the highest utility score overall.
        - Return None if no specialist clears MIN_UTILITY.
        """
        scores: dict[str, float] = {
            name: s.can_handle(state)
            for name, s in self.worker_specialists.items()
        }
        logger.debug("Utility scores: %s", scores)

        # Try planner-preferred candidates first
        for name in planner_ranking:
            if name in scores and scores[name] >= MIN_UTILITY:
                return self.worker_specialists[name]

        # Fall back to highest scorer
        best_name = max(scores, key=lambda n: scores[n])
        if scores[best_name] >= MIN_UTILITY:
            return self.worker_specialists[best_name]

        return None

    def _run_specialist(
        self, specialist: Specialist, state: AgentState
    ) -> AgentState:
        """Call specialist.act() and merge returned next_state into the current state."""
        try:
            result = specialist.act(state)
            next_state: AgentState = result.get("next_state", state)
            logger.debug(
                "Specialist %s acted | evidence=%s actions=%s",
                specialist.name,
                result.get("evidence"),
                result.get("actions_taken"),
            )
            return next_state
        except Exception as exc:  # noqa: BLE001
            logger.exception("Specialist %s raised: %s", specialist.name, exc)
            state.add_evidence(
                specialist=specialist.name,
                source="orchestrator_error",
                content=f"Specialist raised exception: {exc}",
            )
            return state

    def _escalate(self, state: AgentState, reason: str) -> None:
        logger.warning("Escalating: %s", reason)
        state.loop_status = LoopStatus.ESCALATED
        state.escalation_reason = reason
        state.log_action(
            specialist="orchestrator",
            action=f"escalate: {reason}",
            risk=RiskLevel.SAFE,
            status=ActionStatus.EXECUTED,
            result="Human intervention required.",
        )

    def _has_pending_confirmation(self, state: AgentState) -> bool:
        """True if any action_log entry is PENDING with a confirmation token.

        When this returns True, the orchestrator suspends the loop instead of
        iterating further. The FastAPI service layer (or any other caller)
        observes loop_status == AWAITING_CONFIRMATION, holds the SSE stream
        open, and resumes the loop after the user POSTs to /confirm/{token}
        — which calls consume_confirmation_token + update_action_status to
        flip the entry to CONFIRMED, at which point this predicate returns
        False on the next pass and execution proceeds.
        """
        return any(
            a.status == ActionStatus.PENDING and a.confirmation_token
            for a in state.action_log
        )