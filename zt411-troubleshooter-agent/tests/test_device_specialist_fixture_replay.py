"""
DeviceSpecialist behavior under fixture replay.

These tests exercise the entire DeviceSpecialist.act() flow without a real
printer by monkeypatching SNMP/IPP calls with the fixture-replay helpers
in tests/fixtures/replay.py.

What this verifies:
  * SNMP identity, physical flags, consumables, alerts paths each populate
    state.device with the values implied by the canned fixture.
  * printer_status is derived consistently from those flags.
  * Pause vs fault discrimination (alert table cross-check) recommends
    Resume only when the pause is user-initiated and no other faults exist.

What this DOES NOT verify (limitation, not a goal):
  * On the four physical-fault fixtures (head_open / media_out / ribbon_out)
    the live state bitmask captured at OID 10642.2.10.3.7.0 has the
    fault bit set in part 2 (e.g. ``00010004`` for head_open) but
    snmp_zt411_physical_flags reads only part 1, which is zero. So the
    boolean head_open / media_out / ribbon_out fields are all False even
    though the printer reports the fault. The alert table still surfaces
    the correct (group, code) pair, so error_codes are populated; only
    the flag-derived printer_status is misleading. Documenting current
    behaviour here so the gap is visible to the next session.
"""
from __future__ import annotations

import pytest

from zt411_agent.agent.device_specialist import DeviceSpecialist
from zt411_agent.state import (
    ActionStatus,
    AgentState,
    OSPlatform,
    RiskLevel,
)
from fixtures.replay import make_fixture_replay


PRINTER_IP = "192.168.99.10"


def _patch_replay(monkeypatch, fixture_name: str) -> None:
    """Bind fixture replay callables in place of real SNMP/IPP."""
    replay = make_fixture_replay(fixture_name)
    monkeypatch.setattr(
        "zt411_agent.agent.tools.snmp_get", replay["snmp_get"]
    )
    monkeypatch.setattr(
        "zt411_agent.agent.tools.snmp_walk", replay["snmp_walk"]
    )
    monkeypatch.setattr(
        "zt411_agent.agent.tools.ipp_get_attributes",
        replay["ipp_get_attributes"],
    )
    # device_specialist imports ipp_get_attributes directly into its
    # namespace, so patching only the tools module is not enough.
    monkeypatch.setattr(
        "zt411_agent.agent.device_specialist.ipp_get_attributes",
        replay["ipp_get_attributes"],
    )
    # Phase 2.5: device_specialist reads physical flags via
    # zpl_zt411_host_status (lab printer SNMP unreachable). Patch both
    # namespaces — same reason as ipp_get_attributes above.
    monkeypatch.setattr(
        "zt411_agent.agent.tools.zpl_zt411_host_status",
        replay["zpl_zt411_host_status"],
    )
    monkeypatch.setattr(
        "zt411_agent.agent.device_specialist.zpl_zt411_host_status",
        replay["zpl_zt411_host_status"],
    )
    # Phase 4.2: identity / alerts now via ZPL ~HI / ~HQES.
    monkeypatch.setattr(
        "zt411_agent.agent.tools.zpl_zt411_host_identification",
        replay["zpl_zt411_host_identification"],
    )
    monkeypatch.setattr(
        "zt411_agent.agent.device_specialist.zpl_zt411_host_identification",
        replay["zpl_zt411_host_identification"],
    )
    monkeypatch.setattr(
        "zt411_agent.agent.tools.zpl_zt411_extended_status",
        replay["zpl_zt411_extended_status"],
    )
    monkeypatch.setattr(
        "zt411_agent.agent.device_specialist.zpl_zt411_extended_status",
        replay["zpl_zt411_extended_status"],
    )


def _initial_state() -> AgentState:
    state = AgentState(
        os_platform=OSPlatform.LINUX,
        symptoms=["printer paused"],
    )
    state.device.ip = PRINTER_IP
    return state


# ---------------------------------------------------------------------------
# Idle baseline
# ---------------------------------------------------------------------------


class TestIdleBaseline:
    def test_printer_status_is_idle(self, monkeypatch):
        _patch_replay(monkeypatch, "zt411_fixture_idle_baseline.json")
        state = _initial_state()
        DeviceSpecialist().act(state)
        assert state.device.printer_status == "idle"

    def test_no_physical_flags_set(self, monkeypatch):
        _patch_replay(monkeypatch, "zt411_fixture_idle_baseline.json")
        state = _initial_state()
        DeviceSpecialist().act(state)
        assert state.device.head_open is False
        assert state.device.media_out is False
        assert state.device.ribbon_out is False
        assert state.device.paused is False

    def test_no_alerts(self, monkeypatch):
        _patch_replay(monkeypatch, "zt411_fixture_idle_baseline.json")
        state = _initial_state()
        DeviceSpecialist().act(state)
        assert state.device.alerts == []
        assert state.device.error_codes == []

    def test_identity_populated(self, monkeypatch):
        _patch_replay(monkeypatch, "zt411_fixture_idle_baseline.json")
        state = _initial_state()
        DeviceSpecialist().act(state)
        assert state.device.firmware_version == "V92.21.39Z"
        assert "ZT411" in state.device.model

    def test_no_pending_recommendations(self, monkeypatch):
        _patch_replay(monkeypatch, "zt411_fixture_idle_baseline.json")
        state = _initial_state()
        DeviceSpecialist().act(state)
        pending = [a for a in state.action_log if a.status == ActionStatus.PENDING]
        assert pending == []


