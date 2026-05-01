"""
End-to-end agent loop test against the paused-printer fixture.

Goal: with the SNMP / IPP / network tools all served from canned fixture
data, Orchestrator.run() should pick device_specialist via the planner,
correctly diagnose the pause, log a Resume recommendation, and either
escalate (because resume requires human action) or hit max_loop_steps.

The planner is exercised for real — but because the dev environment may
not have an LLM available (no ANTHROPIC_API_KEY, no Ollama models pulled),
the planner is forced to tier 0 (offline rule-based fallback) so the
test runs deterministically without external dependencies. The agent
loop itself is unchanged: the offline planner still ranks specialists
by their can_handle() utility and returns a real PlannerResponse.
"""
from __future__ import annotations

import os
import socket
import time
from unittest.mock import MagicMock

import pytest

from zt411_agent.agent.cups_specialist import CUPSSpecialist
from zt411_agent.agent.device_specialist import DeviceSpecialist
from zt411_agent.agent.network_specialist import NetworkSpecialist
from zt411_agent.agent.orchestrator import Orchestrator
from zt411_agent.agent.tools import ToolResult
from zt411_agent.agent.validation_specialist import ValidationSpecialist
from zt411_agent.agent.windows_specialist import WindowsSpecialist
from zt411_agent.planner import RagSnippet
from zt411_agent.state import (
    ActionStatus,
    AgentState,
    LoopStatus,
    OSPlatform,
)
from tests.fixtures.replay import make_fixture_replay


PRINTER_IP = "192.168.99.10"
PAUSE_FIXTURE = "zt411_fixture_paused.json"
MAX_LOOP_STEPS = 4


# ---------------------------------------------------------------------------
# Canned offline responses for non-SNMP/IPP tools so the loop is truly hermetic
# ---------------------------------------------------------------------------


def _stub_ping(ip, timeout_s=2.0, count=1):
    return ToolResult(
        success=True,
        output={"reachable": True, "latency_ms": 1.0},
        raw=f"ping {ip} ok (stub)",
    )


def _stub_tcp_connect(ip, port, timeout_s=3.0):
    return ToolResult(success=True, output={"open": True})


def _stub_dns_lookup(hostname):
    return ToolResult(success=True, output={"ip": PRINTER_IP, "resolved": True})


