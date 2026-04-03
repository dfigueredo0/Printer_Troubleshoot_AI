from __future__ import annotations

import logging
from typing import Any

from .base import Specialist
from .tools import (
    snmp_zt411_status,
    snmp_zt411_physical_flags,
    snmp_zt411_consumables,
    snmp_zt411_alerts,
    ipp_get_attributes,
    map_error_code_to_kb,
)
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
        """Query the ZT411 via SNMP / IPP and update state."""
        logger.info("DeviceSpecialist acting on session %s", state.session_id)

        actions_taken: list[str] = []
        evidence_items: list[str] = []
        ip = state.device.ip

        # ------------------------------------------------------------------
        # 1. SNMP status poll (model, firmware, serial, printer_status)
        # ------------------------------------------------------------------
        if state.device_unknown and ip != "unknown":
            r = snmp_zt411_status(ip)
            if r.success and r.output:
                d = r.output
                if d.get("zbr_model"):
                    state.device.model = str(d["zbr_model"])
                if d.get("zbr_firmware"):
                    state.device.firmware_version = str(d["zbr_firmware"])
                if d.get("printer_status"):
                    state.device.printer_status = str(d["printer_status"])
                content = (
                    f"SNMP status {ip}: model='{d.get('zbr_model', '')}' "
                    f"firmware='{d.get('zbr_firmware', '')}' "
                    f"status='{d.get('printer_status', '')}' "
                    f"sysDescr='{(d.get('sys_descr') or '')[:80]}'"
                )
            else:
                content = f"SNMP status poll failed: {r.error}"
            ev = state.add_evidence(specialist=self.name, source="snmp_status", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("snmp status poll")

        # ------------------------------------------------------------------
        # 2. SNMP physical flags (head_open, media_out, ribbon_out, paused)
        # ------------------------------------------------------------------
        if ip != "unknown" and any(
            f is None for f in [
                state.device.head_open,
                state.device.media_out,
                state.device.ribbon_out,
                state.device.paused,
            ]
        ):
            r = snmp_zt411_physical_flags(ip)
            if r.success and r.output:
                flags = r.output
                state.device.head_open = flags.get("head_open")
                state.device.media_out = flags.get("media_out")
                state.device.ribbon_out = flags.get("ribbon_out")
                state.device.paused = flags.get("paused")
                active = [k for k, v in flags.items() if v is True]
                content = (
                    f"Physical flags {ip}: head_open={flags.get('head_open')} "
                    f"media_out={flags.get('media_out')} "
                    f"ribbon_out={flags.get('ribbon_out')} "
                    f"paused={flags.get('paused')}"
                    + (f" — ACTIVE: {active}" if active else "")
                )
            else:
                # Zebra enterprise OIDs unavailable — note it but don't fail
                content = f"Physical flags via Zebra OIDs not available ({r.error}); check via front panel"
            ev = state.add_evidence(specialist=self.name, source="snmp_physical_flags", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("snmp physical flags")

        # ------------------------------------------------------------------
        # 3. SNMP consumables (ribbon %, media %)
        # ------------------------------------------------------------------
        if ip != "unknown" and not state.device.consumables:
            r = snmp_zt411_consumables(ip)
            if r.success and r.output:
                consumables = r.output.get("consumables", [])
                state.device.consumables = {c["name"]: c for c in consumables}
                low = [c for c in consumables if 0 <= c.get("pct", 100) < 20]
                content = (
                    f"Consumables {ip}: "
                    + "; ".join(
                        f"{c['name']}={c['pct']:.0f}%" if c.get("pct", -1) >= 0 else c["name"]
                        for c in consumables
                    )
                )
                if low:
                    content += f" — LOW: {[c['name'] for c in low]}"
            else:
                content = f"SNMP consumables read failed: {r.error}"
            ev = state.add_evidence(specialist=self.name, source="snmp_consumables", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("snmp consumables read")

        # ------------------------------------------------------------------
        # 4. SNMP alerts / error codes
        # ------------------------------------------------------------------
        if ip != "unknown" and not state.device.error_codes:
            r = snmp_zt411_alerts(ip)
            if r.success and r.output:
                alerts = r.output.get("alerts", [])
                error_codes = r.output.get("error_codes", [])
                state.device.alerts = alerts
                state.device.error_codes = error_codes
                content = (
                    f"SNMP alerts {ip}: {len(alerts)} alert(s) {alerts[:3]}; "
                    f"error_codes={error_codes}"
                )
            else:
                content = f"SNMP alerts read failed: {r.error}"
            ev = state.add_evidence(specialist=self.name, source="snmp_alerts", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("snmp alerts read")

        # ------------------------------------------------------------------
        # 5. IPP attribute read (secondary — fills gaps SNMP doesn't cover)
        # ------------------------------------------------------------------
        if ip != "unknown" and state.network.port_open.get(631):
            r = ipp_get_attributes(ip, port=631)
            if r.success and r.output:
                attrs = r.output.get("attributes", {})
                ipp_status = attrs.get("printer-state", "")
                ipp_reason = attrs.get("printer-state-reasons", "")
                if ipp_status and state.device.printer_status in ("unknown", ""):
                    _ipp_state_map = {"3": "idle", "4": "printing", "5": "stopped"}
                    state.device.printer_status = _ipp_state_map.get(str(ipp_status), str(ipp_status))
                content = (
                    f"IPP attributes {ip}:631 — state='{ipp_status}' reasons='{ipp_reason}' "
                    f"({len(attrs)} attribute(s) returned)"
                )
            else:
                content = f"IPP GET-PRINTER-ATTRIBUTES failed: {r.error}"
            ev = state.add_evidence(specialist=self.name, source="ipp_attributes", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("ipp get-printer-attributes")

        # ------------------------------------------------------------------
        # 6. Error code → Zebra KB citation mapper
        # ------------------------------------------------------------------
        for code in state.device.error_codes:
            kb = map_error_code_to_kb(code)
            content = (
                f"KB lookup error_code={code}: '{kb['title']}' — {kb['description']} "
                f"(ref: {kb['doc_ref']})"
            )
            ev = state.add_evidence(
                specialist=self.name,
                source="rag_error_kb",
                content=content,
                snippet_id=kb["doc_ref"],
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"kb lookup error {code}")

        # ------------------------------------------------------------------
        # 7. Physical action recommendations (if flags are set)
        # ------------------------------------------------------------------
        recommendations: list[str] = []
        if state.device.head_open:
            recommendations.append("Close printhead and latch.")
        if state.device.media_out:
            recommendations.append("Load media and recalibrate.")
        if state.device.ribbon_out:
            recommendations.append("Replace ribbon and re-thread.")
        if state.device.paused:
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
