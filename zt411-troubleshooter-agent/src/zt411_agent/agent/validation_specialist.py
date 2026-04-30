from __future__ import annotations

import logging
from typing import Any

from .base import Specialist
from ..state import (
    AgentState,
    ActionLogEntry,
    ActionStatus,
    LoopStatus,
    RiskLevel,
)

logger = logging.getLogger(__name__)

# Risk levels that always require an explicit confirmation token before execution
_CONFIRMATION_REQUIRED: set[RiskLevel] = {
    RiskLevel.DESTRUCTIVE,
    RiskLevel.CONFIG_CHANGE,
    RiskLevel.FIRMWARE,
    RiskLevel.REBOOT,
    RiskLevel.SERVICE_RESTART,
}

# High-risk actions that NEVER auto-approve and never accept a token —
# they are held until a human flips them in some out-of-band way
# (operator console, supervisor sign-off, etc.).
_HIGH_RISK_HUMAN_ONLY: set[RiskLevel] = {
    RiskLevel.DESTRUCTIVE,
    RiskLevel.FIRMWARE,
    RiskLevel.REBOOT,
}

# Risk levels that require an explicit confirmation token but can then
# be flipped to APPROVED by `state.consume_confirmation_token(token)`.
_TOKEN_REQUIRED: set[RiskLevel] = {
    RiskLevel.SERVICE_RESTART,
    RiskLevel.CONFIG_CHANGE,
}

# Evidence sources that legitimately back queue_drained.
# Live tools currently emit these names; planner-generated content
# (e.g. anything containing "proposed" or "rag_") never qualifies.
_QUEUE_EVIDENCE_SOURCES: set[str] = {
    "ps_enum_jobs",      # Windows PowerShell EnumJobs (live)
    "enum_jobs",         # WindowsSpecialist (current implementation)
    "lpstat_jobs",       # CUPSSpecialist `lpstat -o`
}

# Evidence sources that legitimately back test_print_ok. There is no
# production code emitting these yet — a test print sub-flow is Phase 4.
# Listed here so the validator recognises them when they appear.
_TEST_PRINT_EVIDENCE_SOURCES: set[str] = {
    "ps_test_print",
    "test_print",
}

# Boot-only informational alert that must be tolerated when judging
# device_ready. The ZT411 firmware emits alert:1.15 ("printer power on")
# on every boot and never clears it without a power cycle, so requiring
# zero error_codes would block ready forever.
_TOLERATED_BOOT_ALERTS: set[str] = {"alert:1.15"}


