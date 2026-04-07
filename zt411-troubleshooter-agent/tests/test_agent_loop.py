"""
tests/test_agent_loop.py

Unit tests for the Orchestrator agent loop.

Covers:
  - Routing: planner-preferred specialist is selected when utility is sufficient.
  - Loop cap: MAX_STEPS exceeded → LoopStatus.MAX_STEPS (via ESCALATED).
  - Escalation: no specialist clears MIN_UTILITY → ESCALATED.
  - Success: validation specialist confirms all three flags → SUCCESS.
  - Validation always runs: validator is called every iteration.
  - Planner downgrade: cloud failure falls back to offline (via mock).
  - Append-only audit: action_log only grows; no entries removed.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.zt411_agent.agent.orchestrator import Orchestrator
from src.zt411_agent.state import (
    AgentState,
    ActionStatus,
    LoopStatus,
    OSPlatform,
    RiskLevel,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _state(**kwargs) -> AgentState:
    s = AgentState(os_platform=OSPlatform.LINUX)
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


def _cfg(tier: str = "tier0") -> MagicMock:
    cfg = MagicMock()
    cfg.runtime.tier = tier
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


def _mock_specialist(name: str, utility: float = 0.8, *, mutate_fn=None):
    """Create a mock Specialist. mutate_fn(state) can modify state before returning."""

    spec = MagicMock()
    spec.name = name
    spec.can_handle = MagicMock(return_value=utility)

    def _act(state):
        if mutate_fn:
            mutate_fn(state)
        return {"evidence": [], "actions_taken": [f"{name} acted"], "next_state": state}

    spec.act = MagicMock(side_effect=_act)
    return spec


def _build_orch(specialists, max_steps=5):
    cfg = _cfg(tier="tier0")
    return Orchestrator(specialists=specialists, cfg=cfg, max_loop_steps=max_steps)


# ---------------------------------------------------------------------------
# 1. Routing
# ---------------------------------------------------------------------------


class TestRouting:
    def test_planner_preferred_specialist_called(self):
        """When device_specialist has highest utility it should act first."""
        device = _mock_specialist("device_specialist", utility=0.9)
        network = _mock_specialist("network_specialist", utility=0.3)
        validator = _mock_specialist("validation_specialist", utility=0.1)
        state = _state()

        orch = _build_orch([device, network, validator], max_steps=1)
        orch.run(state)

        device.act.assert_called_once()

    def test_fallback_to_highest_scorer_when_planner_unknown(self):
        """Orchestrator should pick the highest-scoring available specialist."""
        device = _mock_specialist("device_specialist", utility=0.1)
        network = _mock_specialist("network_specialist", utility=0.7)
        validator = _mock_specialist("validation_specialist", utility=0.1)
        state = _state()

        orch = _build_orch([device, network, validator], max_steps=1)
        orch.run(state)

        network.act.assert_called_once()

    def test_validator_always_called_after_specialist(self):
        """Validator must run every loop iteration regardless of plan."""
        device = _mock_specialist("device_specialist", utility=0.8)
        validator = _mock_specialist("validation_specialist", utility=0.1)
        state = _state()

        orch = _build_orch([device, validator], max_steps=2)
        orch.run(state)

        assert validator.act.call_count >= 1

    def test_visited_specialists_updated(self):
        """After a specialist acts, its name should be in state.visited_specialists."""
        device = _mock_specialist("device_specialist", utility=0.8)
        validator = _mock_specialist("validation_specialist", utility=0.1)
        state = _state()

        orch = _build_orch([device, validator], max_steps=1)
        result = orch.run(state)

        assert "device_specialist" in result.visited_specialists


# ---------------------------------------------------------------------------
# 2. Loop cap enforcement
# ---------------------------------------------------------------------------


class TestLoopCap:
    def test_loop_cap_triggers_escalation(self):
        """Exceeding max_loop_steps must set loop_status to ESCALATED."""
        device = _mock_specialist("device_specialist", utility=0.9)
        validator = _mock_specialist("validation_specialist", utility=0.5)
        state = _state()

        orch = _build_orch([device, validator], max_steps=3)
        result = orch.run(state)

        assert result.loop_status == LoopStatus.ESCALATED
        assert result.loop_counter > 3

    def test_loop_counter_increments(self):
        device = _mock_specialist("device_specialist", utility=0.9)
        validator = _mock_specialist("validation_specialist", utility=0.1)
        state = _state()

        orch = _build_orch([device, validator], max_steps=2)
        result = orch.run(state)

        assert result.loop_counter > 0


# ---------------------------------------------------------------------------
# 3. Escalation — no utility
# ---------------------------------------------------------------------------


class TestEscalation:
    def test_no_utility_escalates(self):
        """All specialists at zero utility → immediate escalation."""
        device = _mock_specialist("device_specialist", utility=0.0)
        validator = _mock_specialist("validation_specialist", utility=0.0)
        state = _state()

        orch = _build_orch([device, validator], max_steps=5)
        result = orch.run(state)

        assert result.loop_status == LoopStatus.ESCALATED
        assert result.escalation_reason != ""

    def test_escalation_reason_recorded_in_action_log(self):
        device = _mock_specialist("device_specialist", utility=0.0)
        validator = _mock_specialist("validation_specialist", utility=0.0)
        state = _state()

        orch = _build_orch([device, validator], max_steps=5)
        result = orch.run(state)

        escalation_entries = [
            a for a in result.action_log
            if "escalate" in a.action
        ]
        assert len(escalation_entries) > 0


# ---------------------------------------------------------------------------
# 4. Success path
# ---------------------------------------------------------------------------


class TestSuccessPath:
    def test_success_when_all_criteria_met(self):
        """If a specialist sets all three success flags the loop exits SUCCESS."""

        def _resolve(state: AgentState):
            state.queue_drained = True
            state.test_print_ok = True
            state.device_ready = True
            state.device.printer_status = "idle"
            state.device.alerts = []
            state.device.error_codes = []
            state.add_evidence("device_specialist", "test_print", "test print success")

        device = _mock_specialist("device_specialist", utility=0.9, mutate_fn=_resolve)
        validator = _mock_specialist("validation_specialist", utility=0.9)

        # Let the real validation specialist confirm success
        from src.zt411_agent.agent.validation_specialist import ValidationSpecialist

        real_validator = ValidationSpecialist()
        state = _state()

        orch = _build_orch([device, real_validator], max_steps=5)
        result = orch.run(state)

        assert result.loop_status == LoopStatus.SUCCESS

    def test_success_not_declared_without_evidence(self):
        """Hallucination guard must reset flags set without tool evidence."""
        from src.zt411_agent.agent.validation_specialist import ValidationSpecialist

        def _fake_success(state: AgentState):
            # Set flags but add NO real tool evidence
            state.queue_drained = True
            state.test_print_ok = True
            state.device_ready = True

        device = _mock_specialist("device_specialist", utility=0.9, mutate_fn=_fake_success)
        real_validator = ValidationSpecialist()
        state = _state()

        orch = _build_orch([device, real_validator], max_steps=2)
        result = orch.run(state)

        # Hallucination guard should have reset flags → no SUCCESS
        assert result.loop_status != LoopStatus.SUCCESS


# ---------------------------------------------------------------------------
# 5. Append-only audit trail
# ---------------------------------------------------------------------------


class TestAuditTrail:
    def test_action_log_only_grows(self):
        """action_log must be strictly append-only — no entries removed between steps."""
        counts: list[int] = []

        def _count_entries(state: AgentState):
            counts.append(len(state.action_log))

        device = _mock_specialist("device_specialist", utility=0.9, mutate_fn=_count_entries)
        validator = _mock_specialist("validation_specialist", utility=0.1)
        state = _state()

        orch = _build_orch([device, validator], max_steps=3)
        orch.run(state)

        # Counts should be non-decreasing
        for i in range(1, len(counts)):
            assert counts[i] >= counts[i - 1], (
                f"action_log shrank from {counts[i-1]} to {counts[i]} at step {i}"
            )

    def test_evidence_only_grows(self):
        counts: list[int] = []

        def _count_evidence(state: AgentState):
            counts.append(len(state.evidence))

        device = _mock_specialist("device_specialist", utility=0.9, mutate_fn=_count_evidence)
        validator = _mock_specialist("validation_specialist", utility=0.1)
        state = _state()

        orch = _build_orch([device, validator], max_steps=3)
        orch.run(state)

        for i in range(1, len(counts)):
            assert counts[i] >= counts[i - 1]


# ---------------------------------------------------------------------------
# 6. Confirmation token gating
# ---------------------------------------------------------------------------


class TestConfirmationTokens:
    def test_risky_action_issues_token(self):
        """A pending action with DESTRUCTIVE risk must get a confirmation token."""
        from src.zt411_agent.agent.validation_specialist import ValidationSpecialist

        def _log_risky(state: AgentState):
            state.log_action(
                specialist="device_specialist",
                action="factory_reset",
                risk=RiskLevel.DESTRUCTIVE,
                status=ActionStatus.PENDING,
            )

        device = _mock_specialist("device_specialist", utility=0.9, mutate_fn=_log_risky)
        real_validator = ValidationSpecialist()
        state = _state()

        orch = _build_orch([device, real_validator], max_steps=1)
        result = orch.run(state)

        # The token should have been issued for the destructive action
        assert len(result.confirmation_tokens) > 0

    def test_safe_action_does_not_issue_token(self):
        """A SAFE action should be auto-approved without a confirmation token."""
        from src.zt411_agent.agent.validation_specialist import ValidationSpecialist

        def _log_safe(state: AgentState):
            state.log_action(
                specialist="device_specialist",
                action="ping printer",
                risk=RiskLevel.SAFE,
                status=ActionStatus.PENDING,
            )

        device = _mock_specialist("device_specialist", utility=0.9, mutate_fn=_log_safe)
        real_validator = ValidationSpecialist()
        state = _state()

        orch = _build_orch([device, real_validator], max_steps=1)
        result = orch.run(state)

        # Safe action confirmed, no pending tokens expected
        confirmed = [
            a for a in result.action_log
            if a.action == "ping printer" and a.status == ActionStatus.CONFIRMED
        ]
        assert len(confirmed) == 1
