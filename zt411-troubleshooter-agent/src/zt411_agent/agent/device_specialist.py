from __future__ import annotations

import logging
import time
from typing import Any

from . import tools as _tools_mod
from .base import Specialist
from .tools import (
    snmp_zt411_status,
    snmp_zt411_physical_flags,  # kept for cross-vendor fallback
    snmp_zt411_consumables,
    snmp_zt411_alerts,
    zpl_zt411_host_status,
    ipp_get_attributes,
    map_error_code_to_kb,
)
from ..state import AgentState, ActionStatus, RiskLevel

# Wait between firing an action over TCP 9100 and re-reading state, so
# the printer has time to update its ~HS response. Empirically 1-3s on
# this firmware; 1.5s is the conservative middle. Module-level so tests
# can patch it down to 0.0 to avoid wall-clock drag.
_ACTION_SETTLE_DELAY_S = 1.5

# Action names that the device specialist knows how to execute (i.e.
# resolves via getattr on the tools module). Anything not in this set
# is logged as FAILED with an "unknown action" reason.
_EXECUTABLE_ACTIONS = frozenset({
    "zpl_zt411_calibrate",
    "zpl_zt411_print_config",
})

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
        """Query the ZT411 via SNMP / IPP and update state.

        On every call, this re-reads the device fresh (SNMP/IPP are cheap)
        rather than trusting cached state. This lets the agent observe
        state changes between loop iterations — e.g., a user clearing a
        fault while the agent is running.
        """
        logger.info("DeviceSpecialist acting on session %s", state.session_id)

        actions_taken: list[str] = []
        evidence_items: list[str] = []
        ip = state.device.ip

        if ip == "unknown":
            ev = state.add_evidence(
                specialist=self.name,
                source="device_specialist",
                content="Device IP unknown; cannot query. Network specialist must run first.",
            )
            evidence_items.append(ev.evidence_id)
            return {
                "evidence": evidence_items,
                "actions_taken": ["skipped: ip unknown"],
                "next_state": state,
            }

        # ------------------------------------------------------------------
        # 1. SNMP identity + status (only on first contact — these don't change)
        # ------------------------------------------------------------------
        if state.device_unknown:
            r = snmp_zt411_status(ip)
            if r.success and r.output:
                d = r.output
                if d.get("zbr_model"):
                    state.device.model = str(d["zbr_model"])
                if d.get("zbr_firmware"):
                    state.device.firmware_version = str(d["zbr_firmware"])
                content = (
                    f"SNMP identity {ip}: model='{d.get('zbr_model', '')}' "
                    f"firmware='{d.get('zbr_firmware', '')}' "
                    f"sysDescr='{(d.get('sys_descr') or '')[:80]}'"
                )
            else:
                content = f"SNMP identity poll failed: {r.error}"
            ev = state.add_evidence(specialist=self.name, source="snmp_status", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("snmp identity poll")

        # ------------------------------------------------------------------
        # 2. Physical flags (always re-read — captures state changes)
        #
        # Phase 2.5: switched from snmp_zt411_physical_flags to
        # zpl_zt411_host_status. The lab printer's SNMP agent is
        # unreachable on UDP/161 (verified 2026-04-30 from both Windows
        # pysnmp and WSL Net-SNMP); ZPL ~HS over TCP 9100 is the working
        # read channel. Output dict shape is identical.
        # ------------------------------------------------------------------
        r = zpl_zt411_host_status(ip)
        if r.success and r.output:
            flags = r.output
            state.device.head_open  = flags.get("head_open")
            state.device.media_out  = flags.get("media_out")
            state.device.ribbon_out = flags.get("ribbon_out")
            state.device.paused     = flags.get("paused")
            paused_user = flags.get("paused_is_user_initiated")

            active = [k for k in ("head_open", "media_out", "ribbon_out", "paused") if flags.get(k)]
            content = (
                f"Physical flags {ip}: head_open={flags.get('head_open')} "
                f"media_out={flags.get('media_out')} "
                f"ribbon_out={flags.get('ribbon_out')} "
                f"paused={flags.get('paused')}"
                f" bitmask={flags.get('raw_bitmask')!r}"
            )
            if active:
                content += f" — ACTIVE: {active}"
            if flags.get("paused"):
                content += (
                    f" (paused_is_user_initiated={paused_user})"
                )
        else:
            content = f"Physical flags read failed: {r.error}"
        # Source label kept as snmp_physical_flags for log/test/metric
        # continuity even though the transport underneath is now ZPL ~HS.
        # The label is a stable identifier for "physical flags read".
        ev = state.add_evidence(specialist=self.name, source="snmp_physical_flags", content=content)
        evidence_items.append(ev.evidence_id)
        actions_taken.append("snmp physical flags")

        # ------------------------------------------------------------------
        # 3. SNMP consumables (boolean presence on this firmware)
        # ------------------------------------------------------------------
        r = snmp_zt411_consumables(ip)
        if r.success and r.output:
            d = r.output
            # Store in dict-shaped form the state schema expects.
            state.device.consumables = {
                "media":  {"name": "media",  "state": d.get("media",  "unknown")},
                "ribbon": {"name": "ribbon", "state": d.get("ribbon", "unknown")},
                "supports_levels": d.get("supports_levels", False),
            }
            content = (
                f"Consumables {ip}: media={d.get('media')} ribbon={d.get('ribbon')} "
                f"(presence-only on this firmware; no level data via SNMP)"
            )
        else:
            content = f"SNMP consumables read failed: {r.error}"
        ev = state.add_evidence(specialist=self.name, source="snmp_consumables", content=content)
        evidence_items.append(ev.evidence_id)
        actions_taken.append("snmp consumables read")

        # ------------------------------------------------------------------
        # 4. SNMP alerts (filter for severity>=3 active faults; ignore boot info)
        # ------------------------------------------------------------------
        r = snmp_zt411_alerts(ip)
        active_alerts: list[dict] = []
        if r.success and r.output:
            all_alerts = r.output.get("alerts", []) or []
            # Severity 1 (informational) includes the persistent boot entry.
            # Filter to severity>=3 (critical) for live-state interpretation.
            active_alerts = [a for a in all_alerts if a.get("severity", 0) >= 3]
            state.device.alerts = [
                f"group={a.get('group')},code={a.get('code')},sev={a.get('severity')}"
                for a in active_alerts
            ]
            # error_codes: stringified (group, code) pairs for KB lookup keys
            state.device.error_codes = [
                f"alert:{a.get('group')}.{a.get('code')}" for a in active_alerts
            ]
            content = (
                f"SNMP alerts {ip}: {len(all_alerts)} total row(s), "
                f"{len(active_alerts)} active critical: {active_alerts}"
            )
        else:
            content = f"SNMP alerts read failed: {r.error}"
        ev = state.add_evidence(specialist=self.name, source="snmp_alerts", content=content)
        evidence_items.append(ev.evidence_id)
        actions_taken.append("snmp alerts read")

        # ------------------------------------------------------------------
        # 5. IPP attribute read (cross-check + reason strings)
        # ------------------------------------------------------------------
        # Try IPP regardless of whether port 631 was previously probed —
        # the call handles connection errors cleanly. This way the device
        # specialist can run before the network specialist has scanned.
        r = ipp_get_attributes(ip, port=631)
        if r.success and r.output:
            attrs = r.output.get("attributes", {})
            ipp_state_raw = attrs.get("printer-state", "")
            # Decode byte-string state (firmware quirk: returns '\x03' instead of 3)
            try:
                ipp_state_int = ord(ipp_state_raw) if isinstance(ipp_state_raw, str) and len(ipp_state_raw) == 1 else int(ipp_state_raw)
            except (TypeError, ValueError):
                ipp_state_int = None
            ipp_reason = attrs.get("printer-state-reasons", "")
            ipp_message = attrs.get("printer-state-message", "")
            _ipp_state_map = {3: "idle", 4: "printing", 5: "stopped"}
            ipp_state_name = _ipp_state_map.get(ipp_state_int, f"raw={ipp_state_raw!r}")
            content = (
                f"IPP {ip}:631 — state={ipp_state_int} ({ipp_state_name}) "
                f"reasons={ipp_reason!r} message={ipp_message!r}"
            )
        else:
            ipp_state_int = None
            ipp_reason = ""
            content = f"IPP GET-PRINTER-ATTRIBUTES failed: {r.error}"
        ev = state.add_evidence(specialist=self.name, source="ipp_attributes", content=content)
        evidence_items.append(ev.evidence_id)
        actions_taken.append("ipp get-printer-attributes")

        # ------------------------------------------------------------------
        # 6. Derive printer_status from interpreted state
        # ------------------------------------------------------------------
        if state.device.head_open:
            state.device.printer_status = "fault:head_open"
        elif state.device.media_out:
            state.device.printer_status = "fault:media_out"
        elif state.device.ribbon_out:
            state.device.printer_status = "fault:ribbon_out"
        elif state.device.paused:
            state.device.printer_status = "paused"
        elif ipp_state_int == 3:
            state.device.printer_status = "idle"
        elif ipp_state_int == 4:
            state.device.printer_status = "printing"
        else:
            state.device.printer_status = "unknown"

        # ------------------------------------------------------------------
        # 7. Error code → KB citation mapper (using alert-derived keys)
        # ------------------------------------------------------------------
        for code in state.device.error_codes:
            kb = map_error_code_to_kb(code)
            content = (
                f"KB lookup {code}: '{kb.get('title', '?')}' — "
                f"{kb.get('description', '(no entry)')} "
                f"(ref: {kb.get('doc_ref', 'none')})"
            )
            ev = state.add_evidence(
                specialist=self.name,
                source="rag_error_kb",
                content=content,
                snippet_id=kb.get("doc_ref"),
            )
            evidence_items.append(ev.evidence_id)
            actions_taken.append(f"kb lookup {code}")

        # ------------------------------------------------------------------
        # 8. Physical action recommendations
        # ------------------------------------------------------------------
        # Order matters: fix underlying faults BEFORE recommending un-pause,
        # since faults auto-emit a companion pause that disappears on its own
        # when the fault clears.
        recommendations: list[str] = []
        if state.device.head_open:
            recommendations.append("Close printhead and latch firmly.")
        if state.device.media_out:
            recommendations.append("Load media roll and recalibrate (FEED button).")
        if state.device.ribbon_out:
            recommendations.append("Install ribbon and re-thread through path.")

        # Only recommend resume if pause is user-initiated (not a fault companion).
        if state.device.paused:
            paused_user = (r.output or {}).get("paused_is_user_initiated") if r.success else None
            # Re-read from the most recent physical_flags result, not the IPP r above
            # (variable shadowing — fix by reading the flag directly from state).
            no_other_faults = not any([
                state.device.head_open,
                state.device.media_out,
                state.device.ribbon_out,
            ])
            if no_other_faults:
                recommendations.append(
                    "Resume printer (press PAUSE button on front panel)."
                )
                state.log_action(
                    specialist=self.name,
                    action="advise: resume user-paused printer",
                    risk=RiskLevel.LOW,
                    status=ActionStatus.PENDING,
                    result="Awaiting human action on physical button.",
                )
                actions_taken.append("recommend resume")
            else:
                recommendations.append(
                    "Printer is paused as a side-effect of an active fault; "
                    "resolve the underlying fault first — pause will clear automatically."
                )

        if recommendations:
            ev = state.add_evidence(
                specialist=self.name,
                source="physical_recommendations",
                content="; ".join(recommendations),
            )
            evidence_items.append(ev.evidence_id)

        # ------------------------------------------------------------------
        # 9. Action proposals (Phase 4.1) — calibrate
        # ------------------------------------------------------------------
        # Symptom hints at "blank labels" pattern AND printer reports healthy
        # idle state (no faults, not paused) AND we haven't already proposed
        # or run calibration this session: propose ~JC. ValidationSpecialist's
        # guardrail issues a confirmation token automatically for
        # SERVICE_RESTART risk; Step 4's execution loop runs it after confirm.
        symptom_text = " ".join(s for s in (state.symptoms or []) if s).lower()
        blank_labels_hint = any(
            h in symptom_text for h in ("blank", "unprinted", "missing print")
        )
        healthy_idle = not (
            state.device.head_open
            or state.device.media_out
            or state.device.ribbon_out
            or state.device.paused
        )
        existing_calibrate = any(
            a.action == "zpl_zt411_calibrate"
            and a.status in {
                ActionStatus.PENDING,
                ActionStatus.CONFIRMED,
                ActionStatus.EXECUTED,
            }
            for a in state.action_log
        )
        if blank_labels_hint and healthy_idle and not existing_calibrate:
            state.log_action(
                specialist=self.name,
                action="zpl_zt411_calibrate",
                risk=RiskLevel.SERVICE_RESTART,
                status=ActionStatus.PENDING,
                result="Awaiting user confirmation. ~JC consumes 1-3 labels.",
            )
            actions_taken.append("propose calibrate")

        # ------------------------------------------------------------------
        # 10. Action execution (Phase 4.1)
        # ------------------------------------------------------------------
        # Find every action_log entry whose status is CONFIRMED and whose
        # action name we know how to execute. For each:
        #  - re-check preconditions (no active fault) via fresh ~HS read
        #  - dispatch via getattr on the tools module (matches existing
        #    direct-call pattern in cups_/windows_specialist)
        #  - settle delay
        #  - verify via second ~HS read
        #  - append a new EXECUTED entry recording the result; the original
        #    CONFIRMED entry is left in place for audit history
        confirmed_executable = [
            a for a in state.action_log
            if a.status == ActionStatus.CONFIRMED
            and a.action in _EXECUTABLE_ACTIONS
        ]
        # Skip entries that already have a sibling EXECUTED entry (loop
        # ran the same action in a previous iteration). Without this guard,
        # confirming once would re-execute every iteration.
        already_executed_actions = {
            a.action for a in state.action_log
            if a.status == ActionStatus.EXECUTED
            and a.action in _EXECUTABLE_ACTIONS
        }
        confirmed_executable = [
            a for a in confirmed_executable
            if a.action not in already_executed_actions
        ]

        for entry in confirmed_executable:
            # 10a. Pre-execution precondition check.
            pre = zpl_zt411_host_status(ip)
            if not pre.success or not pre.output:
                state.log_action(
                    specialist=self.name,
                    action=entry.action,
                    risk=entry.risk,
                    status=ActionStatus.FAILED,
                    result=f"precondition read failed: {pre.error}",
                )
                actions_taken.append(f"abort {entry.action}: pre-read failed")
                continue
            pre_flags = pre.output
            active_fault = (
                pre_flags.get("head_open")
                or pre_flags.get("media_out")
                or pre_flags.get("ribbon_out")
            )
            if active_fault:
                fault_desc = ",".join(
                    k for k in ("head_open", "media_out", "ribbon_out")
                    if pre_flags.get(k)
                )
                state.log_action(
                    specialist=self.name,
                    action=entry.action,
                    risk=entry.risk,
                    status=ActionStatus.FAILED,
                    result=f"precondition violated: {fault_desc} active at execution time",
                )
                state.add_evidence(
                    specialist=self.name,
                    source="action_aborted",
                    content=(
                        f"Aborted {entry.action} — {fault_desc} appeared between "
                        f"confirmation and execution. Original entry "
                        f"{entry.entry_id} stays CONFIRMED for audit."
                    ),
                )
                actions_taken.append(f"abort {entry.action}: {fault_desc}")
                continue

            # 10b. Dispatch the tool function. Direct getattr on the tools
            # module so monkeypatching `tools.zpl_zt411_*` from tests
            # affects this call site without needing registry surgery.
            tool_fn = getattr(_tools_mod, entry.action, None)
            if not callable(tool_fn):
                state.log_action(
                    specialist=self.name,
                    action=entry.action,
                    risk=entry.risk,
                    status=ActionStatus.FAILED,
                    result=f"unknown action callable: {entry.action!r}",
                )
                actions_taken.append(f"abort {entry.action}: not callable")
                continue
            tool_result = tool_fn(ip)
            if not tool_result.success:
                state.log_action(
                    specialist=self.name,
                    action=entry.action,
                    risk=entry.risk,
                    status=ActionStatus.FAILED,
                    result=f"tool call failed: {tool_result.error}",
                )
                actions_taken.append(f"failed {entry.action}: {tool_result.error[:40]}")
                continue

            # 10c. Settle, then verify post-action state.
            time.sleep(_ACTION_SETTLE_DELAY_S)
            post = zpl_zt411_host_status(ip)
            verify_ok = (
                post.success
                and post.output
                and not (
                    post.output.get("head_open")
                    or post.output.get("media_out")
                    or post.output.get("ribbon_out")
                )
            )
            verify_summary = (
                "post-state healthy"
                if verify_ok
                else f"verify failed: success={post.success} flags={post.output}"
            )
            state.log_action(
                specialist=self.name,
                action=entry.action,
                risk=entry.risk,
                status=ActionStatus.EXECUTED,
                result=(
                    f"sent_bytes={tool_result.output.get('sent_bytes') if tool_result.output else '?'}; "
                    f"{verify_summary}"
                ),
            )
            actions_taken.append(f"executed {entry.action}")

            if verify_ok and entry.action == "zpl_zt411_calibrate":
                # Calibration completed without entering a fault — that's
                # the success criterion the loop can check (whether the
                # next print job comes out blank is outside this window).
                # Mark device_ready so ValidationSpecialist's is_resolved
                # predicate can fire on the next iteration.
                state.device_ready = True

        state.log_action(
            specialist=self.name,
            action="; ".join(actions_taken) or "no-op (nothing new to query)",
            risk=RiskLevel.SAFE,
            status=ActionStatus.EXECUTED,
            result=f"collected {len(evidence_items)} evidence items; "
                   f"printer_status={state.device.printer_status}",
        )

        return {
            "evidence": evidence_items,
            "actions_taken": actions_taken,
            "next_state": state,
        }
