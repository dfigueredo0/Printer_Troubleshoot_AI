from __future__ import annotations

import logging
from typing import Any

from .base import Specialist
from ..state import AgentState, ActionStatus, RiskLevel

logger = logging.getLogger(__name__)

_WIN_KEYWORDS = {
    "spooler", "driver", "windows", "win", "queue", "stuck", "offline",
    "type 3", "type 4", "package", "powershell", "wsd", "tcp", "lpr",
}

class WindowsSpecialist(Specialist):
    """
    Utility scoring logic
    ---------------------
    Relevant only on Windows; near-zero score on Linux unless CUPS gives up.

    High score when:
    * OS is Windows and print subsystem is uninspected.
    * Spooler is not running.
    * Queue has stuck/pending jobs.
    * Driver name or version is unknown or flagged as stale.
    * Symptoms mention Windows print-stack keywords.

    Lower when:
    * OS is Linux / macOS.
    * Spooler running, queue empty, driver valid → problem is elsewhere.
    * Already visited with no new info to collect.
    """

    name = "windows_specialist"

    def can_handle(self, state: AgentState) -> float:  # noqa: D401
        score = 0.0

        # Gate: Linux/macOS — only activate if explicitly mentioned
        if state.os_is_linux:
            combined = " ".join(state.symptoms + [state.user_description]).lower()
            if any(k in combined for k in _WIN_KEYWORDS):
                return 0.12
            return 0.01

        # From here: Windows or Unknown OS

        # 1. OS is Windows and we have not inspected the print subsystem yet
        if state.os_is_windows and state.windows.spooler_running is None:
            score += 0.6

        # 2. OS unknown but symptoms strongly suggest Windows stack
        combined = " ".join(state.symptoms + [state.user_description]).lower()
        if any(k in combined for k in _WIN_KEYWORDS):
            score += 0.25

        # 3. Spooler not running — high value to fix
        if state.windows.spooler_running is False:
            score += 0.45

        # 4. Queue stuck / jobs pending
        if state.windows.queue_state in {"paused", "error", "offline"}:
            score += 0.3
        if state.windows.pending_jobs > 0:
            score += 0.2

        # 5. Driver info missing / potentially stale
        if state.windows.driver_name == "" or state.windows.driver_version == "":
            score += 0.15

        # 6. Event log errors present
        if state.windows.event_log_errors:
            score += 0.2

        # --- Diminishing returns ---

        if self.name in state.visited_specialists:
            still_broken = (
                state.windows.spooler_running is False
                or state.windows.pending_jobs > 0
                or state.windows.queue_state in {"paused", "error"}
            )
            score *= 0.55 if still_broken else 0.15

        return min(score, 1.0)

    def act(self, state: AgentState) -> dict[str, Any]:
        """
        Inspect Windows print subsystem and attempt low-risk fixes.

        Structured stub — replace each block with real pywin32 / PowerShell calls.
        """
        logger.info("WindowsSpecialist acting on session %s", state.session_id)

        actions_taken: list[str] = []
        evidence_items: list[str] = []

        # ------------------------------------------------------------------
        # 1. Spooler service status
        # ------------------------------------------------------------------
        if state.windows.spooler_running is None:
            # TODO: win32serviceutil.QueryServiceStatus("Spooler") or
            #       subprocess.run(["sc", "query", "Spooler"])
            placeholder = "Spooler service query — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="spooler_status",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("query spooler status")

        # ------------------------------------------------------------------
        # 2. Printer / queue enumeration
        # ------------------------------------------------------------------
        if not state.windows.queue_name:
            # TODO: win32print.EnumPrinters() / Get-Printer PowerShell
            placeholder = "EnumPrinters — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="enum_printers",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("enum printers")

        # ------------------------------------------------------------------
        # 3. Job list for queue
        # ------------------------------------------------------------------
        if state.windows.queue_name:
            # TODO: win32print.EnumJobs()
            placeholder = f"EnumJobs({state.windows.queue_name}) — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="enum_jobs",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("enum jobs")

        # ------------------------------------------------------------------
        # 4. Driver metadata check
        # ------------------------------------------------------------------
        if not state.windows.driver_name:
            # TODO: Get-PrinterDriver PowerShell
            placeholder = "Get-PrinterDriver — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="driver_info",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("get driver info")

        # ------------------------------------------------------------------
        # 5. Event log: PrintService/Admin errors
        # ------------------------------------------------------------------
        if not state.windows.event_log_errors:
            # TODO: Get-WinEvent -LogName "Microsoft-Windows-PrintService/Admin"
            placeholder = "PrintService/Admin event log — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="event_log",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("read event log")

        # ------------------------------------------------------------------
        # 6. Restart spooler if not running (requires confirmation)
        # ------------------------------------------------------------------
        if state.windows.spooler_running is False:
            entry = state.log_action(
                specialist=self.name,
                action="Restart-Service Spooler",
                risk=RiskLevel.SERVICE_RESTART,
                status=ActionStatus.PENDING,
                result="Pending confirmation",
            )
            token = state.issue_confirmation_token(entry.entry_id)
            ev = state.add_evidence(
                specialist=self.name,
                source="proposed_fix",
                content=f"Spooler stopped — propose restart. Confirmation token: {token}",
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"propose spooler restart (token={token})")

        # ------------------------------------------------------------------
        # 7. Re-enable offline queue (low-risk)
        # ------------------------------------------------------------------
        if state.windows.queue_state == "offline" and state.windows.queue_name:
            entry = state.log_action(
                specialist=self.name,
                action=f"Set-Printer -Name '{state.windows.queue_name}' -Published $true (re-enable)",
                risk=RiskLevel.LOW,
                status=ActionStatus.PENDING,
                result="Pending confirmation",
            )
            token = state.issue_confirmation_token(entry.entry_id)
            ev = state.add_evidence(
                specialist=self.name,
                source="proposed_fix",
                content=f"Queue offline — propose re-enable. Token: {token}",
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"propose queue re-enable (token={token})")

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