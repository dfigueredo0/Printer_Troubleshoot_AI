"""
Orchestrator — owns routing, planning, and the agent loop.

Loop contract
-------------
1. PLAN  — ask the LLM planner which specialist to invoke next and why.
2. ACT   — invoke that specialist; it mutates state and appends evidence.
3. VALIDATE — validation specialist checks success criteria and guardrails.
4. REPEAT until success, escalation, or max_loop_steps.

Design choices
--------------
* The LLM planner is advisory only: it returns a ranked list of specialists.
  The orchestrator always re-scores via can_handle() and takes the best match,
  so a hallucinating planner can't force a destructive action.
* All LLM calls are wrapped in retry logic with JSON schema validation.
* Escalation is triggered when: no specialist can improve the state
  (all can_handle() scores below MIN_UTILITY), the planner fails repeatedly,
  or a human-confirmation timeout fires.
* The full state is passed (as a condensed summary) to the LLM planner on
  every iteration so it has current context without us managing a chat history.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from .base import Specialist
from ..state import (
    AgentState,
    ActionLogEntry,
    ActionStatus,
    LoopStatus,
    RiskLevel,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_UTILITY: float = 0.05          # below this score a specialist is skipped
MAX_PLANNER_RETRIES: int = 3
ANTHROPIC_API_URL = ""
DEFAULT_MODEL = "claude-sonnet-4-20250514" # TODO: should grab from config in the future, hardcoded for now

# ---------------------------------------------------------------------------
# Planner schema — the structured output we ask the LLM for
# ---------------------------------------------------------------------------

PLANNER_SYSTEM_PROMPT = """\
You are the planning brain of a ZT411 printer troubleshooting agent.
Given the current agent state summary, output ONLY a JSON object with this schema:

{
  "ranked_specialists": ["<name>", ...],   // ordered best-first
  "rationale": "<one sentence>",
  "success_criteria_met": false,
  "escalate": false,
  "escalation_reason": ""
}

Specialist names: windows_specialist, cups_specialist, network_specialist,
                  device_specialist, validation_specialist

