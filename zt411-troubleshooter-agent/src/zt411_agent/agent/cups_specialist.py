from __future__ import annotations

import logging
from typing import Any

from .base import Specialist
from ..state import AgentState, ActionStatus, RiskLevel

logger = logging.getLogger(__name__)

_CUPS_KEYWORDS = {
    "cups", "lpd", "ipp", "ppd", "filter", "backend", "queue",
    "spooler", "driver", "job", "stuck", "pending",
}

class CUPSSpecialist(Specialist):
    """
    Utility scoring logic
    ---------------------
    Only meaningful on Linux / macOS; returns a very low score on Windows
    unless the user explicitly mentions CUPS.

    High score when:
    * OS is Linux and CUPS info is not yet populated.
    * CUPS queue is stopped or has pending/stuck jobs.
    * Filter errors are present.
    * PPD/driver not yet validated.
    * Symptoms mention CUPS-layer keywords.

    Lower when:
    * OS is Windows (CUPS very unlikely).
    * Queue is idle with 0 jobs → problem is elsewhere.
    * Already visited and queue is clean.
    """

    name = "cups_specialist"

    def can_handle(self, state: AgentState) -> float:  # noqa: D401
        score = 0.0

        # Gate: Windows makes CUPS very unlikely (but not impossible — WSL etc.)
        if state.os_is_windows:
            # Give a small floor in case someone mentions CUPS explicitly
            if any(k in " ".join(state.symptoms + [state.user_description]).lower() for k in _CUPS_KEYWORDS):
                return 0.15
            return 0.02

        # 1. Linux and CUPS state is unpopulated — first-time read
        if state.os_is_linux and not state.cups.queue_name:
            score += 0.5

        # 2. Queue in bad state
        if state.cups.queue_state in {"stopped", "error", "processing"}:
            score += 0.3

        # 3. Jobs stuck in queue
        if state.cups.pending_jobs > 0:
            score += 0.25

        # 4. Filter errors present
        if state.cups.filter_errors:
            score += 0.3

        # 5. PPD not yet validated
        if state.cups.ppd_valid is None and state.cups.queue_name:
            score += 0.1

        # 6. Symptom keywords
        combined = " ".join(state.symptoms + [state.user_description]).lower()
        if any(k in combined for k in _CUPS_KEYWORDS):
            score += 0.2

        # 7. Device URI missing even though we have a queue
        if state.cups.queue_name and not state.cups.device_uri:
            score += 0.1

        # --- Diminishing returns ---

        if self.name in state.visited_specialists:
            # Already checked; only stay relevant if queue still broken
            if state.cups.queue_state in {"stopped", "error"} or state.cups.pending_jobs > 0:
                score *= 0.6
            else:
                score *= 0.2

        return min(score, 1.0)

    def act(self, state: AgentState) -> dict[str, Any]:
        """
        Inspect CUPS queue/jobs and attempt low-risk fixes.

        Structured stub — replace each block with real subprocess / lpstat calls.
        """
        logger.info("CUPSSpecialist acting on session %s", state.session_id)

        actions_taken: list[str] = []
        evidence_items: list[str] = []

        # ------------------------------------------------------------------
        # 1. List queues (lpstat -v / lpstat -p)
        # ------------------------------------------------------------------
        if not state.cups.queue_name:
            # TODO: subprocess.run(["lpstat", "-v"])
            placeholder = "lpstat -v output — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="lpstat_v",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("lpstat -v")

        # ------------------------------------------------------------------
        # 2. Job list for the queue
        # ------------------------------------------------------------------
        if state.cups.queue_name:
            # TODO: subprocess.run(["lpstat", "-o", state.cups.queue_name])
            placeholder = f"lpstat -o {state.cups.queue_name} — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="lpstat_jobs",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("lpstat -o queue")

        # ------------------------------------------------------------------
        # 3. CUPS error_log tail
        # ------------------------------------------------------------------
        if not state.cups.last_error_log:
            # TODO: read /var/log/cups/error_log (last N lines)
            placeholder = "tail /var/log/cups/error_log — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="cups_error_log",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("read cups error_log")

        # ------------------------------------------------------------------
        # 4. PPD / driver validation
        # ------------------------------------------------------------------
        if state.cups.queue_name and state.cups.ppd_valid is None:
            # TODO: lpinfo -m | grep Zebra; lpoptions -p <queue> -l
            placeholder = f"PPD/driver check for {state.cups.queue_name} — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="ppd_check",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("ppd validation")

        # ------------------------------------------------------------------
        # 5. Re-enable a stopped queue (low risk — no confirmation needed)
        # ------------------------------------------------------------------
        if state.cups.queue_state == "stopped" and state.cups.queue_name:
            entry = state.log_action(
                specialist=self.name,
                action=f"cupsenable {state.cups.queue_name}",
                risk=RiskLevel.SERVICE_RESTART,
                status=ActionStatus.PENDING,
                result="Pending confirmation",
            )
            token = state.issue_confirmation_token(entry.entry_id)
            ev = state.add_evidence(
                specialist=self.name,
                source="proposed_fix",
                content=f"Queue stopped — propose cupsenable. Confirmation token: {token}",
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"propose cupsenable (token={token})")

        # ------------------------------------------------------------------
        # 6. CUPS service restart (requires confirmation)
        # ------------------------------------------------------------------
        if state.cups.filter_errors and state.cups.queue_name:
            entry = state.log_action(
                specialist=self.name,
                action="systemctl restart cups",
                risk=RiskLevel.SERVICE_RESTART,
                status=ActionStatus.PENDING,
                result="Pending confirmation",
            )
            token = state.issue_confirmation_token(entry.entry_id)
            ev = state.add_evidence(
                specialist=self.name,
                source="proposed_fix",
                content=f"Filter errors found — propose CUPS restart. Token: {token}",
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"propose cups restart (token={token})")

        state.log_action(
            specialist=self.name,
            action="; ".join(actions_taken) or "no-op",
            risk=RiskLevel.SAFE,
            status=ActionStatus.EXECUTED,
            result=f"collected {len(evidence_items)} evidence items",
        )

        return {
            "evidence": evidence_items,
            "actions_taken": actions_taken,
            "next_state": state,
        }