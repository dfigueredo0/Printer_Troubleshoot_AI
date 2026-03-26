from __future__ import annotations

import logging
from typing import Any

from .base import Specialist
from ..state import AgentState, ActionStatus, RiskLevel

logger = logging.getLogger(__name__)

# Physical / device-layer error keywords we look for in symptoms
_DEVICE_KEYWORDS = {
    "ribbon", "media", "head", "jam", "calibrat", "firmware",
    "error", "pause", "beep", "blink", "alert", "offline",
    "ready", "reset", "reboot",
}

class DeviceSpecialist(Specialist):
    """
    Utility scoring logic
    ---------------------
    High score when:
    * Device status is still unknown — we need to read SNMP/IPP before anything else.
    * Device has active alerts or non-empty error codes.
    * Physical flags (head_open, media_out, ribbon_out, paused) are set.
    * Symptoms mention device-layer keywords.

    Lower score when:
    * Device status is "idle" / "ready" and no alerts → device is fine,
      problem is likely in the host/print-stack layer.
    * Already visited with a full read — diminishing returns.
    """

    name = "device_specialist"

    def can_handle(self, state: AgentState) -> float:  # noqa: D401
        score = 0.0

        # 1. Device status not yet known — must probe
        if state.device_unknown:
            score += 0.55

        # 2. Active device alerts or error codes reported
        if state.device.alerts:
            score += 0.3

        if state.device.error_codes:
            score += 0.25

        # 3. Physical problem flags
        physical_flags = [
            state.device.head_open,
            state.device.media_out,
            state.device.ribbon_out,
            state.device.paused,
        ]
        flagged = sum(1 for f in physical_flags if f is True)
        score += 0.15 * flagged  # up to +0.60 if all four are set

        # 4. Symptom keywords match device layer
        if state.has_device_symptoms:
            score += 0.2

        # 5. Printer explicitly not ready
        if state.device.printer_status in {"error", "offline", "stopped"}:
            score += 0.25

        # 6. Firmware version unknown — worth checking
        if state.device.firmware_version == "unknown":
            score += 0.05

        # --- Diminishing returns ---

        # Already visited AND device status now known with no active alerts
        if (
            self.name in state.visited_specialists
            and not state.device_unknown
            and not state.device.alerts
            and not state.device.error_codes
        ):
            score *= 0.2

        # Already visited but still has open issues — moderate reduction
        elif self.name in state.visited_specialists:
            score *= 0.6

        return min(score, 1.0)

    def act(self, state: AgentState) -> dict[str, Any]:
        """
        Query the ZT411 via SNMP / IPP and update state.

        Structured stub — TODO: replace each block with real pysnmp / httpx tool calls.
        """
        logger.info("DeviceSpecialist acting on session %s", state.session_id)

        actions_taken: list[str] = []
        evidence_items: list[str] = []

        ip = state.device.ip

        # ------------------------------------------------------------------
        # 1. SNMP status poll
        # ------------------------------------------------------------------
        if state.device_unknown and ip != "unknown":
            # TODO: real SNMP walk — prtGeneralPrinterStatus, hrDeviceStatus, etc.
            placeholder = f"SNMP status poll {ip} — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="snmp_status",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("snmp status poll")

        # ------------------------------------------------------------------
        # 2. SNMP consumables (ribbon %, media %)
        # ------------------------------------------------------------------
        if ip != "unknown" and not state.device.consumables:
            placeholder = f"SNMP consumables {ip} — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="snmp_consumables",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("snmp consumables read")

        # ------------------------------------------------------------------
        # 3. SNMP alerts / error OIDs
        # ------------------------------------------------------------------
        if ip != "unknown" and not state.device.error_codes:
            placeholder = f"SNMP error codes / alerts {ip} — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="snmp_alerts",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append("snmp alerts read")

        # ------------------------------------------------------------------
        # 4. Error code → Zebra KB lookup (RAG)
        # ------------------------------------------------------------------
        for code in state.device.error_codes:
            # TODO: trigger RAG lookup for this error code
            placeholder = f"RAG KB lookup for error code {code} — not yet implemented"
            ev = state.add_evidence(
                specialist=self.name,
                source="rag_error_kb",
                content=placeholder,
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"kb lookup error {code}")

        # ------------------------------------------------------------------
        # 5. Physical action recommendations (if flags are set)
        # ------------------------------------------------------------------
        recommendations: list[str] = []
        if state.device.head_open:
            recommendations.append("Close printhead and latch.")
        if state.device.media_out:
            recommendations.append("Load media and recalibrate.")
        if state.device.ribbon_out:
            recommendations.append("Replace ribbon and re-thread.")
        if state.device.paused:
            # Resuming via SNMP SET or front panel — low risk
            recommendations.append("Resume printer (front panel or SNMP SET prtGeneralReset).")
            state.log_action(
                specialist=self.name,
                action="advise: resume paused printer",
                risk=RiskLevel.LOW,
                status=ActionStatus.PENDING,
                result="Awaiting confirmation",
            )
            actions_taken.append("recommend resume")

        if recommendations:
            ev = state.add_evidence(
                specialist=self.name,
                source="physical_recommendations",
                content="; ".join(recommendations),
            )
            evidence_items.append(ev.evidence_id)

        state.log_action(
            specialist=self.name,
            action="; ".join(actions_taken) or "no-op (nothing new to query)",
            risk=RiskLevel.SAFE,
            status=ActionStatus.EXECUTED,
            result=f"collected {len(evidence_items)} evidence items",
        )

        return {
            "evidence": evidence_items,
            "actions_taken": actions_taken,
            "next_state": state,
        }