Rules:
- ranked_specialists must contain at least one entry.
- If the printer issue is clearly resolved, set success_criteria_met to true.
- If blocked with no viable next step, set escalate to true with a reason.
- Output ONLY the JSON object. No markdown, no explanation.
"""

def _build_planner_prompt(state: AgentState) -> str:
    """Produce a token-efficient state summary for the LLM."""
    lines = [
        f"session_id: {state.session_id}",
        f"os_platform: {state.os_platform}",
        f"loop_counter: {state.loop_counter}",
        f"symptoms: {state.symptoms}",
        f"user_description: {state.user_description}",
        f"last_specialist: {state.last_specialist}",
        f"visited: {state.visited_specialists}",
        "",
        "--- device ---",
        f"  printer_status: {state.device.printer_status}",
        f"  alerts: {state.device.alerts}",
        f"  error_codes: {state.device.error_codes}",
        f"  head_open: {state.device.head_open}  media_out: {state.device.media_out}  ribbon_out: {state.device.ribbon_out}",
        "",
        "--- network ---",
        f"  reachable: {state.network.reachable}  latency_ms: {state.network.latency_ms}",
        f"  ports_open: {state.network.port_open}",
        "",
        "--- cups (linux) ---",
        f"  queue_state: {state.cups.queue_state}  pending_jobs: {state.cups.pending_jobs}",
        f"  filter_errors: {state.cups.filter_errors}",
        "",
        "--- windows ---",
        f"  spooler: {state.windows.spooler_running}  queue_state: {state.windows.queue_state}",
        f"  pending_jobs: {state.windows.pending_jobs}",
        "",
        "--- success flags ---",
        f"  queue_drained: {state.queue_drained}  test_print_ok: {state.test_print_ok}  device_ready: {state.device_ready}",
        "",
        "--- recent evidence (last 3) ---",
    ]
    for ev in state.evidence[-3:]:
        lines.append(f"  [{ev.specialist}] {ev.source}: {ev.content[:120]}")

    lines += [
        "",
        "--- recent actions (last 3) ---",
    ]
    for act in state.action_log[-3:]:
        lines.append(f"  [{act.specialist}] {act.action} → {act.status} {act.result[:80]}")

    return "\n".join(lines)

# ---------------------------------------------------------------------------
# LLM planner call
# ---------------------------------------------------------------------------

def _call_planner(state: AgentState, model: str = DEFAULT_MODEL) -> dict[str, Any]:
    """
    Call the Anthropic API and return the parsed planner JSON.
    Retries up to MAX_PLANNER_RETRIES times on parse failure.
    Returns a safe fallback dict on total failure.
    """
    prompt = _build_planner_prompt(state)

    for attempt in range(1, MAX_PLANNER_RETRIES + 1):
        try:
            response = httpx.post(
                ANTHROPIC_API_URL,
                headers={"Content-Type": "application/json"},
                json={
                    "model": model,
                    "max_tokens": 512,
                    "system": PLANNER_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            text = "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
            parsed = json.loads(text.strip())
            # Validate required keys
            if "ranked_specialists" not in parsed:
                raise ValueError("Missing ranked_specialists key")
            logger.info(
                "Planner response (attempt %d): %s", attempt, json.dumps(parsed)
            )
            return parsed

        except (httpx.HTTPError, json.JSONDecodeError, ValueError, KeyError) as exc:
            logger.warning("Planner attempt %d failed: %s", attempt, exc)
            if attempt == MAX_PLANNER_RETRIES:
                logger.error("All planner retries exhausted; using fallback routing.")
                return {
                    "ranked_specialists": [],  # triggers pure utility scoring
                    "rationale": "planner unavailable",
                    "success_criteria_met": False,
                    "escalate": False,
                    "escalation_reason": "",
                }
            time.sleep(1)

    # unreachable, but satisfies type checker
    return {"ranked_specialists": [], "rationale": "", "success_criteria_met": False, "escalate": False, "escalation_reason": ""}

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """
    Drives the plan → act → validate loop.

    Parameters
    ----------
    specialists:
        All available Specialist instances.  The orchestrator discovers the
        validation specialist automatically (name == "validation_specialist").
    max_loop_steps:
        Hard cutoff.  Overrides the config value if passed explicitly.
    model:
        LLM model string forwarded to the planner.
    """

    def __init__(
        self,
        specialists: list[Specialist],
        max_loop_steps: int = 10,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self.specialists: dict[str, Specialist] = {s.name: s for s in specialists}
        self.max_loop_steps = max_loop_steps
        self.model = model

        # Separate the validation specialist — it's always called at the end
        # of each loop iteration, not ranked against others.
        if "validation_specialist" not in self.specialists:
            raise ValueError("validation_specialist must be in the specialists list.")
        self.validator: Specialist = self.specialists["validation_specialist"]
        self.worker_specialists: dict[str, Specialist] = {
            name: s
            for name, s in self.specialists.items()
            if name != "validation_specialist"
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, state: AgentState) -> AgentState:
        """
        Execute the agent loop until resolved, escalated, or max steps hit.
        Mutates *state* in-place and returns it.
        """
        logger.info(
            "Agent loop starting | session=%s os=%s symptoms=%s",
            state.session_id,
            state.os_platform,
            state.symptoms,
        )

        while state.loop_status == LoopStatus.RUNNING:
            state.increment_loop()
            logger.info("--- Loop step %d ---", state.loop_counter)

            # 1. Hard cutoff
            if state.loop_counter > self.max_loop_steps:
                self._escalate(state, "max_loop_steps exceeded")
                break

            # 2. Already resolved? (can happen if validator flipped flags last round)
            if state.is_resolved():
                state.loop_status = LoopStatus.SUCCESS
                logger.info("Success criteria met; exiting loop.")
                break

            # 3. PLAN — ask LLM for preferred specialist ordering
            planner_out = _call_planner(state, self.model)

            if planner_out.get("success_criteria_met"):
                # Planner believes we're done; confirm with validator before trusting
                logger.info("Planner signals success; running validator to confirm.")
                state = self._run_specialist(self.validator, state)
                if state.is_resolved():
                    state.loop_status = LoopStatus.SUCCESS
                break

            if planner_out.get("escalate"):
                self._escalate(state, planner_out.get("escalation_reason", "planner requested escalation"))
                break

            # 4. ACT — pick the best specialist
            specialist = self._select_specialist(state, planner_out.get("ranked_specialists", []))

            if specialist is None:
                self._escalate(state, "no specialist with sufficient utility score; unable to make progress")
                break

            logger.info("Selected specialist: %s", specialist.name)
            state.last_specialist = specialist.name
            state.mark_visited(specialist.name)

            state = self._run_specialist(specialist, state)

            # 5. VALIDATE — always run validation after every worker action
            state = self._run_specialist(self.validator, state)

            # Check again in case validator just confirmed success
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