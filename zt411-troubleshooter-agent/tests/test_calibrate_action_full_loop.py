"""
End-to-end hermetic test for the Phase 4.1 calibrate action lifecycle.

Drives DeviceSpecialist + ValidationSpecialist directly across three
iterations to exercise the full PENDING -> CONFIRMED -> EXECUTED flow,
with a confirmation token consumed mid-loop to mimic the
``service/app.py::confirm_action`` endpoint without spinning up FastAPI.

What this test verifies:
  * symptom + idle ~HS read => calibrate proposed (PENDING) once
  * validator issues a confirmation token for the SERVICE_RESTART entry
  * after consume_confirmation_token + manual status flip (the FastAPI
    pattern), the next DeviceSpecialist iteration executes calibrate
  * the executed entry records ``post-state healthy`` and
    ``state.device_ready`` flips True
  * dedupe holds: a third iteration does not re-execute or re-propose
  * the action_log contains exactly the expected lifecycle entries

What this test does NOT exercise (out of scope, covered elsewhere):
  * orchestrator planner ranking (test_agent_loop_pause_fixture.py)
  * full FastAPI confirm round-trip (service/app.py has its own tests)
  * real-network ~JC behavior on the lab printer (recon-only finding;
    Step 4 ground-truth hand verification)
"""
from __future__ import annotations

from typing import List

import pytest

from zt411_agent.agent.device_specialist import DeviceSpecialist
import zt411_agent.agent.device_specialist as ds_mod
from zt411_agent.agent.validation_specialist import ValidationSpecialist
import zt411_agent.agent.tools as tools_mod
from zt411_agent.agent.tools import ToolResult
from zt411_agent.state import (
    ActionStatus,
    AgentState,
    OSPlatform,
    RiskLevel,
)
from fixtures.replay import make_fixture_replay


PRINTER_IP = "192.168.99.10"


@pytest.fixture
def calibrate_calls() -> dict:
    return {"count": 0, "ips": []}


@pytest.fixture
def patched_calibrate_happy_path(monkeypatch, calibrate_calls):
    """Patch all reads to idle baseline + stub zpl_zt411_calibrate as success.

    The settle delay is patched to 0.0 so the test isn't gated on
    wall-clock time.
    """
    replay = make_fixture_replay("zt411_fixture_idle_baseline.json")

    monkeypatch.setattr("zt411_agent.agent.tools.snmp_get", replay["snmp_get"])
    monkeypatch.setattr("zt411_agent.agent.tools.snmp_walk", replay["snmp_walk"])
    monkeypatch.setattr(
        "zt411_agent.agent.tools.ipp_get_attributes", replay["ipp_get_attributes"]
    )
    monkeypatch.setattr(
        "zt411_agent.agent.device_specialist.ipp_get_attributes",
        replay["ipp_get_attributes"],
    )
    monkeypatch.setattr(
        "zt411_agent.agent.tools.zpl_zt411_host_status",
        replay["zpl_zt411_host_status"],
    )
    monkeypatch.setattr(
        "zt411_agent.agent.device_specialist.zpl_zt411_host_status",
        replay["zpl_zt411_host_status"],
    )

    def stub_calibrate(ip, port=9100):
        calibrate_calls["count"] += 1
        calibrate_calls["ips"].append(ip)
        return ToolResult(success=True, output={"sent_bytes": 3})

    monkeypatch.setattr("zt411_agent.agent.tools.zpl_zt411_calibrate", stub_calibrate)
    monkeypatch.setattr(
        "zt411_agent.agent.device_specialist._ACTION_SETTLE_DELAY_S", 0.0
    )
    return replay


def _initial_state() -> AgentState:
    state = AgentState(
        os_platform=OSPlatform.LINUX,
        symptoms=["printer is printing blank labels"],
    )
    state.device.ip = PRINTER_IP
    return state


def _confirm_first_pending_calibrate(state: AgentState) -> str:
    """Mimic service/app.py::confirm_action: pull token, find entry, flip status."""
    pending = [
        a for a in state.action_log
        if a.action == "zpl_zt411_calibrate"
        and a.status == ActionStatus.PENDING
    ]
    assert pending, "no pending calibrate entry found to confirm"
    token = pending[0].confirmation_token
    assert token, f"validator should have issued a token (entry={pending[0].entry_id})"
    entry_id = state.consume_confirmation_token(token)
    assert entry_id == pending[0].entry_id
    for entry in state.action_log:
        if entry.entry_id == entry_id:
            entry.status = ActionStatus.CONFIRMED
            return entry_id
    raise AssertionError(f"entry_id {entry_id} not found in action_log")


def _calibrate_entries(state: AgentState) -> List:
    return [a for a in state.action_log if a.action == "zpl_zt411_calibrate"]