# ---------------------------------------------------------------------------
# Paused (user-initiated) — the canonical happy path for Session A
# ---------------------------------------------------------------------------


class TestPausedFixture:
    def test_printer_status_is_paused(self, monkeypatch):
        _patch_replay(monkeypatch, "zt411_fixture_paused.json")
        state = _initial_state()
        DeviceSpecialist().act(state)
        assert state.device.printer_status == "paused"

    def test_paused_flag_true_no_other_faults(self, monkeypatch):
        _patch_replay(monkeypatch, "zt411_fixture_paused.json")
        state = _initial_state()
        DeviceSpecialist().act(state)
        assert state.device.paused is True
        assert state.device.head_open is False
        assert state.device.media_out is False
        assert state.device.ribbon_out is False

    def test_pause_does_not_register_as_error_via_hqes(self, monkeypatch):
        _patch_replay(monkeypatch, "zt411_fixture_paused.json")
        state = _initial_state()
        DeviceSpecialist().act(state)
        # Phase 4.2: alerts come from ~HQES counts, not the SNMP alert
        # table. On this firmware, a user-pressed pause does not increment
        # errors_count or warnings_count — pause is surfaced via ~HS only.
        # The granularity loss vs. the old SNMP path is documented in the
        # findings doc and tracked for phase 5 bitmask decoding.
        assert state.device.error_codes == []
        assert state.device.alerts == []

    def test_resume_recommendation_logged_pending_low_risk(self, monkeypatch):
        _patch_replay(monkeypatch, "zt411_fixture_paused.json")
        state = _initial_state()
        DeviceSpecialist().act(state)
        resume_entries = [
            a for a in state.action_log
            if a.action.startswith("advise: resume")
        ]
        assert len(resume_entries) == 1
        entry = resume_entries[0]
        assert entry.status == ActionStatus.PENDING
        assert entry.risk == RiskLevel.LOW

    def test_resume_recommendation_evidence_present(self, monkeypatch):
        _patch_replay(monkeypatch, "zt411_fixture_paused.json")
        state = _initial_state()
        DeviceSpecialist().act(state)
        rec_evidence = [
            ev for ev in state.evidence
            if ev.source == "physical_recommendations"
        ]
        assert len(rec_evidence) == 1
        assert "Resume" in rec_evidence[0].content

    def test_evidence_has_snmp_physical_flags_and_alerts(self, monkeypatch):
        _patch_replay(monkeypatch, "zt411_fixture_paused.json")
        state = _initial_state()
        DeviceSpecialist().act(state)
        sources = {ev.source for ev in state.evidence}
        assert "snmp_physical_flags" in sources
        assert "snmp_alerts" in sources


# ---------------------------------------------------------------------------
# Physical-fault fixtures — alert table is the source of truth here
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture_name",
    [
        "zt411_fixture_head_open.json",
        "zt411_fixture_media_out.json",
        "zt411_fixture_ribbon_out.json",
    ],
)

class TestFaultFixtures:
    """Fault fixtures should report errors_count > 0 via ~HQES, plus the
    paused flag via ~HS (every fault emits a companion pause).

    Phase 4.2: error_codes lost the named (group, code) granularity
    because the SNMP alert-table read was retired. error_codes now
    contains a synthetic 'hqes:errors_count=N' marker until phase 5
    bitmask decoding lands.
    """

    def test_errors_count_in_error_codes(
        self, monkeypatch, fixture_name
    ):
        _patch_replay(monkeypatch, fixture_name)
        state = _initial_state()
        DeviceSpecialist().act(state)
        assert any(c.startswith("hqes:errors_count=") for c in state.device.error_codes), (
            f"expected an hqes:errors_count marker in {state.device.error_codes!r}"
        )

    def test_alerts_populated_with_bitmask(
        self, monkeypatch, fixture_name
    ):
        _patch_replay(monkeypatch, fixture_name)
        state = _initial_state()
        DeviceSpecialist().act(state)
        # state.device.alerts captures the bitmask string for later phase-5
        # decoding. Just assert non-empty + bitmask field present.
        assert state.device.alerts, "fault fixture should produce ~HQES alerts entry"
        assert "bitmask=" in state.device.alerts[0]

    def test_paused_flag_true_via_companion_pause(
        self, monkeypatch, fixture_name
    ):
        _patch_replay(monkeypatch, fixture_name)
        state = _initial_state()
        DeviceSpecialist().act(state)
        # Faults still emit a companion pause via ~HS, regardless of how
        # alerts are reported. The pause-discrimination path uses ~HS, not
        # the SNMP alert table.
        assert state.device.paused is True

