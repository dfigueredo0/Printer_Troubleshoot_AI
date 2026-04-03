from __future__ import annotations

import logging
from typing import Any

from .base import Specialist
from .tools import (
    ps_query_spooler,
    ps_enum_printers,
    ps_enum_jobs,
    ps_get_driver,
    ps_get_event_log,
    ps_restart_service,
    ps_cancel_job,
    ps_set_printer_online,
)
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
        """Inspect Windows print subsystem and attempt low-risk fixes."""
        logger.info("WindowsSpecialist acting on session %s", state.session_id)

        actions_taken: list[str] = []
        evidence_items: list[str] = []

        # ------------------------------------------------------------------
        # 1. Spooler service status
        # ------------------------------------------------------------------
        if state.windows.spooler_running is None:
            r = ps_query_spooler()
            if r.success and r.output:
                running = bool(r.output.get("running", False))
                state.windows.spooler_running = running
                content = (
                    f"Spooler running={running}; "
                    f"status={r.output.get('status', '')}; "
                    f"start_type={r.output.get('start_type', '')}"
                )
            else:
                content = f"Spooler query failed: {r.error}"

            ev = state.add_evidence(specialist=self.name, source="spooler_status", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("query spooler status")

        # ------------------------------------------------------------------
        # 2. Printer / queue enumeration
        # ------------------------------------------------------------------
        if not state.windows.queue_name:
            r = ps_enum_printers()
            if r.success and r.output:
                printers = r.output.get("printers", [])
                if printers:
                    # Prefer the first Zebra / ZT411 queue; fall back to first found
                    chosen = next(
                        (p for p in printers if "zt" in p["name"].lower() or "zebra" in p["driver"].lower()),
                        printers[0],
                    )
                    state.windows.queue_name = chosen["name"]
                    state.windows.driver_name = chosen["driver"]
                    state.windows.port_name = chosen["port"]
                    # Map PrinterStatus integer: 0=normal, 1=paused, 4=error, 7=offline
                    ps_int = int(chosen.get("printer_status", 0))
                    state.windows.queue_state = {0: "idle", 1: "paused", 4: "error", 7: "offline"}.get(
                        ps_int, str(ps_int)
                    )
                    content = (
                        f"Found {len(printers)} printer(s). "
                        f"Selected: '{chosen['name']}' driver='{chosen['driver']}' "
                        f"port='{chosen['port']}' status={chosen['printer_status']}"
                    )
                else:
                    content = "No printers found via Get-Printer"
            else:
                content = f"EnumPrinters failed: {r.error}"

            ev = state.add_evidence(specialist=self.name, source="enum_printers", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("enum printers")

        # ------------------------------------------------------------------
        # 3. Job list for queue
        # ------------------------------------------------------------------
        if state.windows.queue_name:
            r = ps_enum_jobs(state.windows.queue_name)
            if r.success and r.output:
                jobs = r.output.get("jobs", [])
                state.windows.pending_jobs = len(jobs)
                job_summary = (
                    "; ".join(
                        f"id={j['id']} doc='{j['document']}' status={j['status']}"
                        for j in jobs[:10]
                    )
                    or "no jobs"
                )
                content = f"Jobs in '{state.windows.queue_name}': {len(jobs)} — {job_summary}"
            else:
                content = f"EnumJobs failed: {r.error}"

            ev = state.add_evidence(specialist=self.name, source="enum_jobs", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("enum jobs")

        # ------------------------------------------------------------------
        # 4. Driver metadata check
        # ------------------------------------------------------------------
        if state.windows.queue_name and (
            not state.windows.driver_name or not state.windows.driver_version
        ):
            r = ps_get_driver(state.windows.queue_name)
            if r.success and r.output:
                state.windows.driver_name = r.output.get("name", state.windows.driver_name)
                state.windows.driver_version = r.output.get("version", "")
                state.windows.driver_isolation = r.output.get("isolation", "")
                content = (
                    f"Driver: name='{state.windows.driver_name}' "
                    f"version='{state.windows.driver_version}' "
                    f"isolation='{state.windows.driver_isolation}'"
                )
            else:
                content = f"Get-PrinterDriver failed: {r.error}"

            ev = state.add_evidence(specialist=self.name, source="driver_info", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("get driver info")

        # ------------------------------------------------------------------
        # 5. Event log: PrintService/Admin errors
        # ------------------------------------------------------------------
        if not state.windows.event_log_errors:
            r = ps_get_event_log(last_n=50)
            if r.success and r.output:
                errors = r.output.get("errors", [])
                state.windows.event_log_errors = errors[:20]  # cap at 20
                content = f"PrintService/Admin event log: {len(errors)} error(s) found"
                if errors:
                    content += " — " + "; ".join(errors[:3])
            else:
                content = f"Event log read failed: {r.error}"

            ev = state.add_evidence(specialist=self.name, source="event_log", content=content)
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
        # 7. Cancel stuck jobs (requires confirmation per job)
        # ------------------------------------------------------------------
        if state.windows.pending_jobs > 0 and state.windows.queue_name:
            r = ps_enum_jobs(state.windows.queue_name)
            if r.success and r.output:
                stuck_jobs = [
                    j for j in r.output.get("jobs", [])
                    if "error" in str(j.get("status", "")).lower()
                    or "deleting" in str(j.get("status", "")).lower()
                ]
                for job in stuck_jobs[:3]:  # cap at 3 to avoid token flood
                    entry = state.log_action(
                        specialist=self.name,
                        action=f"Remove-PrintJob -PrinterName '{state.windows.queue_name}' -ID {job['id']}",
                        risk=RiskLevel.LOW,
                        status=ActionStatus.PENDING,
                        result="Pending confirmation",
                    )
                    token = state.issue_confirmation_token(entry.entry_id)
                    ev = state.add_evidence(
                        specialist=self.name,
                        source="proposed_fix",
                        content=(
                            f"Stuck job id={job['id']} doc='{job['document']}' — "
                            f"propose cancel. Token: {token}"
                        ),
                    )
                    evidence_items.append(ev.evidence_id)
                    actions_taken.append(f"propose cancel job {job['id']} (token={token})")

        # ------------------------------------------------------------------
        # 8. Re-enable offline queue (low-risk)
        # ------------------------------------------------------------------
        if state.windows.queue_state == "offline" and state.windows.queue_name:
            entry = state.log_action(
                specialist=self.name,
                action=f"Set-Printer -Name '{state.windows.queue_name}' (re-enable online)",
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
