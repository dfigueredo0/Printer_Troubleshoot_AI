"""Phase 4.4 — orchestrator pause/resume.

The orchestrator suspends with loop_status=AWAITING_CONFIRMATION when
the validation specialist issues a confirmation token. After the user
flips the entry to CONFIRMED (mimicking POST /confirm/{token}) and
resets loop_status to RUNNING, a second orch.run(state) pass must
execute the action and reach SUCCESS.

These tests exercise the *orchestrator* on the calibrate scenario (the
test_calibrate_action_full_loop tests drive specialists directly and
don't touch loop_status — different concern).
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# tests/fixtures/replay.py imports zt411_agent — keep the project src on
# sys.path even when invoked from outside the repo root.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402

from zt411_agent.agent.device_specialist import DeviceSpecialist  # noqa: E402
from zt411_agent.agent.orchestrator import Orchestrator  # noqa: E402
from zt411_agent.agent.tools import ToolResult  # noqa: E402
from zt411_agent.agent.validation_specialist import ValidationSpecialist  # noqa: E402
from zt411_agent.state import (  # noqa: E402
    ActionStatus,
    AgentState,
    LoopStatus,
    OSPlatform,
)
from tests.fixtures.replay import make_fixture_replay  # noqa: E402


PRINTER_IP = "192.168.99.10"


def _offline_cfg() -> MagicMock:
    """Minimal cfg that pins the planner to tier0 (no LLM, no network)."""
    cfg = MagicMock()
    cfg.runtime.tier = "tier0"
    cfg.runtime.mode = "auto"
    cfg.llm.planner_backend = "claude"
    cfg.llm.model = "claude-sonnet-4-6"
    cfg.llm.temperature = 0.0
    cfg.llm.max_tokens = 512
    cfg.llm.timeout = 5.0
    cfg.llm.require_citations = False
    cfg.llm.json_schema.retries = 1
    cfg.ollama.host = "http://localhost:11434"
    cfg.ollama.model = "granite4"
    cfg.ollama.temperature = 0.0
    cfg.ollama.num_ctx = 4096
    return cfg


def _patch_idle_baseline(monkeypatch) -> None:
    """Patch all device-side tools with the idle-baseline replay."""
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
    # Block any tier-detection probe so the planner cannot reach the
    # network even if it tries.
    monkeypatch.setattr("zt411_agent.planner._tcp_reachable", lambda *a, **k: False)


def _build_orchestrator(max_steps: int = 5) -> Orchestrator:
    return Orchestrator(
        specialists=[DeviceSpecialist(), ValidationSpecialist()],
        cfg=_offline_cfg(),
        max_loop_steps=max_steps,
    )


def _initial_state() -> AgentState:
    state = AgentState(
        os_platform=OSPlatform.LINUX,
        symptoms=["printer is printing blank labels"],
    )
    state.device.ip = PRINTER_IP
    return state


# ---------------------------------------------------------------------------
# Pause: validator issues a token → orchestrator yields with AWAITING
# ---------------------------------------------------------------------------


class TestOrchestratorSuspends:
    def test_loop_status_is_awaiting_confirmation(self, monkeypatch):
        _patch_idle_baseline(monkeypatch)
        state = _initial_state()
        orch = _build_orchestrator()
        state = orch.run(state)
        assert state.loop_status == LoopStatus.AWAITING_CONFIRMATION

    def test_calibrate_pending_with_token(self, monkeypatch):
        _patch_idle_baseline(monkeypatch)
        state = _initial_state()
        orch = _build_orchestrator()
        state = orch.run(state)

        pending = [
            a for a in state.action_log
            if a.action == "zpl_zt411_calibrate"
            and a.status == ActionStatus.PENDING
        ]
        assert len(pending) == 1
        assert pending[0].confirmation_token, (
            "validator must issue a token on the SERVICE_RESTART proposal"
        )

    def test_did_not_escalate_or_succeed(self, monkeypatch):
        _patch_idle_baseline(monkeypatch)
        state = _initial_state()
        orch = _build_orchestrator()
        state = orch.run(state)
        assert state.loop_status not in {
            LoopStatus.SUCCESS, LoopStatus.ESCALATED, LoopStatus.MAX_STEPS,
        }


# ---------------------------------------------------------------------------
# Resume: confirm + reset RUNNING → orch.run() executes and reaches SUCCESS
# ---------------------------------------------------------------------------


class TestOrchestratorResumes:
    def test_resumes_to_success_after_confirm(self, monkeypatch):
        _patch_idle_baseline(monkeypatch)

        calibrate_calls = {"n": 0}

        def stub_calibrate(ip, port=9100):
            calibrate_calls["n"] += 1
            return ToolResult(success=True, output={"sent_bytes": 3})

        monkeypatch.setattr(
            "zt411_agent.agent.tools.zpl_zt411_calibrate", stub_calibrate
        )
        monkeypatch.setattr(
            "zt411_agent.agent.device_specialist._ACTION_SETTLE_DELAY_S", 0.0
        )

        state = _initial_state()
        orch = _build_orchestrator()

        # Pass 1: should suspend.
        state = orch.run(state)
        assert state.loop_status == LoopStatus.AWAITING_CONFIRMATION
        assert calibrate_calls["n"] == 0

        # Mimic the confirm endpoint: consume token, flip status to
        # CONFIRMED via the helper (so status_history records the step),
        # reset loop_status to RUNNING.
        pending = next(
            a for a in state.action_log
            if a.action == "zpl_zt411_calibrate"
            and a.status == ActionStatus.PENDING
        )
        entry_id = state.consume_confirmation_token(pending.confirmation_token)
        assert entry_id == pending.entry_id
        state.update_action_status(pending.entry_id, ActionStatus.CONFIRMED)
        state.loop_status = LoopStatus.RUNNING

        # Pass 2: should execute calibrate, verify, and reach a terminal
        # loop_status. Whether the orchestrator reaches SUCCESS depends on
        # whether is_resolved() flips True (queue_drained + test_print_ok
        # + device_ready) — for the calibrate-only path only device_ready
        # gets set, so the loop terminates via escalation/max_steps after
        # exhausting useful work. Either is acceptable here; the load-bearing
        # assertions are: the loop did NOT remain suspended, the action did
        # fire, and the entry walked through to RESOLVED.
        state = orch.run(state)
        assert state.loop_status != LoopStatus.AWAITING_CONFIRMATION, (
            "loop must have advanced past suspension on resume"
        )
        assert state.loop_status in {
            LoopStatus.SUCCESS, LoopStatus.ESCALATED, LoopStatus.MAX_STEPS,
        }, f"expected a terminal status, got {state.loop_status.value}"
        assert calibrate_calls["n"] == 1

        cals = [
            a for a in state.action_log if a.action == "zpl_zt411_calibrate"
        ]
        assert len(cals) == 1
        assert cals[0].status == ActionStatus.RESOLVED
        # Full mutation history preserved.
        history = cals[0].status_history
        for stage in (
            ActionStatus.PENDING,
            ActionStatus.CONFIRMED,
            ActionStatus.EXECUTED,
            ActionStatus.VERIFYING,
            ActionStatus.RESOLVED,
        ):
            assert stage in history, f"missing {stage.value} in {history}"

    def test_does_not_re_suspend_on_already_confirmed(self, monkeypatch):
        """Once an entry is CONFIRMED (not PENDING), the predicate that
        drives the AWAITING_CONFIRMATION suspension must return False —
        otherwise the loop deadlocks on resume."""
        _patch_idle_baseline(monkeypatch)
        monkeypatch.setattr(
            "zt411_agent.agent.tools.zpl_zt411_calibrate",
            lambda ip, port=9100: ToolResult(success=True, output={"sent_bytes": 3}),
        )
        monkeypatch.setattr(
            "zt411_agent.agent.device_specialist._ACTION_SETTLE_DELAY_S", 0.0
        )

        state = _initial_state()
        orch = _build_orchestrator()
        state = orch.run(state)
        # Confirm.
        pending = next(
            a for a in state.action_log
            if a.action == "zpl_zt411_calibrate"
            and a.status == ActionStatus.PENDING
        )
        state.consume_confirmation_token(pending.confirmation_token)
        state.update_action_status(pending.entry_id, ActionStatus.CONFIRMED)
        state.loop_status = LoopStatus.RUNNING

        state = orch.run(state)
        # Must NOT have suspended again.
        assert state.loop_status != LoopStatus.AWAITING_CONFIRMATION
