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
    SnapshotDiff,
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
        1. Check all pending actions for guardrail violations.
        2. Approve safe actions; hold/reject risky ones without a token.
        3. Evaluate observable success criteria.
        4. Record snapshot diffs for the audit trail.
        """
        logger.info("ValidationSpecialist acting on session %s", state.session_id)

        actions_taken: list[str] = []
        evidence_items: list[str] = []

        # ------------------------------------------------------------------
        # 1. Guardrail pass — review pending actions
        # ------------------------------------------------------------------
        pending_actions = [a for a in state.action_log if a.status == ActionStatus.PENDING]

        for entry in pending_actions:
            verdict, reason = self._evaluate_action(entry, state)
            if verdict == "approve":
                entry.status = ActionStatus.CONFIRMED
                ev = state.add_evidence(
                    specialist=self.name,
                    source="guardrail_approved",
                    content=f"APPROVED: {entry.action} — {reason}",
                )
                evidence_items.append(ev.evidence_id)
                actions_taken.append(f"approved: {entry.action}")
            elif verdict == "hold":
                # Leave as PENDING; issue a confirmation token if not already issued
                if not entry.confirmation_token:
                    token = state.issue_confirmation_token(entry.entry_id)
                    entry.confirmation_token = token
                ev = state.add_evidence(
                    specialist=self.name,
                    source="guardrail_hold",
                    content=f"HOLD (needs human confirmation): {entry.action} — {reason}. Token: {entry.confirmation_token}",
                )
                evidence_items.append(ev.evidence_id)
                actions_taken.append(f"hold (token issued): {entry.action}")
            else:  # reject
                entry.status = ActionStatus.SKIPPED
                ev = state.add_evidence(
                    specialist=self.name,
                    source="guardrail_rejected",
                    content=f"REJECTED: {entry.action} — {reason}",
                )
                evidence_items.append(ev.evidence_id)
                actions_taken.append(f"rejected: {entry.action}")

        # ------------------------------------------------------------------
        # 2. Hallucination guard — refuse success claims without tool output
        # ------------------------------------------------------------------
        if state.queue_drained or state.test_print_ok or state.device_ready:
            # Verify each success flag has at least one backing evidence item
            # from a real tool output (not a proposed/pending action).
            real_sources = {ev.source for ev in state.evidence if "proposed" not in ev.source}
            if not real_sources:
                logger.warning(
                    "Success flags set but NO real tool evidence found — resetting flags."
                )
                state.queue_drained = False
                state.test_print_ok = False
                state.device_ready = False
                ev = state.add_evidence(
                    specialist=self.name,
                    source="hallucination_guard",
                    content="Success flags reset: no observable tool output to support success claim.",
                )
                evidence_items.append(ev.evidence_id)
                actions_taken.append("reset unsubstantiated success flags")

        # ------------------------------------------------------------------
        # 3. Evaluate observable success criteria
        #    (stub — replace conditions with real signal checks)
        # ------------------------------------------------------------------
        self._check_success_criteria(state, evidence_items, actions_taken)

        # ------------------------------------------------------------------
        # 4. Snapshot diff — record before/after for any state change this turn
        # ------------------------------------------------------------------
        # The orchestrator will compare state at the start/end of the loop;
        # here we record any field-level diffs we can observe locally.
        # Example: queue length before vs after
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

    def _evaluate_action(
        self, entry: ActionLogEntry, state: AgentState
    ) -> tuple[str, str]:
        """
        Returns ("approve" | "hold" | "reject", reason_string).

        Rules (ordered by precedence):
        1. Destructive / firmware / reboot → always HOLD for human confirmation.
        2. Service restart / config change → HOLD unless allow_elevation is set in config.
        3. Safe / low risk → APPROVE.
        4. Privilege check: driver/port/firmware changes need admin flag.
        """
        risk = entry.risk

        # 1. Always-hold categories
        if risk in {RiskLevel.DESTRUCTIVE, RiskLevel.FIRMWARE, RiskLevel.REBOOT}:
            return "hold", f"risk={risk.value} requires explicit human confirmation"

        # 2. Service restart / config change — hold unless config allows
        if risk in {RiskLevel.SERVICE_RESTART, RiskLevel.CONFIG_CHANGE}:
            # TODO: read allow_service_restart from loaded runtime config
            # For now, always hold these for safety
            return "hold", f"risk={risk.value} requires confirmation token"

        # 3. Safe or low risk — approve
        if risk in {RiskLevel.SAFE, RiskLevel.LOW}:
            return "approve", f"risk={risk.value} within auto-approve threshold"

        # 4. Default: hold unknown risk levels
        return "hold", f"unknown risk level {risk.value} — defaulting to hold"

    def _check_success_criteria(
        self,
        state: AgentState,
        evidence_items: list[str],
        actions_taken: list[str],
    ) -> None:
        """
        Observable signals needed to confirm each success criterion.

        All three must be confirmed with real tool output (not just flags) before
        we set the success flags.  This is a stub — replace with actual queries.
        """
        # queue_drained: CUPS or Windows queue has 0 pending jobs
        if not state.queue_drained:
            cups_clear = state.cups.queue_name and state.cups.pending_jobs == 0
            win_clear = state.windows.queue_name and state.windows.pending_jobs == 0
            if cups_clear or win_clear:
                # TODO: verify with a live lpstat / EnumJobs call before setting
                # For now, only set if we have at least one job-related evidence entry
                job_evidence = [
                    e for e in state.evidence
                    if "job" in e.source or "queue" in e.source
                ]
                if job_evidence:
                    state.queue_drained = True
                    ev = state.add_evidence(
                        specialist=self.name,
                        source="success_check",
                        content="queue_drained confirmed: job count = 0, backed by tool evidence.",
                    )
                    evidence_items.append(ev.evidence_id)
                    actions_taken.append("confirmed queue_drained")

        # device_ready: device reports "idle" / "ready" and no active alerts
        if not state.device_ready:
            if (
                state.device.printer_status in {"idle", "ready"}
                and not state.device.alerts
                and not state.device.error_codes
            ):
                state.device_ready = True
                ev = state.add_evidence(
                    specialist=self.name,
                    source="success_check",
                    content="device_ready confirmed: status=idle, no alerts, no error codes.",
                )
                evidence_items.append(ev.evidence_id)
                actions_taken.append("confirmed device_ready")

        # test_print_ok: requires an explicit test print result in evidence
        if not state.test_print_ok:
            test_evidence = [
                e for e in state.evidence
                if "test_print" in e.source
            ]
            if test_evidence and "success" in test_evidence[-1].content.lower():
                state.test_print_ok = True
                ev = state.add_evidence(
                    specialist=self.name,
                    source="success_check",
                    content="test_print_ok confirmed: test print evidence found.",
                )
                evidence_items.append(ev.evidence_id)
                actions_taken.append("confirmed test_print_ok")

        if state.is_resolved():
            logger.info("All success criteria confirmed by validation specialist.")