class TestCalibrateActionFullLoop:
    def test_iteration_1_proposes_pending(self, patched_calibrate_happy_path, calibrate_calls):
        state = _initial_state()
        DeviceSpecialist().act(state)
        ValidationSpecialist().act(state)

        cals = _calibrate_entries(state)
        assert len(cals) == 1
        assert cals[0].status == ActionStatus.PENDING
        assert cals[0].risk == RiskLevel.SERVICE_RESTART
        assert cals[0].confirmation_token, "validator must issue a token"
        assert calibrate_calls["count"] == 0, "must not execute before confirmation"

    def test_iteration_2_executes_after_confirm(
        self, patched_calibrate_happy_path, calibrate_calls
    ):
        state = _initial_state()
        ds = DeviceSpecialist()
        val = ValidationSpecialist()

        ds.act(state); val.act(state)
        _confirm_first_pending_calibrate(state)
        ds.act(state); val.act(state)

        cals = _calibrate_entries(state)
        statuses = [a.status for a in cals]
        assert ActionStatus.CONFIRMED in statuses
        assert ActionStatus.EXECUTED in statuses
        assert calibrate_calls["count"] == 1
        assert calibrate_calls["ips"] == [PRINTER_IP]

        executed = [a for a in cals if a.status == ActionStatus.EXECUTED]
        assert len(executed) == 1
        assert "post-state healthy" in executed[0].result
        assert "sent_bytes=3" in executed[0].result

    def test_device_ready_flips_after_executed(
        self, patched_calibrate_happy_path
    ):
        state = _initial_state()
        ds = DeviceSpecialist()
        val = ValidationSpecialist()

        ds.act(state); val.act(state)
        _confirm_first_pending_calibrate(state)
        ds.act(state); val.act(state)

        assert state.device_ready is True

    def test_dedupe_holds_across_third_iteration(
        self, patched_calibrate_happy_path, calibrate_calls
    ):
        state = _initial_state()
        ds = DeviceSpecialist()
        val = ValidationSpecialist()

        ds.act(state); val.act(state)
        _confirm_first_pending_calibrate(state)
        ds.act(state); val.act(state)
        ds.act(state); val.act(state)

        # Still exactly one tool call. No additional PENDING entries.
        assert calibrate_calls["count"] == 1
        pending_after = [
            a for a in _calibrate_entries(state) if a.status == ActionStatus.PENDING
        ]
        assert pending_after == []

    def test_full_lifecycle_entry_sequence(
        self, patched_calibrate_happy_path
    ):
        """The action_log entries for zpl_zt411_calibrate, in order,
        should walk PENDING -> CONFIRMED -> EXECUTED. No ABORTED enum
        churn — FAILED with a precondition message would appear here on
        the unhappy path."""
        state = _initial_state()
        ds = DeviceSpecialist()
        val = ValidationSpecialist()

        ds.act(state); val.act(state)
        _confirm_first_pending_calibrate(state)
        ds.act(state); val.act(state)

        statuses = [a.status for a in _calibrate_entries(state)]
        assert statuses == [ActionStatus.CONFIRMED, ActionStatus.EXECUTED], (
            f"unexpected lifecycle: {[s.value for s in statuses]}"
        )

    def test_active_fault_at_execution_aborts_with_FAILED(
        self, monkeypatch, calibrate_calls
    ):
        """If a fault appears between confirmation and execution, the
        action does NOT fire and a FAILED entry records the precondition
        violation. Original CONFIRMED entry stays for audit history."""
        # Iteration 1: idle baseline -> propose
        idle_replay = make_fixture_replay("zt411_fixture_idle_baseline.json")
        monkeypatch.setattr(
            "zt411_agent.agent.tools.snmp_get", idle_replay["snmp_get"]
        )
        monkeypatch.setattr(
            "zt411_agent.agent.tools.snmp_walk", idle_replay["snmp_walk"]
        )
        monkeypatch.setattr(
            "zt411_agent.agent.tools.ipp_get_attributes",
            idle_replay["ipp_get_attributes"],
        )
        monkeypatch.setattr(
            "zt411_agent.agent.device_specialist.ipp_get_attributes",
            idle_replay["ipp_get_attributes"],
        )
        monkeypatch.setattr(
            "zt411_agent.agent.tools.zpl_zt411_host_status",
            idle_replay["zpl_zt411_host_status"],
        )
        monkeypatch.setattr(
            "zt411_agent.agent.device_specialist.zpl_zt411_host_status",
            idle_replay["zpl_zt411_host_status"],
        )
        called = {"n": 0}

        def stub_calibrate(ip, port=9100):
            called["n"] += 1
            return ToolResult(success=True, output={"sent_bytes": 3})

        monkeypatch.setattr(
            "zt411_agent.agent.tools.zpl_zt411_calibrate", stub_calibrate
        )
        monkeypatch.setattr(
            "zt411_agent.agent.device_specialist._ACTION_SETTLE_DELAY_S", 0.0
        )

        state = _initial_state()
        ds = DeviceSpecialist()
        val = ValidationSpecialist()

        ds.act(state); val.act(state)
        _confirm_first_pending_calibrate(state)

        # Inject a head-open fault BEFORE execution
        head_replay = make_fixture_replay("zt411_fixture_head_open.json")
        monkeypatch.setattr(
            "zt411_agent.agent.tools.zpl_zt411_host_status",
            head_replay["zpl_zt411_host_status"],
        )
        monkeypatch.setattr(
            "zt411_agent.agent.device_specialist.zpl_zt411_host_status",
            head_replay["zpl_zt411_host_status"],
        )

        ds.act(state); val.act(state)

        cals = _calibrate_entries(state)
        failed = [a for a in cals if a.status == ActionStatus.FAILED]
        assert len(failed) == 1, f"expected 1 FAILED entry, got: {[a.status.value for a in cals]}"
        assert "precondition violated" in failed[0].result
        assert "head_open" in failed[0].result
        assert called["n"] == 0, "tool must not fire when precondition fails"
