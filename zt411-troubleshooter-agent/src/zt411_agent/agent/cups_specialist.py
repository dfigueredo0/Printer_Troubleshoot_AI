from __future__ import annotations

import logging
from typing import Any

from .base import Specialist
from .tools import (
    lpstat_v,
    lpstat_p,
    lpstat_jobs,
    cups_error_log,
    lpinfo_m,
    lpoptions,
    cupsenable,
    restart_cups,
    test_print,
)
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
            if state.cups.queue_state in {"stopped", "error"} or state.cups.pending_jobs > 0:
                score *= 0.6
            else:
                score *= 0.2

        return min(score, 1.0)

    def act(self, state: AgentState) -> dict[str, Any]:
        """Inspect CUPS queue/jobs and attempt low-risk fixes."""
        logger.info("CUPSSpecialist acting on session %s", state.session_id)

        actions_taken: list[str] = []
        evidence_items: list[str] = []

        # ------------------------------------------------------------------
        # 1. List queues (lpstat -v) and their status (lpstat -p)
        # ------------------------------------------------------------------
        if not state.cups.queue_name:
            r_v = lpstat_v()
            r_p = lpstat_p()

            if r_v.success and r_v.output:
                queues = r_v.output.get("queues", [])
                # Prefer queue whose device_uri looks like a Zebra / ZT411 device
                chosen = next(
                    (q for q in queues if "zt" in q["name"].lower() or "zebra" in q["device_uri"].lower()),
                    queues[0] if queues else None,
                )
                if chosen:
                    state.cups.queue_name = chosen["name"]
                    state.cups.device_uri = chosen["device_uri"]

            if r_p.success and r_p.output and state.cups.queue_name:
                printers = r_p.output.get("printers", [])
                for p in printers:
                    if p["name"] == state.cups.queue_name:
                        state.cups.queue_state = p["state"]
                        break

            content = (
                f"lpstat -v: {len((r_v.output or {}).get('queues', []))} queue(s); "
                f"selected='{state.cups.queue_name}' uri='{state.cups.device_uri}' "
                f"state='{state.cups.queue_state}'"
            )
            ev = state.add_evidence(specialist=self.name, source="lpstat_v", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("lpstat -v / lpstat -p")

        # ------------------------------------------------------------------
        # 2. Job list for the queue
        # ------------------------------------------------------------------
        if state.cups.queue_name:
            r = lpstat_jobs(state.cups.queue_name)
            if r.success and r.output:
                jobs = r.output.get("jobs", [])
                state.cups.pending_jobs = len(jobs)
                job_summary = (
                    "; ".join(f"id={j['id']} user={j['user']}" for j in jobs[:5]) or "no jobs"
                )
                content = f"lpstat -o '{state.cups.queue_name}': {len(jobs)} job(s) — {job_summary}"
            else:
                content = f"lpstat -o failed: {r.error}"
            ev = state.add_evidence(specialist=self.name, source="lpstat_jobs", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("lpstat -o queue")

        # ------------------------------------------------------------------
        # 3. CUPS error_log tail
        # ------------------------------------------------------------------
        if not state.cups.last_error_log:
            r = cups_error_log(n_lines=100)
            if r.success and r.output:
                filter_errors = r.output.get("filter_errors", [])
                state.cups.filter_errors = filter_errors[:20]
                # Store last N lines as the error log snapshot
                state.cups.last_error_log = "\n".join(r.output.get("lines", [])[-20:])
                content = (
                    f"CUPS error_log: {len(r.output.get('lines', []))} lines read; "
                    f"{len(filter_errors)} error/warning line(s)"
                )
                if filter_errors:
                    content += " — " + filter_errors[0]
            else:
                content = f"error_log read failed: {r.error}"
            ev = state.add_evidence(specialist=self.name, source="cups_error_log", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("read cups error_log")

        # ------------------------------------------------------------------
        # 4. PPD / driver validation (lpinfo -m + lpoptions)
        # ------------------------------------------------------------------
        if state.cups.queue_name and state.cups.ppd_valid is None:
            r_lpinfo = lpinfo_m()
            r_lpopts = lpoptions(state.cups.queue_name)

            zebra_models = (r_lpinfo.output or {}).get("zebra_models", []) if r_lpinfo.success else []
            opts = (r_lpopts.output or {}).get("options", {}) if r_lpopts.success else {}

            # Mark PPD valid if the driver name references Zebra or ZPL
            driver_name = state.cups.driver_name or ""
            ppd_ok = bool(
                zebra_models
                or "zebra" in driver_name.lower()
                or "zpl" in driver_name.lower()
                or opts
            )
            state.cups.ppd_valid = ppd_ok

            content = (
                f"PPD/driver check: zebra_models={len(zebra_models)} "
                f"lpoptions={len(opts)} ppd_valid={ppd_ok}"
            )
            ev = state.add_evidence(specialist=self.name, source="ppd_check", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("ppd validation (lpinfo -m + lpoptions)")

        # ------------------------------------------------------------------
        # 5. Re-enable a stopped queue (requires confirmation)
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
        # 6. CUPS service restart if filter errors present (requires confirmation)
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

        # ------------------------------------------------------------------
        # 7. Test print (only if queue is idle and no pending jobs)
        # ------------------------------------------------------------------
        if (
            state.cups.queue_name
            and state.cups.queue_state == "idle"
            and state.cups.pending_jobs == 0
            and self.name in state.visited_specialists  # second visit — queue looks clean, verify
        ):
            entry = state.log_action(
                specialist=self.name,
                action=f"lp -d {state.cups.queue_name} /dev/null (test print)",
                risk=RiskLevel.LOW,
                status=ActionStatus.PENDING,
                result="Pending confirmation",
            )
            token = state.issue_confirmation_token(entry.entry_id)
            ev = state.add_evidence(
                specialist=self.name,
                source="proposed_fix",
                content=f"Queue idle and clean — propose test print to verify. Token: {token}",
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"propose test print (token={token})")

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
