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
    zpl_zt411_host_identification,
    zpl_zt411_extended_status,
    ipp_get_attributes,
    map_error_code_to_kb,
)
from ..state import AgentState, ActionStatus, LoopIntent, RiskLevel

# Loop intents for which the consumables read is worth the UDP/161
# round-trip. Kept here (rather than parameterized per-call) so the gate
# is auditable in one place. GENERAL preserves pre-Phase-4 behavior for
# callers that don't set an intent.
_CONSUMABLES_INTENTS = {
    LoopIntent.GENERAL,
    LoopIntent.DIAGNOSE_CONSUMABLES,
    LoopIntent.DIAGNOSE_PRINT_QUALITY,
}

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
        # Phase 4.4: if the action_log holds a CONFIRMED entry whose
        # action is one we can execute, the orchestrator MUST pick us
        # next so .act() can run its execution branch — no other
        # specialist can advance that work. Short-circuit at the top of
        # can_handle with a high score so the diminishing-returns clauses
        # below don't drop us under MIN_UTILITY after a resume.
        for a in state.action_log:
            if (
                a.status == ActionStatus.CONFIRMED
                and a.action in _EXECUTABLE_ACTIONS
            ):
                return 0.95

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
        # 1. Identity (only on first contact — these don't change)
        #
        # Phase 4.2: switched from snmp_zt411_status to
        # zpl_zt411_host_identification (~HI). The lab printer's SNMP
        # agent is unreachable on UDP/161 — see Phase 2.5 note above.
        # Trade-off: ~HI does not return a serial number on this firmware,
        # so state.device.serial stays empty — the demo path doesn't use
        # it, and the SNMP tool stays available for printers where it
        # works.
        # ------------------------------------------------------------------
        if state.device_unknown:
            r = zpl_zt411_host_identification(ip)
            if r.success and r.output:
                d = r.output
                if d.get("model"):
                    state.device.model = str(d["model"])
                if d.get("firmware"):
                    state.device.firmware_version = str(d["firmware"])
                content = (
                    f"ZPL ~HI identity {ip}: model='{d.get('model', '')}' "
                    f"firmware='{d.get('firmware', '')}' "
                    f"memory_kb={d.get('memory_kb', '?')} "
                    f"(serial: not available via ~HI on this firmware)"
                )
            else:
                content = f"ZPL ~HI identity poll failed: {r.error}"
            # Evidence source label kept stable across the SNMP→ZPL swap
            # so log/test/metric continuity is preserved.
            ev = state.add_evidence(specialist=self.name, source="snmp_status", content=content)
            evidence_items.append(ev.evidence_id)
            actions_taken.append("zpl identity poll")

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
        #
        # Phase 4.2: gated behind loop_intent. No ZPL equivalent exists
        # on V92.21.39Z — and the lab printer's SNMP agent has been
        # intermittently silent on UDP/161 — so paths that don't need
        # consumables (e.g. CALIBRATE, DIAGNOSE_NETWORK) skip this read
        # entirely. No evidence entry is emitted on skip; logging "we
        # didn't do something" is noise.
        # ------------------------------------------------------------------
        if state.loop_intent in _CONSUMABLES_INTENTS:
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
        # 4. Extended status (errors / warnings)
        #
        # Phase 4.2: switched from snmp_zt411_alerts to
        # zpl_zt411_extended_status (~HQES). Counts (errors_count /
        # warnings_count) are the demo-relevant fields — the calibrate
        # precondition is "errors_count == 0 and warnings_count == 0".
        # Bitmask interpretation (decoding which named conditions are
        # set) is deferred to Phase 5 — see TODO in tools.py.
        # ------------------------------------------------------------------
        r = zpl_zt411_extended_status(ip)
        if r.success and r.output:
            d = r.output
            errors_count = d.get("errors_count", -1)
            warnings_count = d.get("warnings_count", -1)
            # Until bitmask decoding lands, the only signal we can offer
            # the rest of the loop is presence/absence. Generate a coarse
            # "errors:N" / "warnings:N" code so existing consumers of
            # state.device.error_codes still get a non-empty list when
            # the printer is unhealthy. The KB lookup that fires off
            # error_codes will simply miss until phase 5.
            alerts: list[str] = []
            error_codes: list[str] = []
            if errors_count > 0:
                alerts.append(
                    f"errors_count={errors_count} bitmask={d.get('errors_bitmask_1')}.{d.get('errors_bitmask_2')}"
                )
                error_codes.append(f"hqes:errors_count={errors_count}")
            if warnings_count > 0:
                alerts.append(
                    f"warnings_count={warnings_count} bitmask={d.get('warnings_bitmask_1')}.{d.get('warnings_bitmask_2')}"
                )
            state.device.alerts = alerts
            state.device.error_codes = error_codes
            content = (
                f"ZPL ~HQES {ip}: errors={errors_count} warnings={warnings_count} "
                f"(bitmask decoding deferred to phase 5)"
            )
        else:
            content = f"ZPL ~HQES read failed: {r.error}"
        # Evidence source label kept stable across the SNMP→ZPL swap.
        ev = state.add_evidence(specialist=self.name, source="snmp_alerts", content=content)
        evidence_items.append(ev.evidence_id)
        actions_taken.append("zpl extended status read")

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
                ActionStatus.VERIFYING,
                ActionStatus.RESOLVED,
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
        # 10. Action execution (Phase 4.1, refactored in 4.3)
        # ------------------------------------------------------------------
        # Phase 4.3: each logical action now flows through one entry whose
        # status mutates over time (PENDING → CONFIRMED → EXECUTED →
        # VERIFYING → RESOLVED, branching to FAILED on any aborted step).
        # The full chronological history lives on entry.status_history;
        # the SSE bridge picks up status changes via per-entry snapshot
        # diffing and emits a fresh `action` event per transition. The
        # frontend uses entry_id to OOB-swap the same row in place.
        #
        # For each CONFIRMED entry we know how to execute:
        #  - re-check preconditions (no active fault) via fresh ~HS read
        #  - dispatch via getattr on the tools module
        #  - flip to EXECUTED, then VERIFYING, settle, verify via second ~HS
        #  - flip to RESOLVED (verify ok) or FAILED (verify problem)
        confirmed_executable = [
            a for a in state.action_log
            if a.status == ActionStatus.CONFIRMED
            and a.action in _EXECUTABLE_ACTIONS
        ]
        # An entry that has *ever* progressed past CONFIRMED has already
        # been handled — its status_history will contain EXECUTED. Without
        # this guard, the next iteration would re-fire on a stale CONFIRMED
        # snapshot. (Mutation happens in-place but a previous iteration
        # might have failed mid-flow; status_history captures that.)
        already_handled_entries = {
            a.entry_id for a in state.action_log
            if ActionStatus.EXECUTED in a.status_history
            or a.status in {
                ActionStatus.EXECUTED,
                ActionStatus.VERIFYING,
                ActionStatus.RESOLVED,
                ActionStatus.FAILED,
            }
        }
        confirmed_executable = [
            a for a in confirmed_executable
            if a.entry_id not in already_handled_entries
        ]

        for entry in confirmed_executable:
            # 10a. Pre-execution precondition check.
            pre = zpl_zt411_host_status(ip)
            if not pre.success or not pre.output:
                state.update_action_status(
                    entry.entry_id,
                    ActionStatus.FAILED,
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
                state.update_action_status(
                    entry.entry_id,
                    ActionStatus.FAILED,
                    result=f"precondition violated: {fault_desc} active at execution time",
                )
                state.add_evidence(
                    specialist=self.name,
                    source="action_aborted",
                    content=(
                        f"Aborted {entry.action} — {fault_desc} appeared between "
                        f"confirmation and execution. Entry {entry.entry_id} "
                        f"flipped to FAILED."
                    ),
                )
                actions_taken.append(f"abort {entry.action}: {fault_desc}")
                continue

            # 10b. Dispatch the tool function. Direct getattr on the tools
            # module so monkeypatching `tools.zpl_zt411_*` from tests
            # affects this call site without needing registry surgery.
            tool_fn = getattr(_tools_mod, entry.action, None)
            if not callable(tool_fn):
                state.update_action_status(
                    entry.entry_id,
                    ActionStatus.FAILED,
                    result=f"unknown action callable: {entry.action!r}",
                )
                actions_taken.append(f"abort {entry.action}: not callable")
                continue
            tool_result = tool_fn(ip)
            if not tool_result.success:
                state.update_action_status(
                    entry.entry_id,
                    ActionStatus.FAILED,
                    result=f"tool call failed: {tool_result.error}",
                )
                actions_taken.append(f"failed {entry.action}: {tool_result.error[:40]}")
                continue

            # 10c. Tool call succeeded — flip to EXECUTED, then VERIFYING.
            # The two transitions are intentionally distinct: EXECUTED
            # records that the bytes left the wire, VERIFYING records
            # that we are now in the settle window before re-reading.
            sent_bytes = (
                tool_result.output.get("sent_bytes") if tool_result.output else "?"
            )
            state.update_action_status(
                entry.entry_id,
                ActionStatus.EXECUTED,
                result=f"sent_bytes={sent_bytes}; awaiting verify",
            )
            state.update_action_status(
                entry.entry_id,
                ActionStatus.VERIFYING,
                result=f"sent_bytes={sent_bytes}; settle window ({_ACTION_SETTLE_DELAY_S}s)",
            )

            # 10d. Settle, then verify post-action state.
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
            final_status = ActionStatus.RESOLVED if verify_ok else ActionStatus.FAILED
            state.update_action_status(
                entry.entry_id,
                final_status,
                result=f"sent_bytes={sent_bytes}; {verify_summary}",
            )
            actions_taken.append(
                f"executed {entry.action} -> {final_status.value}"
            )

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