class TestFaultFixturesBooleanFlags:
    """The dedicated boolean fields on state.device should reflect the
    physical fault, derived from the live state bitmask. These tests
    exercise the bitmask-parsing code path in snmp_zt411_physical_flags
    independently of the alert-table cross-check.

    If these fail with the boolean field == False on a fault fixture, the
    bitmask field-index logic in tools.py is wrong (almost certainly
    reading the wrong comma-separated field).
    """

    def test_head_open_flag_set_on_head_open_fixture(self, monkeypatch):
        _patch_replay(monkeypatch, "zt411_fixture_head_open.json")
        state = _initial_state()
        DeviceSpecialist().act(state)
        assert state.device.head_open is True, (
            f"Bitmask parser failed to detect head_open. "
            f"Got head_open={state.device.head_open!r}; "
            f"live printer captured this fixture in HEAD_OPEN state."
        )
        assert state.device.media_out is False
        assert state.device.ribbon_out is False

    def test_media_out_flag_set_on_media_out_fixture(self, monkeypatch):
        _patch_replay(monkeypatch, "zt411_fixture_media_out.json")
        state = _initial_state()
        DeviceSpecialist().act(state)
        assert state.device.media_out is True, (
            f"Bitmask parser failed to detect media_out. "
            f"Got media_out={state.device.media_out!r}."
        )
        assert state.device.head_open is False
        assert state.device.ribbon_out is False

    def test_ribbon_out_flag_set_on_ribbon_out_fixture(self, monkeypatch):
        _patch_replay(monkeypatch, "zt411_fixture_ribbon_out.json")
        state = _initial_state()
        DeviceSpecialist().act(state)
        assert state.device.ribbon_out is True, (
            f"Bitmask parser failed to detect ribbon_out. "
            f"Got ribbon_out={state.device.ribbon_out!r}."
        )
        assert state.device.head_open is False
        assert state.device.media_out is False
        
# ---------------------------------------------------------------------------
# Post-test idle — captured after recovering from faults; should look idle
# ---------------------------------------------------------------------------


class TestPostTestIdle:
    def test_returns_to_idle(self, monkeypatch):
        _patch_replay(monkeypatch, "zt411_fixture_post_test_idle.json")
        state = _initial_state()
        DeviceSpecialist().act(state)
        assert state.device.printer_status == "idle"
        assert state.device.alerts == []
        assert state.device.error_codes == []
        assert state.device.paused is False


# ---------------------------------------------------------------------------
# Loop-intent gating (Phase 4.2)
# ---------------------------------------------------------------------------


class TestLoopIntentGating:
    """LoopIntent.CALIBRATE should skip the SNMP consumables read, which
    is the one device-side tool with no ZPL replacement on this firmware
    and which is the slowest/most-likely-to-time-out call when UDP/161 is
    silent. GENERAL and the consumables-relevant intents must still call
    it so legacy callers see no behavior change.
    """

    def test_calibrate_intent_skips_consumables_read(self, monkeypatch):
        from zt411_agent.state import LoopIntent

        _patch_replay(monkeypatch, "zt411_fixture_idle_baseline.json")
        consumables_calls = {"n": 0}

        def spy_consumables(ip, community="public"):
            consumables_calls["n"] += 1
            from zt411_agent.agent.tools import ToolResult
            return ToolResult(success=True, output={"media": "ok", "ribbon": "ok"})

        monkeypatch.setattr(
            "zt411_agent.agent.tools.snmp_zt411_consumables", spy_consumables
        )
        monkeypatch.setattr(
            "zt411_agent.agent.device_specialist.snmp_zt411_consumables",
            spy_consumables,
        )

        state = _initial_state()
        state.loop_intent = LoopIntent.CALIBRATE
        DeviceSpecialist().act(state)

        assert consumables_calls["n"] == 0, (
            "CALIBRATE intent must skip snmp_zt411_consumables; "
            f"got {consumables_calls['n']} call(s)."
        )

    def test_general_intent_still_calls_consumables(self, monkeypatch):
        from zt411_agent.state import LoopIntent

        _patch_replay(monkeypatch, "zt411_fixture_idle_baseline.json")
        consumables_calls = {"n": 0}

        def spy_consumables(ip, community="public"):
            consumables_calls["n"] += 1
            from zt411_agent.agent.tools import ToolResult
            return ToolResult(success=True, output={"media": "ok", "ribbon": "ok"})

        monkeypatch.setattr(
            "zt411_agent.agent.tools.snmp_zt411_consumables", spy_consumables
        )
        monkeypatch.setattr(
            "zt411_agent.agent.device_specialist.snmp_zt411_consumables",
            spy_consumables,
        )

        state = _initial_state()
        # GENERAL is the default; assert by setting it explicitly.
        state.loop_intent = LoopIntent.GENERAL
        DeviceSpecialist().act(state)

        assert consumables_calls["n"] == 1