class ValidationSpecialist(Specialist):
    """
    Utility scoring logic
    ---------------------
    The validator is never ranked against workers — the orchestrator always
    calls it at the end of each loop iteration.  can_handle() still reflects
    how urgently it's needed so the orchestrator can use it for ordering if
    needed in the future.

    High score when:
    * There are pending actions in the action log (need go/no-go decision).
    * New evidence exists that hasn't been diffed yet.
    * Success criteria are partially met (confirm before declaring victory).

    Lower when:
    * No pending actions and no new evidence.
    """

    name = "validation_specialist"

    def __init__(self) -> None:
        super().__init__()
        # Cached at the END of each act() call so the next call can detect
        # "no progress" between iterations. Used only by the Session C.5
        # fault short-circuit path.
        self._last_device_snapshot: tuple | None = None
        self._snapshot_session_id: str = ""
        # Number of physical_recommendations evidence items observed at the
        # end of the prior validator call. The fault short-circuit only
        # fires when this is >= 1, which guarantees at least one
        # recommendation existed in a strictly prior loop iteration.
        self._physical_rec_count_at_last_check: int = 0

    def can_handle(self, state: AgentState) -> float:  # noqa: D401
        score = 0.0

        # 1. Pending actions awaiting confirmation
        pending = [a for a in state.action_log if a.status == ActionStatus.PENDING]
        score += min(0.4 * len(pending), 0.6)

        # 2. Any success flag was just flipped (confirm it's real)
        if state.queue_drained or state.test_print_ok or state.device_ready:
            score += 0.3

        # 3. Evidence collected but not yet diffed — take a snapshot
        undiffed_evidence = len(state.evidence) - len(state.snapshot_diffs)
        if undiffed_evidence > 0:
            score += min(0.05 * undiffed_evidence, 0.2)

        # 4. Loop nearing the cap — validate before running out of steps
        from ..settings import Settings  # avoid circular at module level
        try:
            cfg = Settings.load()
            max_steps = cfg.model.max_steps
        except Exception:
            max_steps = 10
        if state.loop_counter >= max_steps - 2:
            score += 0.2

        return min(score, 1.0)

    def act(self, state: AgentState) -> dict[str, Any]:
        """
        1. Risk-tiered guardrail pass over PENDING actions.
        2. Three-flag success-criteria evaluation, evidence-grounded.
        3. Hallucination guard: any flag set without backing evidence is reset
           and an audit-trail evidence item is emitted.
        4. Snapshot diffs for any state field that flipped this turn.
        5. Loop short-circuit when the loop is stuck on a human-action recommendation.
        """
        logger.info("ValidationSpecialist acting on session %s", state.session_id)

        actions_taken: list[str] = []
        evidence_items: list[str] = []

        # ------------------------------------------------------------------
        # 1. Guardrail pass — risk-tiered review of pending actions
        # ------------------------------------------------------------------
        pending_actions = [a for a in state.action_log if a.status == ActionStatus.PENDING]

        for entry in pending_actions:
            self._apply_guardrail(entry, state, evidence_items, actions_taken)

        # ------------------------------------------------------------------
        # 2. Three-flag success-criteria evaluation, evidence-grounded
        # ------------------------------------------------------------------
        self._check_success_criteria(state, evidence_items, actions_taken)

        # ------------------------------------------------------------------
        # 3. Hallucination guard — reset any flag whose backing evidence is
        #    missing.  This is the gate that keeps a planner that returned
        #    success_criteria_met=true from sneaking past the validator: even
        #    if some other path flipped state.queue_drained / device_ready /
        #    test_print_ok, we re-prove each one against the evidence here
        #    and unflip anything that doesn't hold up.
        # ------------------------------------------------------------------
        self._hallucination_guard(state, evidence_items, actions_taken)

        # ------------------------------------------------------------------
        # 4. Snapshot diffs — record observable state changes this turn
        # ------------------------------------------------------------------
        if state.cups.pending_jobs == 0 and state.cups.queue_name:
            diff = state.record_diff(
                field="cups.pending_jobs",
                before="unknown",
                after=0,
                confirmed_by=self.name,
            )
            evidence_items.append(
                state.add_evidence(
                    specialist=self.name,
                    source="snapshot_diff",
                    content=f"Diff recorded: {diff.field} before={diff.before} after={diff.after}",
                ).evidence_id
            )

        if state.windows.pending_jobs == 0 and state.windows.queue_name:
            diff = state.record_diff(
                field="windows.pending_jobs",
                before="unknown",
                after=0,
                confirmed_by=self.name,
            )
            evidence_items.append(
                state.add_evidence(
                    specialist=self.name,
                    source="snapshot_diff",
                    content=f"Diff recorded: {diff.field} before={diff.before} after={diff.after}",
                ).evidence_id
            )

        # ------------------------------------------------------------------
        # 5. Loop-termination check — short-circuit on repeated human-action
        #    recommendation
        # ------------------------------------------------------------------
        # When a worker specialist has logged a SAFE/LOW-risk recommendation
        # whose result is "Awaiting human action..." and an entire prior
        # loop iteration has elapsed without the underlying physical
        # condition clearing, the loop is stuck — the planner has nothing
        # new to plan, the worker keeps re-emitting the same recommendation,
        # and we eventually escalate with a misleading
        # "max_loop_steps exceeded" reason. Detect this and escalate with
        # an honest reason instead, before the cap fires.
        triggering_entry = self._find_repeated_human_action_entry(state)
        if triggering_entry is not None:
            state.loop_status = LoopStatus.ESCALATED
            state.escalation_reason = "awaiting_human_action"
            ev = state.add_evidence(
                specialist=self.name,
                source="validation_short_circuit",
                content=(
                    f"short-circuit on stuck human-action recommendation; "
                    f"triggered by action_log entry {triggering_entry.entry_id} "
                    f"(specialist={triggering_entry.specialist}, "
                    f"action={triggering_entry.action!r}). Underlying "
                    f"condition still present after {state.loop_counter} "
                    f"loop step(s); escalating instead of cycling further."
                ),
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append(
                f"short-circuit: awaiting human action on {triggering_entry.entry_id}"
            )

        # ------------------------------------------------------------------
        # 5b. Loop-termination check — Session C.5 fault short-circuit
        # ------------------------------------------------------------------
        # The B.5 path above only fires when a worker emitted an action_log
        # result string containing "awaiting human action" — currently only
        # the DeviceSpecialist user-paused branch does that. Fault branches
        # (head_open / media_out / ribbon_out) emit human-readable advice
        # as `physical_recommendations` evidence and never set that result
        # string, so the loop runs out the cap on faults.
        #
        # This second path generalises the short-circuit: if the device
        # state is unchanged from the prior validator call AND a physical
        # condition is still active AND a `physical_recommendations`
        # evidence item already existed at the end of the prior call,
        # escalate with the same `awaiting_human_action` reason.
        if state.loop_status == LoopStatus.RUNNING:
            stuck_flag = self._find_stuck_physical_condition(state)
            if stuck_flag is not None:
                state.loop_status = LoopStatus.ESCALATED
                state.escalation_reason = "awaiting_human_action"
                ev = state.add_evidence(
                    specialist=self.name,
                    source="validation_short_circuit",
                    content=(
                        f"short-circuit on stuck physical condition; "
                        f"{stuck_flag}=True after {state.loop_counter} "
                        f"loop step(s) with no progress observed since "
                        f"prior validator call. A physical_recommendations "
                        f"evidence item is already on record; escalating "
                        f"instead of cycling further."
                    ),
                )
                evidence_items.append(ev.evidence_id)
                actions_taken.append(
                    f"short-circuit: stuck on physical condition {stuck_flag}"
                )

        # ------------------------------------------------------------------
        # 6. Update cached snapshot for the next validator call. Always
        # runs (including after a short-circuit), so an external test
        # harness re-running the validator in the same session sees a
        # consistent baseline.
        # ------------------------------------------------------------------
        self._update_device_snapshot(state)

        state.log_action(
            specialist=self.name,
            action="; ".join(actions_taken) or "validation pass — nothing pending",
            risk=RiskLevel.SAFE,
            status=ActionStatus.EXECUTED,
            result=f"reviewed {len(pending_actions)} pending actions; {len(evidence_items)} evidence items added",
        )

        return {
            "evidence": evidence_items,
            "actions_taken": actions_taken,
            "next_state": state,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_guardrail(
        self,
        entry: ActionLogEntry,
        state: AgentState,
        evidence_items: list[str],
        actions_taken: list[str],
    ) -> None:
        """Risk-tiered guardrail decision for a single PENDING action.

        Three branches per the architecture spec:

        * High-risk human-only (DESTRUCTIVE / FIRMWARE / REBOOT): leave the
          action PENDING, emit ``validation_guardrail_high_risk`` evidence
          explaining what blocked. Never auto-approves and never accepts a
          token — escalation path only.
        * Token-required (SERVICE_RESTART / CONFIG_CHANGE): leave PENDING,
          issue a confirmation token via ``state.issue_confirmation_token``,
          attach to the entry, emit ``validation_guardrail_token`` evidence
          including the token ID. The human can later flip the action by
          calling ``state.consume_confirmation_token(token)``.
        * SAFE / LOW: auto-approve (status → CONFIRMED), emit
          ``guardrail_approved`` evidence (existing source name kept for
          back-compat with prior tests).
        """
        risk = entry.risk

        if risk in _HIGH_RISK_HUMAN_ONLY:
            ev = state.add_evidence(
                specialist=self.name,
                source="validation_guardrail_high_risk",
                content=(
                    f"HOLD (high-risk human approval required): "
                    f"action={entry.action!r} risk={risk.value} "
                    f"entry_id={entry.entry_id}. Will not auto-approve and "
                    f"will not accept a confirmation token; escalation "
                    f"required."
                ),
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append(
                f"hold (high-risk, human only): {entry.action}"
            )
            return

        if risk in _TOKEN_REQUIRED:
            if not entry.confirmation_token:
                token = state.issue_confirmation_token(entry.entry_id)
                entry.confirmation_token = token
            ev = state.add_evidence(
                specialist=self.name,
                source="validation_guardrail_token",
                content=(
                    f"HOLD (confirmation token required): "
                    f"action={entry.action!r} risk={risk.value} "
                    f"entry_id={entry.entry_id} "
                    f"token={entry.confirmation_token}. "
                    f"Call state.consume_confirmation_token(token) to "
                    f"approve."
                ),
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"hold (token issued): {entry.action}")
            return

        if risk in {RiskLevel.SAFE, RiskLevel.LOW}:
            entry.status = ActionStatus.CONFIRMED
            ev = state.add_evidence(
                specialist=self.name,
                source="guardrail_approved",
                content=(
                    f"APPROVED: {entry.action} — risk={risk.value} within "
                    f"auto-approve threshold"
                ),
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"approved: {entry.action}")
            return

        # Unknown / MEDIUM / future risk levels — conservative HOLD with
        # token. Better to ask than guess.
        if not entry.confirmation_token:
            token = state.issue_confirmation_token(entry.entry_id)
            entry.confirmation_token = token
        ev = state.add_evidence(
            specialist=self.name,
            source="validation_guardrail_token",
            content=(
                f"HOLD (unrecognised risk class — defaulting to token): "
                f"action={entry.action!r} risk={risk.value} "
                f"entry_id={entry.entry_id} "
                f"token={entry.confirmation_token}."
            ),
        )
        evidence_items.append(ev.evidence_id)
        actions_taken.append(f"hold (token, fallback): {entry.action}")

    def _check_success_criteria(
        self,
        state: AgentState,
        evidence_items: list[str],
        actions_taken: list[str],
    ) -> None:
        """Set queue_drained / device_ready / test_print_ok ONLY when
        backed by real tool-source evidence.

        These are positive checks: each flag is only flipped to True when
        the corresponding evidence is present. The hallucination guard
        runs after this and resets any flag the planner / external code
        flipped without backing evidence.
        """
        # queue_drained: at least one evidence item from a live job-listing
        # tool indicating zero pending jobs.
        if not state.queue_drained and self._queue_drained_supported(state):
            state.queue_drained = True
            ev = state.add_evidence(
                specialist=self.name,
                source="success_check",
                content=(
                    "queue_drained confirmed: backing evidence from "
                    f"{sorted(_QUEUE_EVIDENCE_SOURCES)} present and "
                    "indicates zero pending jobs."
                ),
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("confirmed queue_drained")

        # device_ready: status == idle/ready AND alerts/error_codes only
        # contain the tolerated boot informational entry.
        if not state.device_ready and self._device_ready_supported(state):
            state.device_ready = True
            ev = state.add_evidence(
                specialist=self.name,
                source="success_check",
                content=(
                    f"device_ready confirmed: status={state.device.printer_status} "
                    f"alerts={state.device.alerts} "
                    f"error_codes={state.device.error_codes}"
                ),
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("confirmed device_ready")

        # test_print_ok: explicit test-print success evidence required.
        if not state.test_print_ok and self._test_print_supported(state):
            state.test_print_ok = True
            ev = state.add_evidence(
                specialist=self.name,
                source="success_check",
                content="test_print_ok confirmed: success evidence found.",
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("confirmed test_print_ok")

        if state.is_resolved():
            logger.info("All success criteria confirmed by validation specialist.")

    def _queue_drained_supported(self, state: AgentState) -> bool:
        """Is there at least one job-listing evidence item with content
        indicating zero pending jobs?
        """
        for ev in state.evidence:
            if ev.source not in _QUEUE_EVIDENCE_SOURCES:
                continue
            content = (ev.content or "").lower()
            # Heuristic match. The two production sources phrase it as
            #   lpstat_jobs    : "0 pending jobs ..."
            #   enum_jobs      : "no print jobs in queue ..."
            # A future ps_enum_jobs would phrase it similarly. We accept
            # any of the common zero-job phrasings.
            zero_indicators = (
                "0 pending",
                "no pending",
                "no print jobs",
                "0 jobs",
                "queue empty",
                "pending_jobs=0",
                "pending=0",
            )
            if any(ind in content for ind in zero_indicators):
                return True
        return False

    def _device_ready_supported(self, state: AgentState) -> bool:
        if state.device.printer_status not in {"idle", "ready"}:
            return False
        if state.device.alerts:
            return False
        # error_codes may contain the boot informational entry; nothing else.
        residual = [
            code for code in state.device.error_codes
            if code not in _TOLERATED_BOOT_ALERTS
        ]
        return not residual

    def _test_print_supported(self, state: AgentState) -> bool:
        for ev in state.evidence:
            if ev.source not in _TEST_PRINT_EVIDENCE_SOURCES:
                continue
            if "success" in (ev.content or "").lower():
                return True
        return False

    def _hallucination_guard(
        self,
        state: AgentState,
        evidence_items: list[str],
        actions_taken: list[str],
    ) -> None:
        """Reset any success flag that lacks backing evidence and emit a
        ``validation_hallucination_guard`` audit item per reset.

        Runs AFTER ``_check_success_criteria``, so a flag we just set on
        valid evidence stays True. A flag that was set externally
        (planner, another specialist) and we cannot re-prove gets reset.
        """
        missing: list[str] = []

        if state.queue_drained and not self._queue_drained_supported(state):
            state.queue_drained = False
            missing.append("queue_drained")

        if state.device_ready and not self._device_ready_supported(state):
            state.device_ready = False
            missing.append("device_ready")

        if state.test_print_ok and not self._test_print_supported(state):
            state.test_print_ok = False
            missing.append("test_print_ok")

        if missing:
            ev = state.add_evidence(
                specialist=self.name,
                source="validation_hallucination_guard",
                content=(
                    f"Reset {missing} — flag(s) were set but no "
                    f"backing tool-output evidence found. Loop will not be "
                    f"marked resolved this turn."
                ),
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"hallucination guard: reset {missing}")

    def _find_repeated_human_action_entry(
        self, state: AgentState
    ) -> ActionLogEntry | None:
        """Return an action_log entry matching the "stuck on human action"
        pattern, or None if the loop is not stuck.

        The pattern: a worker specialist (not validation itself) has logged
        a SAFE/LOW-risk action whose ``result`` contains "awaiting human
        action", at least one full prior loop iteration has completed, and
        the physical condition that triggered the recommendation is still
        present in ``state.device``. The presence of an outstanding
        recommendation that the human hasn't acted on, after a complete
        loop has cycled through every other specialist with no new
        information, is the terminal signal the orchestrator currently
        misses.

        Returning the triggering entry (instead of just a bool) lets the
        caller cite which recommendation the loop is stuck on, which
        matters for the audit trail.
        """
        if state.loop_counter < 2:
            return None

        # The condition the recommendation is asking the human to fix must
        # still be observable. If it has cleared, the worker will stop
        # emitting the recommendation on its own and we shouldn't pre-empt
        # a clean success path.
        physical_condition_active = (
            state.device.paused is True
            or state.device.head_open is True
            or state.device.media_out is True
            or state.device.ribbon_out is True
        )
        if not physical_condition_active:
            return None

        candidates = [
            a for a in state.action_log
            if a.specialist != self.name
            and a.risk in {RiskLevel.SAFE, RiskLevel.LOW}
            and a.status in {ActionStatus.PENDING, ActionStatus.CONFIRMED}
            and "awaiting human action" in (a.result or "").lower()
        ]
        if not candidates:
            return None

        # Most recent triggering entry — that's the one we cite.
        return candidates[-1]

    # ------------------------------------------------------------------
    # Session C.5 — fault short-circuit helpers
    # ------------------------------------------------------------------

    def _find_stuck_physical_condition(self, state: AgentState) -> str | None:
        """Return the name of an active physical condition the loop is stuck
        on, or None.

        Stuck means: loop has run at least 2 iterations, the device's
        printer_status + four physical flags are unchanged from the prior
        validator call, at least one flag is True, and a
        ``physical_recommendations`` evidence item already existed at the
        end of the prior call. Together these mean the loop has emitted
        actionable advice, the human hasn't acted, and nothing on the
        device side has changed in this iteration — cycling further is
        wasted work.
        """
        if state.loop_counter < 2:
            return None

        # Validator was constructed in a different session or never run
        # before — no baseline to compare against.
        if (
            self._last_device_snapshot is None
            or self._snapshot_session_id != state.session_id
        ):
            return None

        if self._device_snapshot(state) != self._last_device_snapshot:
            return None

        # First active flag wins for the audit-trail evidence content.
        if state.device.paused is True:
            active_flag: str | None = "paused"
        elif state.device.head_open is True:
            active_flag = "head_open"
        elif state.device.media_out is True:
            active_flag = "media_out"
        elif state.device.ribbon_out is True:
            active_flag = "ribbon_out"
        else:
            active_flag = None
        if active_flag is None:
            return None

        # The recommendation must have existed BEFORE this iteration —
        # checking the cached count from the prior call enforces that.
        if self._physical_rec_count_at_last_check < 1:
            return None

        return active_flag

    def _device_snapshot(self, state: AgentState) -> tuple:
        """Tuple of the five device fields used for no-progress detection."""
        return (
            state.device.printer_status,
            state.device.paused,
            state.device.head_open,
            state.device.media_out,
            state.device.ribbon_out,
        )

    def _update_device_snapshot(self, state: AgentState) -> None:
        """Cache device snapshot + physical_recommendations count for the
        next validator call. Runs at the end of every act()."""
        self._snapshot_session_id = state.session_id
        self._last_device_snapshot = self._device_snapshot(state)
        self._physical_rec_count_at_last_check = sum(
            1 for ev in state.evidence
            if ev.source == "physical_recommendations"
        )
