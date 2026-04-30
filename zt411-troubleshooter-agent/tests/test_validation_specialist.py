"""
ValidationSpecialist depth tests (Phase 3 — Session C).

Covers:
  * Risk-tiered guardrails per RiskLevel value.
  * Three-flag success criteria (queue_drained / device_ready /
    test_print_ok) with evidence grounding.
  * Hallucination guard: success flags set without backing evidence are
    reset and an audit item is emitted.
  * Regression for the Session B.5 short-circuit pattern (loop stuck on
    a human-action recommendation while a physical condition remains).

These tests do not exercise the orchestrator end-to-end — they construct
``AgentState`` directly and call ``ValidationSpecialist.act()`` once per
case. The pre-existing fixture-replay loop tests in
``test_agent_loop_pause_fixture.py`` remain the authority on full-loop
behaviour and are the regression net for the short-circuit path.
"""
from __future__ import annotations

import pytest

from zt411_agent.agent.validation_specialist import ValidationSpecialist
from zt411_agent.state import (
    ActionStatus,
    AgentState,
    LoopStatus,
    OSPlatform,
    RiskLevel,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(
    *,
    paused: bool | None = None,
    head_open: bool | None = None,
    media_out: bool | None = None,
    ribbon_out: bool | None = None,
    printer_status: str = "unknown",
    loop_counter: int = 0,
    error_codes: list[str] | None = None,
) -> AgentState:
    state = AgentState(
        os_platform=OSPlatform.LINUX,
        symptoms=["test"],
    )
    state.device.ip = "192.168.99.10"
    state.device.paused = paused
    state.device.head_open = head_open
    state.device.media_out = media_out
    state.device.ribbon_out = ribbon_out
    state.device.printer_status = printer_status
    state.device.error_codes = list(error_codes or [])
    state.loop_counter = loop_counter
    return state


def _validator() -> ValidationSpecialist:
    return ValidationSpecialist()


# ---------------------------------------------------------------------------
# 1a. Risk-tiered guardrails
# ---------------------------------------------------------------------------


class TestRiskTieredGuardrails:
    """Each risk level must take its prescribed branch:
    SAFE/LOW → auto-approve, SERVICE_RESTART/CONFIG_CHANGE → token,
    DESTRUCTIVE/FIRMWARE/REBOOT → human-only hold (no token).
    """

    @pytest.mark.parametrize("risk", [RiskLevel.SAFE, RiskLevel.LOW])
    def test_safe_and_low_auto_approve(self, risk):
        state = _make_state()
        entry = state.log_action(
            specialist="device_specialist",
            action="advise: trivial action",
            risk=risk,
            status=ActionStatus.PENDING,
            result="(no human action required)",
        )

        _validator().act(state)

        assert entry.status == ActionStatus.CONFIRMED, (
            f"{risk.value} should auto-approve to CONFIRMED"
        )
        approved = [
            ev for ev in state.evidence if ev.source == "guardrail_approved"
        ]
        assert approved, "expected guardrail_approved evidence"

    @pytest.mark.parametrize(
        "risk", [RiskLevel.SERVICE_RESTART, RiskLevel.CONFIG_CHANGE]
    )
    def test_service_restart_and_config_change_require_token(self, risk):
        state = _make_state()
        entry = state.log_action(
            specialist="windows_specialist",
            action="restart spooler",
            risk=risk,
            status=ActionStatus.PENDING,
            result="awaiting confirmation",
        )

        _validator().act(state)

        # Status stays PENDING until the human consumes the token.
        assert entry.status == ActionStatus.PENDING, (
            f"{risk.value} must remain PENDING after validator runs"
        )
        assert entry.confirmation_token, "expected a confirmation token"
        assert entry.confirmation_token in state.confirmation_tokens
        assert state.confirmation_tokens[entry.confirmation_token] == entry.entry_id

        token_evidence = [
            ev for ev in state.evidence
            if ev.source == "validation_guardrail_token"
        ]
        assert token_evidence, (
            "expected validation_guardrail_token audit item"
        )
        assert entry.confirmation_token in token_evidence[-1].content, (
            "audit item must include the token id"
        )

    def test_consume_confirmation_token_returns_entry_id(self):
        state = _make_state()
        entry = state.log_action(
            specialist="windows_specialist",
            action="restart spooler",
            risk=RiskLevel.SERVICE_RESTART,
            status=ActionStatus.PENDING,
            result="awaiting confirmation",
        )

        _validator().act(state)

        token = entry.confirmation_token
        consumed = state.consume_confirmation_token(token)
        assert consumed == entry.entry_id, (
            "consuming a valid token must return the action's entry_id"
        )
        assert token not in state.confirmation_tokens, (
            "token must be removed from the active set after consumption"
        )

    @pytest.mark.parametrize(
        "risk",
        [RiskLevel.DESTRUCTIVE, RiskLevel.FIRMWARE, RiskLevel.REBOOT],
    )
    def test_destructive_firmware_reboot_human_approval_only(self, risk):
        state = _make_state()
        entry = state.log_action(
            specialist="device_specialist",
            action="dangerous: factory reset device",
            risk=risk,
            status=ActionStatus.PENDING,
            result="awaiting human approval",
        )

        _validator().act(state)

        assert entry.status == ActionStatus.PENDING, (
            f"{risk.value} must NOT auto-approve"
        )
        # No token should be issued for human-only branches.
        assert not entry.confirmation_token, (
            f"{risk.value} must not issue a confirmation token "
            "(human-only approval path)"
        )

        high_risk_evidence = [
            ev for ev in state.evidence
            if ev.source == "validation_guardrail_high_risk"
        ]
        assert high_risk_evidence, (
            "expected validation_guardrail_high_risk audit item"
        )
        assert risk.value in high_risk_evidence[-1].content


# ---------------------------------------------------------------------------
# 1b. Three-flag success criteria (queue / device / test print)
# ---------------------------------------------------------------------------


class TestThreeFlagSuccessCriteria:
    def test_queue_drained_set_when_evidence_present(self):
        state = _make_state(printer_status="idle")
        state.add_evidence(
            specialist="cups_specialist",
            source="lpstat_jobs",
            content="lpstat -o reports 0 pending jobs on queue ZT411",
        )

        _validator().act(state)

        assert state.queue_drained is True
        success = [
            ev for ev in state.evidence
            if ev.source == "success_check"
            and "queue_drained" in ev.content
        ]
        assert success

    def test_queue_drained_not_set_without_evidence(self):
        state = _make_state(printer_status="idle")
        # No job-listing evidence at all.

        _validator().act(state)

        assert state.queue_drained is False

    def test_queue_drained_not_set_when_evidence_source_is_wrong(self):
        """An evidence item that mentions zero pending jobs but originated
        from a non-whitelisted source must NOT flip the flag — that's
        exactly the planner-hallucination shape we're guarding against.
        """
        state = _make_state(printer_status="idle")
        state.add_evidence(
            specialist="planner",
            source="rag_snippet",
            content="The manual states queues should have 0 pending jobs after a successful drain.",
        )

        _validator().act(state)

        assert state.queue_drained is False, (
            "evidence sourced from RAG / planner must not back queue_drained"
        )

    def test_device_ready_set_when_idle_and_no_alerts(self):
        state = _make_state(printer_status="idle")
        # No alerts, no error codes.

        _validator().act(state)

        assert state.device_ready is True

    def test_device_ready_tolerates_boot_alert_only(self):
        """``alert:1.15`` is the firmware boot-info entry that never clears
        without a power cycle. It must not block device_ready.
        """
        state = _make_state(
            printer_status="idle",
            error_codes=["alert:1.15"],
        )

        _validator().act(state)

        assert state.device_ready is True

    def test_device_ready_not_set_when_active_critical_alert(self):
        state = _make_state(
            printer_status="paused",
            error_codes=["alert:1.11", "alert:1.15"],
        )
        state.device.alerts = ["group=1,code=11,sev=3"]

        _validator().act(state)

        assert state.device_ready is False

    def test_test_print_ok_set_when_evidence_present(self):
        state = _make_state(printer_status="idle")
        state.add_evidence(
            specialist="cups_specialist",
            source="test_print",
            content="test print job submitted and reported success",
        )

        _validator().act(state)

        assert state.test_print_ok is True

    def test_test_print_ok_not_set_when_evidence_missing(self):
        state = _make_state(printer_status="idle")

        _validator().act(state)

        assert state.test_print_ok is False


# ---------------------------------------------------------------------------
# 1b/c. Hallucination guard
# ---------------------------------------------------------------------------


class TestHallucinationGuard:
    def test_planner_claim_without_evidence_does_not_resolve(self):
        """Simulate a planner that returned ``success_criteria_met=true``
        and somehow flipped the flags before validation runs (this is the
        attack surface we're closing). The validator must reset the
        flags, emit a hallucination_guard audit item, and leave
        ``state.is_resolved()`` False.
        """
        state = _make_state(printer_status="idle")
        # No backing evidence whatsoever.
        state.queue_drained = True
        state.test_print_ok = True
        state.device_ready = True

        _validator().act(state)

        # device_ready survives because status==idle and no alerts —
        # that path is fine. queue_drained and test_print_ok must both be
        # reset because no whitelisted evidence backs them.
        assert state.queue_drained is False
        assert state.test_print_ok is False
        assert state.is_resolved() is False, (
            "loop must not declare resolution when evidence is missing"
        )

        guard = [
            ev for ev in state.evidence
            if ev.source == "validation_hallucination_guard"
        ]
        assert guard, "expected validation_hallucination_guard audit item"
        # Audit must list which flags were reset.
        content = guard[-1].content
        assert "queue_drained" in content
        assert "test_print_ok" in content

    def test_all_three_flags_with_evidence_does_resolve(self):
        """The successful-resolution path: every flag has a backing
        evidence item from a whitelisted source.
        """
        state = _make_state(printer_status="idle")

        state.add_evidence(
            specialist="cups_specialist",
            source="lpstat_jobs",
            content="lpstat -o: 0 pending jobs",
        )
        state.add_evidence(
            specialist="cups_specialist",
            source="test_print",
            content="test print success — ZPL render OK",
        )

        _validator().act(state)

        assert state.queue_drained is True
        assert state.device_ready is True
        assert state.test_print_ok is True
        assert state.is_resolved() is True

        # No hallucination guard fired because every flag was supported.
        guards = [
            ev for ev in state.evidence
            if ev.source == "validation_hallucination_guard"
        ]
        assert not guards


# ---------------------------------------------------------------------------
# 1c. Session B.5 short-circuit regression
# ---------------------------------------------------------------------------


class TestShortCircuitRegression:
    """The Session B.5 termination-correctness path. The depth changes in
    Session C must not regress any of these properties.
    """

    def test_short_circuit_fires_when_paused_and_recommendation_outstanding(
        self,
    ):
        state = _make_state(
            paused=True,
            printer_status="paused",
            loop_counter=2,
        )
        state.log_action(
            specialist="device_specialist",
            action="advise: resume user-paused printer",
            risk=RiskLevel.LOW,
            status=ActionStatus.CONFIRMED,
            result="Awaiting human action on physical button.",
        )

        _validator().act(state)

        assert state.loop_status == LoopStatus.ESCALATED
        assert state.escalation_reason == "awaiting_human_action"
        sc = [
            ev for ev in state.evidence
            if ev.source == "validation_short_circuit"
        ]
        assert sc, "expected validation_short_circuit audit item"

    def test_short_circuit_does_not_fire_when_loop_counter_too_low(self):
        state = _make_state(
            paused=True,
            printer_status="paused",
            loop_counter=1,  # not yet > 1
        )
        state.log_action(
            specialist="device_specialist",
            action="advise: resume user-paused printer",
            risk=RiskLevel.LOW,
            status=ActionStatus.PENDING,
            result="Awaiting human action on physical button.",
        )

        _validator().act(state)

        assert state.loop_status == LoopStatus.RUNNING

    def test_short_circuit_does_not_fire_when_condition_cleared(self):
        """A clean resume between loop steps clears state.device.paused.
        The validator must not pre-empt a successful resume just because
        the prior PENDING/CONFIRMED entry remains in action_log.
        """
        state = _make_state(
            paused=False,
            printer_status="idle",
            loop_counter=3,
        )
        state.log_action(
            specialist="device_specialist",
            action="advise: resume user-paused printer",
            risk=RiskLevel.LOW,
            status=ActionStatus.CONFIRMED,
            result="Awaiting human action on physical button.",
        )

        _validator().act(state)

        assert state.loop_status == LoopStatus.RUNNING, (
            "must not escalate when the underlying physical condition has "
            "cleared between iterations"
        )


# ---------------------------------------------------------------------------
# 1d. Session C.5 fault short-circuit
# ---------------------------------------------------------------------------


def _seed_iter1_for_fault(state: AgentState, recommendation: str) -> None:
    """Mimic what DeviceSpecialist's fault branch emits during loop iter 1:
    a ``physical_recommendations`` evidence item carrying the human-readable
    advice, plus a high-level action_log entry summarising tool calls.
    The fault branch deliberately does NOT log an "Awaiting human action"
    result string — that's exactly the gap Session C.5 closes.
    """
    state.add_evidence(
        specialist="device_specialist",
        source="physical_recommendations",
        content=recommendation,
    )
    state.log_action(
        specialist="device_specialist",
        action="snmp_zt411_status; snmp_zt411_physical_flags",
        risk=RiskLevel.SAFE,
        status=ActionStatus.EXECUTED,
        result="emitted physical_recommendations evidence",
    )


class TestFaultShortCircuit:
    """Session C.5: the validator escalates with awaiting_human_action when
    a fault has been outstanding for one full prior loop iteration with no
    progress — same outcome as the B.5 paused path, generalised to cover
    head_open / media_out / ribbon_out fault states.
    """

    def _run_two_iterations(
        self,
        *,
        flag_name: str,
        printer_status: str,
        recommendation: str,
    ) -> AgentState:
        kwargs = {
            "head_open": None,
            "media_out": None,
            "ribbon_out": None,
            "paused": None,
        }
        kwargs[flag_name] = True
        state = _make_state(
            printer_status=printer_status,
            loop_counter=1,
            **kwargs,
        )
        _seed_iter1_for_fault(state, recommendation)

        validator = _validator()

        # Iteration 1: validator captures snapshot; must not short-circuit.
        validator.act(state)
        assert state.loop_status == LoopStatus.RUNNING, (
            "iter 1 must not short-circuit (loop_counter < 2 and no prior "
            "snapshot)"
        )

        # Iteration 2: same physical state, recommendation outstanding.
        state.loop_counter = 2
        validator.act(state)
        return state

    def test_short_circuit_fires_on_stuck_fault_head_open(self):
        state = self._run_two_iterations(
            flag_name="head_open",
            printer_status="paused",
            recommendation="Close printhead and latch firmly.",
        )

        assert state.loop_status == LoopStatus.ESCALATED
        assert state.escalation_reason == "awaiting_human_action"
        assert state.loop_counter <= 3
        sc = [
            ev for ev in state.evidence
            if ev.source == "validation_short_circuit"
        ]
        assert sc, "expected validation_short_circuit audit item"
        assert "head_open" in sc[-1].content

    def test_short_circuit_fires_on_stuck_fault_media_out(self):
        state = self._run_two_iterations(
            flag_name="media_out",
            printer_status="paused",
            recommendation="Load media roll and recalibrate (FEED button).",
        )

        assert state.loop_status == LoopStatus.ESCALATED
        assert state.escalation_reason == "awaiting_human_action"
        assert state.loop_counter <= 3
        sc = [
            ev for ev in state.evidence
            if ev.source == "validation_short_circuit"
        ]
        assert sc, "expected validation_short_circuit audit item"
        assert "media_out" in sc[-1].content

    def test_short_circuit_fires_on_stuck_fault_ribbon_out(self):
        state = self._run_two_iterations(
            flag_name="ribbon_out",
            printer_status="paused",
            recommendation="Install ribbon and re-thread through path.",
        )

        assert state.loop_status == LoopStatus.ESCALATED
        assert state.escalation_reason == "awaiting_human_action"
        assert state.loop_counter <= 3
        sc = [
            ev for ev in state.evidence
            if ev.source == "validation_short_circuit"
        ]
        assert sc, "expected validation_short_circuit audit item"
        assert "ribbon_out" in sc[-1].content

    def test_short_circuit_does_not_fire_when_state_changed(self):
        """The human acted between iterations: the fault flag flipped
        False and printer_status returned to idle. The validator must not
        pre-empt the loop's natural success path.
        """
        state = _make_state(
            head_open=True,
            printer_status="paused",
            loop_counter=1,
        )
        _seed_iter1_for_fault(
            state,
            "Close printhead and latch firmly.",
        )

        validator = _validator()
        validator.act(state)
        assert state.loop_status == LoopStatus.RUNNING

        # Human closes the printhead between iterations.
        state.device.head_open = False
        state.device.printer_status = "idle"
        state.loop_counter = 2

        validator.act(state)

        assert state.loop_status == LoopStatus.RUNNING, (
            "must not short-circuit when the physical condition has "
            "cleared between iterations"
        )
        sc = [
            ev for ev in state.evidence
            if ev.source == "validation_short_circuit"
        ]
        assert not sc, (
            "no short-circuit audit item should exist when state changed"
        )

    def test_short_circuit_does_not_fire_on_first_iteration_with_fault(self):
        """``loop_counter < 2`` guard: a single validator call on iter 1
        with a fault and a recommendation must not short-circuit even
        though the fault and recommendation are both present, because we
        haven't given the human a chance to act yet.
        """
        state = _make_state(
            head_open=True,
            printer_status="paused",
            loop_counter=1,
        )
        _seed_iter1_for_fault(
            state,
            "Close printhead and latch firmly.",
        )

        _validator().act(state)

        assert state.loop_status == LoopStatus.RUNNING
        sc = [
            ev for ev in state.evidence
            if ev.source == "validation_short_circuit"
        ]
        assert not sc
