"""
Synthetic eval cases for the ZT411 troubleshooter agent.

Each case pairs one captured fixture with a plausible operator-reported
symptom and expected outcomes. The eval runner replays the fixture
through the full Orchestrator → Specialist → ValidationSpecialist loop
and scores the run against the four expected fields.

Calibration notes
-----------------
* DeviceSpecialist's *recommendation phrasing* lives in two places per
  iteration: a high-level ``action_log`` entry whose ``action`` field
  summarises the SNMP/IPP/KB calls just made, plus an ``evidence`` item
  with ``source="physical_recommendations"`` that holds the human-
  readable advice ("Close printhead and latch firmly.", etc.). The eval
  runner therefore looks for keywords across **both** action_log and
  evidence content — single-source matching would fail every fault
  case because the recommendation text is never copied into action.action.
* Only the user-paused branch logs an action_log entry whose action
  starts with "advise:" and whose risk is LOW. Faults skip the
  log_action call (only the recommendation evidence is emitted), so the
  fault eval cases must NOT require risk=LOW; they assert
  ``expected_risk_level="safe"`` (matching the DeviceSpecialist
  high-level entry's risk).
* For idle / negative cases the agent never logs a human-action
  recommendation at all. The orchestrator then falls through to either
  ESCALATED (no specialist with sufficient utility) or SUCCESS once
  validation flags flip — neither is wrong. We assert ``loop_status in
  {"escalated", "success", "max_steps", "running"}`` rather than pin a
  single value, with no specific escalation_reason.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EvalCase:
    case_id: str
    symptom: str
    fixture_path: str
    expected_diagnosis: str           # e.g. "paused" / "fault:media_out" / "idle"
    expected_recommendation_keywords: list[str] = field(default_factory=list)
    expected_risk_level: str = "safe"  # "safe" / "low" / "config_change" / etc.
    expected_loop_status: str = "escalated"
    expected_escalation_reason: str | None = None
    # When True, the case is in the "no actionable recommendation"
    # category — keyword check is skipped and the loop status is
    # accepted as any of {success, escalated, max_steps}.
    no_action_expected: bool = False


# ---------------------------------------------------------------------------
# 1. Pause user-initiated (paused fixture)
# ---------------------------------------------------------------------------

_PAUSED_CASES: list[EvalCase] = [
    EvalCase(
        case_id="paused_user_001",
        symptom="printer paused",
        fixture_path="zt411_fixture_paused.json",
        expected_diagnosis="paused",
        expected_recommendation_keywords=["resume", "pause"],
        expected_risk_level="low",
        expected_loop_status="escalated",
        expected_escalation_reason="awaiting_human_action",
    ),
    EvalCase(
        case_id="paused_user_002",
        symptom="labels stopped printing, front panel shows pause",
        fixture_path="zt411_fixture_paused.json",
        expected_diagnosis="paused",
        expected_recommendation_keywords=["resume", "press"],
        expected_risk_level="low",
        expected_loop_status="escalated",
        expected_escalation_reason="awaiting_human_action",
    ),
    EvalCase(
        case_id="paused_user_003",
        symptom="ZT411 pause LED is on, jobs queued",
        fixture_path="zt411_fixture_paused.json",
        expected_diagnosis="paused",
        expected_recommendation_keywords=["resume"],
        expected_risk_level="low",
        expected_loop_status="escalated",
        expected_escalation_reason="awaiting_human_action",
    ),
    EvalCase(
        case_id="paused_user_004",
        symptom="printer not feeding labels, front panel says paused",
        fixture_path="zt411_fixture_paused.json",
        expected_diagnosis="paused",
        expected_recommendation_keywords=["pause", "front panel"],
        expected_risk_level="low",
        expected_loop_status="escalated",
        expected_escalation_reason="awaiting_human_action",
    ),
]


# ---------------------------------------------------------------------------
# 2. Head open (head_open fixture)
# ---------------------------------------------------------------------------

_HEAD_OPEN_CASES: list[EvalCase] = [
    EvalCase(
        case_id="head_open_001",
        symptom="printer reports head open error",
        fixture_path="zt411_fixture_head_open.json",
        expected_diagnosis="fault:head_open",
        expected_recommendation_keywords=["close", "head", "latch"],
        expected_risk_level="safe",
        expected_loop_status="escalated",
        expected_escalation_reason=None,
    ),
    EvalCase(
        case_id="head_open_002",
        symptom="zebra printhead alarm, paused",
        fixture_path="zt411_fixture_head_open.json",
        expected_diagnosis="fault:head_open",
        expected_recommendation_keywords=["close", "head"],
        expected_risk_level="safe",
        expected_loop_status="escalated",
        expected_escalation_reason=None,
    ),
    EvalCase(
        case_id="head_open_no_resume_recommendation",
        symptom="head open fault on ZT411",
        fixture_path="zt411_fixture_head_open.json",
        expected_diagnosis="fault:head_open",
        expected_recommendation_keywords=["close", "latch"],
        expected_risk_level="safe",
        expected_loop_status="escalated",
        expected_escalation_reason=None,
    ),
]


# ---------------------------------------------------------------------------
# 3. Media out (media_out fixture)
# ---------------------------------------------------------------------------

_MEDIA_OUT_CASES: list[EvalCase] = [
    EvalCase(
        case_id="media_out_001",
        symptom="printer says media out, no labels in tray",
        fixture_path="zt411_fixture_media_out.json",
        expected_diagnosis="fault:media_out",
        expected_recommendation_keywords=["load", "media", "calibrat"],
        expected_risk_level="safe",
        expected_loop_status="escalated",
        expected_escalation_reason=None,
    ),
    EvalCase(
        case_id="media_out_002",
        symptom="ZT411 paused with media-out alert",
        fixture_path="zt411_fixture_media_out.json",
        expected_diagnosis="fault:media_out",
        expected_recommendation_keywords=["load", "media"],
        expected_risk_level="safe",
        expected_loop_status="escalated",
        expected_escalation_reason=None,
    ),
    EvalCase(
        case_id="media_out_003",
        symptom="no labels feeding, front panel media out",
        fixture_path="zt411_fixture_media_out.json",
        expected_diagnosis="fault:media_out",
        expected_recommendation_keywords=["load", "calibrat"],
        expected_risk_level="safe",
        expected_loop_status="escalated",
        expected_escalation_reason=None,
    ),
    EvalCase(
        case_id="media_out_004",
        symptom="out of media on Zebra",
        fixture_path="zt411_fixture_media_out.json",
        expected_diagnosis="fault:media_out",
        expected_recommendation_keywords=["media"],
        expected_risk_level="safe",
        expected_loop_status="escalated",
        expected_escalation_reason=None,
    ),
]


# ---------------------------------------------------------------------------
# 4. Ribbon out (ribbon_out fixture)
# ---------------------------------------------------------------------------

_RIBBON_OUT_CASES: list[EvalCase] = [
    EvalCase(
        case_id="ribbon_out_001",
        symptom="ribbon out alert on ZT411",
        fixture_path="zt411_fixture_ribbon_out.json",
        expected_diagnosis="fault:ribbon_out",
        expected_recommendation_keywords=["ribbon"],
        expected_risk_level="safe",
        expected_loop_status="escalated",
        expected_escalation_reason=None,
    ),
    EvalCase(
        case_id="ribbon_out_002",
        symptom="thermal transfer printer, ribbon empty",
        fixture_path="zt411_fixture_ribbon_out.json",
        expected_diagnosis="fault:ribbon_out",
        expected_recommendation_keywords=["ribbon", "install"],
        expected_risk_level="safe",
        expected_loop_status="escalated",
        expected_escalation_reason=None,
    ),
    EvalCase(
        case_id="ribbon_out_003",
        symptom="printer paused with ribbon-out fault",
        fixture_path="zt411_fixture_ribbon_out.json",
        expected_diagnosis="fault:ribbon_out",
        expected_recommendation_keywords=["ribbon"],
        expected_risk_level="safe",
        expected_loop_status="escalated",
        expected_escalation_reason=None,
    ),
]


# ---------------------------------------------------------------------------
# 5. Idle / no action expected
# ---------------------------------------------------------------------------

_IDLE_CASES: list[EvalCase] = [
    EvalCase(
        case_id="idle_baseline_001",
        symptom="printer health check",
        fixture_path="zt411_fixture_idle_baseline.json",
        expected_diagnosis="idle",
        expected_recommendation_keywords=[],
        expected_risk_level="safe",
        expected_loop_status="escalated",   # falls through; no human-action
        expected_escalation_reason=None,
        no_action_expected=True,
    ),
    EvalCase(
        case_id="idle_baseline_002",
        symptom="check ZT411 status",
        fixture_path="zt411_fixture_idle_baseline.json",
        expected_diagnosis="idle",
        expected_recommendation_keywords=[],
        expected_risk_level="safe",
        expected_loop_status="escalated",
        expected_escalation_reason=None,
        no_action_expected=True,
    ),
    EvalCase(
        case_id="post_test_idle_001",
        symptom="confirm printer ready after test print",
        fixture_path="zt411_fixture_post_test_idle.json",
        expected_diagnosis="idle",
        expected_recommendation_keywords=[],
        expected_risk_level="safe",
        expected_loop_status="escalated",
        expected_escalation_reason=None,
        no_action_expected=True,
    ),
    EvalCase(
        case_id="post_test_idle_002",
        symptom="post-test verification",
        fixture_path="zt411_fixture_post_test_idle.json",
        expected_diagnosis="idle",
        expected_recommendation_keywords=[],
        expected_risk_level="safe",
        expected_loop_status="escalated",
        expected_escalation_reason=None,
        no_action_expected=True,
    ),
    EvalCase(
        case_id="idle_baseline_003",
        symptom="diagnostic baseline",
        fixture_path="zt411_fixture_idle_baseline.json",
        expected_diagnosis="idle",
        expected_recommendation_keywords=[],
        expected_risk_level="safe",
        expected_loop_status="escalated",
        expected_escalation_reason=None,
        no_action_expected=True,
    ),
    EvalCase(
        case_id="idle_baseline_004",
        symptom="printer running fine, just verifying",
        fixture_path="zt411_fixture_idle_baseline.json",
        expected_diagnosis="idle",
        expected_recommendation_keywords=[],
        expected_risk_level="safe",
        expected_loop_status="escalated",
        expected_escalation_reason=None,
        no_action_expected=True,
    ),
]


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


def load_cases() -> list[EvalCase]:
    """Return the full eval set."""
    return [
        *_PAUSED_CASES,
        *_HEAD_OPEN_CASES,
        *_MEDIA_OUT_CASES,
        *_RIBBON_OUT_CASES,
        *_IDLE_CASES,
    ]


if __name__ == "__main__":  # pragma: no cover
    import json

    cases = load_cases()
    print(f"Total cases: {len(cases)}")
    print(json.dumps([c.__dict__ for c in cases[:3]], indent=2))