def _stub_arp_lookup(ip):
    return ToolResult(
        success=True,
        output={"mac": "00:07:4D:AB:CD:EF", "found": True},
        raw="stub arp",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_tools(monkeypatch):
    """Monkeypatch every external tool the loop touches with offline replays."""
    replay = make_fixture_replay(PAUSE_FIXTURE)

    # SNMP / IPP — served from the captured fixture
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
    monkeypatch.setattr(
        "zt411_agent.agent.device_specialist.ipp_get_attributes",
        replay["ipp_get_attributes"],
    )
    # Phase 2.5: ZPL ~HS replaces SNMP for physical-flags reads. Patch
    # both namespaces — device_specialist imports the symbol directly.
    monkeypatch.setattr(
        "zt411_agent.agent.tools.zpl_zt411_host_status",
        replay["zpl_zt411_host_status"],
    )
    monkeypatch.setattr(
        "zt411_agent.agent.device_specialist.zpl_zt411_host_status",
        replay["zpl_zt411_host_status"],
    )
    # Phase 4.2: ~HI / ~HQES replace snmp_zt411_status / snmp_zt411_alerts.
    # Same dual-namespace pattern.
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

    # Network probes — stubbed so the loop never hits the real network.
    # The network specialist imports these names directly from tools, so
    # patching its module-local references is required in addition to the
    # tools-module-level patch (kept for any callers that look it up there).
    monkeypatch.setattr("zt411_agent.agent.tools.ping", _stub_ping)
    monkeypatch.setattr(
        "zt411_agent.agent.tools.tcp_connect", _stub_tcp_connect
    )
    monkeypatch.setattr(
        "zt411_agent.agent.tools.dns_lookup", _stub_dns_lookup
    )
    monkeypatch.setattr(
        "zt411_agent.agent.tools.arp_lookup", _stub_arp_lookup
    )
    monkeypatch.setattr("zt411_agent.agent.network_specialist.ping", _stub_ping)
    monkeypatch.setattr(
        "zt411_agent.agent.network_specialist.tcp_connect", _stub_tcp_connect
    )
    monkeypatch.setattr(
        "zt411_agent.agent.network_specialist.dns_lookup", _stub_dns_lookup
    )
    monkeypatch.setattr(
        "zt411_agent.agent.network_specialist.arp_lookup", _stub_arp_lookup
    )

    # Block any TCP probe inside the planner's tier-detection path so it
    # cannot reach the live internet during the test. With force_tier="tier0"
    # the probe is bypassed anyway, but this is a belt-and-braces guard.
    def _no_tcp(*_args, **_kwargs):
        return False

    monkeypatch.setattr("zt411_agent.planner._tcp_reachable", _no_tcp)

    return replay


@pytest.fixture
def offline_cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.runtime.tier = "tier0"
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


@pytest.fixture
def initial_state() -> AgentState:
    state = AgentState(
        os_platform=OSPlatform.LINUX,
        symptoms=["printer paused"],
    )
    state.device.ip = PRINTER_IP
    return state


@pytest.fixture
def pause_snippet() -> RagSnippet:
    return RagSnippet(
        snippet_id="ZT411_OG_pause_p45",
        source="ZT411 Operations Guide",
        section="Pause / Resume",
        text=(
            "Press the PAUSE button on the front panel to resume printing "
            "after a pause condition. Verify no other faults are present."
        ),
        score=0.92,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _build_orchestrator(cfg, max_steps=MAX_LOOP_STEPS) -> Orchestrator:
    return Orchestrator(
        specialists=[
            DeviceSpecialist(),
            NetworkSpecialist(),
            CUPSSpecialist(),
            WindowsSpecialist(),
            ValidationSpecialist(),
        ],
        cfg=cfg,
        max_loop_steps=max_steps,
    )


class TestAgentLoopPauseFixture:
    def test_loop_terminates_within_max_steps(
        self, patched_tools, offline_cfg, initial_state, pause_snippet
    ):
        orch = _build_orchestrator(offline_cfg)
        t0 = time.monotonic()
        result = orch.run(initial_state, [pause_snippet])
        elapsed = time.monotonic() - t0

        assert result.loop_status in {
            LoopStatus.SUCCESS,
            LoopStatus.ESCALATED,
            LoopStatus.MAX_STEPS,
        }
        # max_loop_steps=4: the orchestrator escalates once loop_counter > 4,
        # so the counter can hit 5 in the iteration where escalation fires.
        assert result.loop_counter <= MAX_LOOP_STEPS + 1
        # Soft wall-clock guard against runaway loops, not a perf assertion.
        # Bumped from 10.0 -> 15.0 in Phase 2.5: not a regression caused by
        # the SNMP->ZPL swap (parser is 2.4us per call; replay is sync), but
        # the previous threshold was already at the boundary on Windows.
        # Iteration-count assertion above is the real "no runaway" check.
        assert elapsed < 15.0, f"loop took {elapsed:.2f}s, expected <15s"

    def test_action_log_contains_resume_recommendation(
        self, patched_tools, offline_cfg, initial_state, pause_snippet
    ):
        orch = _build_orchestrator(offline_cfg)
        result = orch.run(initial_state, [pause_snippet])

        # The DeviceSpecialist logs the resume recommendation as PENDING.
        # Validator auto-approves LOW-risk actions, so the entry's status
        # may be CONFIRMED by the time the loop ends — either is fine,
        # the recommendation itself is what matters.
        resume = [
            a for a in result.action_log
            if a.action.startswith("advise: resume")
        ]
        assert resume, (
            "expected at least one 'advise: resume user-paused printer' "
            "entry in action_log"
        )
        # Must mention awaiting human action — this is the PENDING-from-the-
        # user's-perspective signal the spec calls out.
        assert any(
            "awaiting human action" in a.result.lower() for a in resume
        )
        # And the recommendation came in at LOW risk (not destructive / firmware).
        assert all(a.risk.value == "low" for a in resume)

    def test_evidence_contains_snmp_physical_flags_and_alerts(
        self, patched_tools, offline_cfg, initial_state, pause_snippet
    ):
        orch = _build_orchestrator(offline_cfg)
        result = orch.run(initial_state, [pause_snippet])

        sources = {ev.source for ev in result.evidence}
        assert "snmp_physical_flags" in sources
        assert "snmp_alerts" in sources

    def test_planner_received_evidence(
        self, patched_tools, offline_cfg, initial_state, pause_snippet
    ):
        """The planner runs every iteration and either cites snippets or
        returns an empty list. In offline tier 0 the planner doesn't cite,
        but it must still receive (and rank) the evidence the device
        specialist produced. We assert that by checking each iteration
        had a chance to see device-specialist evidence — i.e., evidence
        items were collected, the planner ran, and ranked specialists at
        least once chose device_specialist (visible via visited_specialists).
        """
        orch = _build_orchestrator(offline_cfg)
        result = orch.run(initial_state, [pause_snippet])

        assert result.evidence, "no evidence collected"
        assert "device_specialist" in result.visited_specialists

    def test_planner_citation_evidence_when_available(
        self, patched_tools, offline_cfg, initial_state, pause_snippet
    ):
        """When the planner runs at tier1 (Ollama) or tier2 (Claude),
        every loop iteration should add a ``planner_citations`` evidence
        item. This test exercises the assertion against the *resolved*
        planner tier — not whether some LLM happens to be reachable on
        the host — because the previous probe-based check fired even
        when the orchestrator's planner had already been forced to
        tier0 by configuration, leaving the assertion logically
        inconsistent with the fixture.

        ``offline_cfg`` pins ``cfg.runtime.tier = "tier0"``, so the
        resolved tier is always tier0 here; the citation assertion is
        therefore unreachable by design and the test skips with a clear
        reason. If a future fixture exercises tier1/tier2 against the
        replayed SNMP/IPP path, the assertion below will run and
        validate citation emission.
        """
        resolved_tier = getattr(offline_cfg.runtime, "tier", "auto")
        if resolved_tier == "tier0":
            pytest.skip(
                "tier0 planner does not emit citations by design; "
                "fixture forces tier0 to keep this test hermetic"
            )

        orch = _build_orchestrator(offline_cfg)
        result = orch.run(initial_state, [pause_snippet])

        citation_evidence = [
            ev for ev in result.evidence if ev.source == "planner_citations"
        ]
        assert citation_evidence, (
            f"planner ran at resolved tier={resolved_tier} but no "
            "planner_citations evidence was recorded — planner is not "
            "citing snippets"
        )

    def test_state_device_reflects_paused_diagnosis(
        self, patched_tools, offline_cfg, initial_state, pause_snippet
    ):
        orch = _build_orchestrator(offline_cfg)
        result = orch.run(initial_state, [pause_snippet])

        assert result.device.printer_status == "paused"
        assert result.device.paused is True
        assert "alert:1.11" in result.device.error_codes

    def test_loop_terminates_on_repeated_human_action_recommendation(
        self, patched_tools, offline_cfg, initial_state, pause_snippet
    ):
        """Regression: when a worker specialist has logged a SAFE/LOW-risk
        ``Awaiting human action`` recommendation and a full loop iteration
        has elapsed without the physical condition clearing, the
        orchestrator must escalate with reason ``awaiting_human_action``
        before ``max_loop_steps`` fires.

        Pre-fix behavior (Session B, log
        ``tests/logs/session_b_20260429-221403.log``): the offline planner
        cycled DeviceSpecialist → WindowsSpecialist → NetworkSpecialist →
        DeviceSpecialist while the printer remained paused, eventually
        escalating with the misleading reason ``"max_loop_steps exceeded"``.
        Post-fix behavior: ValidationSpecialist short-circuits on the
        second iteration when it observes the same prior recommendation
        and the device is still paused.
        """
        orch = _build_orchestrator(offline_cfg)
        result = orch.run(initial_state, [pause_snippet])

        assert result.loop_status == LoopStatus.ESCALATED, (
            f"expected ESCALATED, got {result.loop_status}"
        )
        assert result.escalation_reason == "awaiting_human_action", (
            f"expected reason 'awaiting_human_action', "
            f"got {result.escalation_reason!r}"
        )
        # Proves we terminated *before* hitting the cap, not at it.
        # MAX_LOOP_STEPS=4; the short-circuit fires at the start of
        # validation in step 2 once the prior recommendation has been
        # auto-approved in step 1.
        assert result.loop_counter <= 3, (
            f"expected loop_counter <= 3, got {result.loop_counter}"
        )

        short_circuit_evidence = [
            ev for ev in result.evidence
            if ev.source == "validation_short_circuit"
        ]
        assert short_circuit_evidence, (
            "expected at least one evidence item with source "
            "'validation_short_circuit' citing the recommendation that "
            "triggered the short-circuit"
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def any_llm_available() -> bool:
    """Best-effort probe for whether a real LLM is reachable.

    Returns True only if (a) ANTHROPIC_API_KEY is set, or (b) the local
    Ollama daemon is responding AND has at least one model pulled.
    Conservative — defaults to False when uncertain.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    try:
        import httpx  # type: ignore[import]

        resp = httpx.get("http://localhost:11434/api/tags", timeout=1.5)
        if resp.status_code != 200:
            return False
        models = resp.json().get("models", [])
        return bool(models)
    except Exception:  # noqa: BLE001
        return False
