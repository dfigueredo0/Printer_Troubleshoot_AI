"""
Eval runner — replays the captured fixtures through the full agent loop
and scores each outcome against ``synth_cases``.

Reproducible & free
-------------------
* The planner is forced to tier0 via a stub config (see ``_make_cfg``)
  so no LLM API calls are made — eval is hermetic and costs nothing.
* The retriever is stubbed to return ``[]`` so eval doesn't depend on
  whether the production RAG index exists.
* SNMP / IPP / network probes are replayed from the fixture files via
  ``tests/fixtures/replay.py``.

Scoring
-------
Per case, four binary criteria:

1. ``diagnosis_correct``           — printer_status (or fault flag)
                                      matches expected_diagnosis.
2. ``recommendation_keywords``     — every expected keyword appears in
                                      either an action_log entry's
                                      ``action`` field OR an evidence
                                      content string.
3. ``risk_level_correct``          — at least one action_log entry has
                                      the expected risk; for "safe"
                                      cases that's the DeviceSpecialist
                                      high-level entry.
4. ``loop_terminated_correctly``   — loop_status matches and (if set)
                                      escalation_reason matches.

Results are written to ``eval/results/eval_<timestamp>.csv`` and a
console summary is printed. Exit 0 when the overall pass rate is at
least 70%, 1 otherwise.

CLI
---
::

    python -m eval.run_eval
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Pythonpath setup so this module runs both as ``python -m eval.run_eval``
# from the repo root AND as a script from inside the package dir.
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_REPO_DIR = _HERE.parent
if str(_REPO_DIR) not in sys.path:
    sys.path.insert(0, str(_REPO_DIR))

from eval.synth_cases import EvalCase, load_cases  # noqa: E402

from zt411_agent.agent.cups_specialist import CUPSSpecialist  # noqa: E402
from zt411_agent.agent.device_specialist import DeviceSpecialist  # noqa: E402
from zt411_agent.agent.network_specialist import NetworkSpecialist  # noqa: E402
from zt411_agent.agent.orchestrator import Orchestrator  # noqa: E402
from zt411_agent.agent.tools import ToolResult  # noqa: E402
from zt411_agent.agent.validation_specialist import ValidationSpecialist  # noqa: E402
from zt411_agent.agent.windows_specialist import WindowsSpecialist  # noqa: E402
from zt411_agent.rag.retriever import Retriever  # noqa: E402
from zt411_agent.state import AgentState, OSPlatform  # noqa: E402

# Fixture replay helpers live under tests/fixtures/. Import via the tests
# package — the repo's pytest.ini puts tests/ on the path; for the eval
# CLI we add it explicitly here.
_TESTS_DIR = _REPO_DIR / "tests"
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from fixtures.replay import make_fixture_replay  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Score record
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    case_id: str
    fixture: str
    expected_diagnosis: str
    actual_diagnosis: str
    diagnosis_correct: bool

    expected_keywords: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    recommendation_keywords_correct: bool = False

    expected_risk: str = ""
    matched_risk: bool = False
    risk_level_correct: bool = False

    expected_loop_status: str = ""
    actual_loop_status: str = ""
    expected_escalation_reason: str | None = None
    actual_escalation_reason: str = ""
    loop_terminated_correctly: bool = False

    error: str = ""

    def overall_pass(self) -> bool:
        return all(
            (
                self.diagnosis_correct,
                self.recommendation_keywords_correct,
                self.risk_level_correct,
                self.loop_terminated_correctly,
            )
        )


# ---------------------------------------------------------------------------
# Stubs for hermetic execution
# ---------------------------------------------------------------------------

_STUB_PRINTER_IP = "192.168.99.10"


def _stub_ping(*_a, **_kw):
    return ToolResult(success=True, output={"reachable": True, "latency_ms": 1.0})


def _stub_tcp_connect(*_a, **_kw):
    return ToolResult(success=True, output={"open": True})


def _stub_dns(*_a, **_kw):
    return ToolResult(success=True, output={"ip": _STUB_PRINTER_IP, "resolved": True})


def _stub_arp(*_a, **_kw):
    return ToolResult(success=True, output={"mac": "00:07:4D:AA:BB:CC", "found": True})


def _make_cfg() -> Any:
    """tier0 / no-LLM / fast-fail planner config."""
    cfg = MagicMock()
    cfg.runtime.tier = "tier0"
    cfg.llm.planner_backend = "claude"
    cfg.llm.model = "stub"
    cfg.llm.temperature = 0.0
    cfg.llm.max_tokens = 256
    cfg.llm.timeout = 1.0
    cfg.llm.require_citations = False
    cfg.llm.json_schema.retries = 1
    cfg.ollama.host = "http://localhost:11434"
    cfg.ollama.model = "granite4"
    cfg.ollama.temperature = 0.0
    cfg.ollama.num_ctx = 1024
    return cfg


class _NoOpRetriever(Retriever):
    """Returns [] regardless of state — keeps eval reproducible without
    depending on a built RAG index.
    """

    def __init__(self) -> None:  # pragma: no cover - trivial
        self._unavailable = True
        # Provide harmless attributes so any introspection doesn't crash.
        self.index_path = Path("(eval-noop)")
        self.chunks_path = Path("(eval-noop)")
        self.embedding_model = "(none)"
        self._encoder = None
        self._index = None
        self._chunks = None

    def retrieve(self, query: str, k: int = 5):
        return []


# ---------------------------------------------------------------------------
# Patch helper — wires fixture replay into the tools module without
# depending on pytest's monkeypatch fixture.
# ---------------------------------------------------------------------------


class _FixturePatcher:
    """Context manager that monkey-patches SNMP/IPP/network tools for
    one fixture replay, then restores the originals on exit.
    """

    _SNMP_IPP_MODULE_PATHS = (
        ("zt411_agent.agent.tools", "snmp_get"),
        ("zt411_agent.agent.tools", "snmp_walk"),
        ("zt411_agent.agent.tools", "ipp_get_attributes"),
        ("zt411_agent.agent.device_specialist", "ipp_get_attributes"),
        ("zt411_agent.agent.tools", "ping"),
        ("zt411_agent.agent.tools", "tcp_connect"),
        ("zt411_agent.agent.tools", "dns_lookup"),
        ("zt411_agent.agent.tools", "arp_lookup"),
        ("zt411_agent.agent.network_specialist", "ping"),
        ("zt411_agent.agent.network_specialist", "tcp_connect"),
        ("zt411_agent.agent.network_specialist", "dns_lookup"),
        ("zt411_agent.agent.network_specialist", "arp_lookup"),
        ("zt411_agent.planner", "_tcp_reachable"),
    )

    def __init__(self, fixture_name: str) -> None:
        self.fixture_name = fixture_name
        self._originals: dict[tuple[str, str], Any] = {}

    def __enter__(self) -> "_FixturePatcher":
        replay = make_fixture_replay(self.fixture_name)
        replacements = {
            ("zt411_agent.agent.tools", "snmp_get"): replay["snmp_get"],
            ("zt411_agent.agent.tools", "snmp_walk"): replay["snmp_walk"],
            ("zt411_agent.agent.tools", "ipp_get_attributes"): replay[
                "ipp_get_attributes"
            ],
            (
                "zt411_agent.agent.device_specialist",
                "ipp_get_attributes",
            ): replay["ipp_get_attributes"],
            ("zt411_agent.agent.tools", "ping"): _stub_ping,
            ("zt411_agent.agent.tools", "tcp_connect"): _stub_tcp_connect,
            ("zt411_agent.agent.tools", "dns_lookup"): _stub_dns,
            ("zt411_agent.agent.tools", "arp_lookup"): _stub_arp,
            ("zt411_agent.agent.network_specialist", "ping"): _stub_ping,
            ("zt411_agent.agent.network_specialist", "tcp_connect"): _stub_tcp_connect,
            ("zt411_agent.agent.network_specialist", "dns_lookup"): _stub_dns,
            ("zt411_agent.agent.network_specialist", "arp_lookup"): _stub_arp,
            ("zt411_agent.planner", "_tcp_reachable"): lambda *a, **kw: False,
        }

        import importlib

        for module_name, attr in self._SNMP_IPP_MODULE_PATHS:
            mod = importlib.import_module(module_name)
            self._originals[(module_name, attr)] = getattr(mod, attr)
            setattr(mod, attr, replacements[(module_name, attr)])
        return self

    def __exit__(self, *_exc) -> None:
        import importlib

        for (module_name, attr), original in self._originals.items():
            mod = importlib.import_module(module_name)
            setattr(mod, attr, original)


# ---------------------------------------------------------------------------
# Single-case scoring
# ---------------------------------------------------------------------------


def _score_one(case: EvalCase, *, max_loop_steps: int = 5) -> CaseResult:
    state = AgentState(os_platform=OSPlatform.LINUX, symptoms=[case.symptom])
    state.device.ip = _STUB_PRINTER_IP

    result = CaseResult(
        case_id=case.case_id,
        fixture=case.fixture_path,
        expected_diagnosis=case.expected_diagnosis,
        actual_diagnosis="",
        diagnosis_correct=False,
        expected_keywords=list(case.expected_recommendation_keywords),
        expected_risk=case.expected_risk_level,
        expected_loop_status=case.expected_loop_status,
        expected_escalation_reason=case.expected_escalation_reason,
    )

    try:
        with _FixturePatcher(case.fixture_path):
            orch = Orchestrator(
                specialists=[
                    DeviceSpecialist(),
                    NetworkSpecialist(),
                    CUPSSpecialist(),
                    WindowsSpecialist(),
                    ValidationSpecialist(),
                ],
                cfg=_make_cfg(),
                max_loop_steps=max_loop_steps,
                retriever=_NoOpRetriever(),
            )
            final_state = orch.run(state)
    except Exception as exc:  # noqa: BLE001
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    # 1. Diagnosis
    result.actual_diagnosis = final_state.device.printer_status
    result.diagnosis_correct = (
        result.actual_diagnosis == case.expected_diagnosis
    )

    # 2. Recommendation keywords (across action_log + evidence)
    blob = " ".join(
        [a.action.lower() for a in final_state.action_log]
        + [(a.result or "").lower() for a in final_state.action_log]
        + [ev.content.lower() for ev in final_state.evidence]
    )
    matched = [kw for kw in case.expected_recommendation_keywords if kw.lower() in blob]
    result.matched_keywords = matched
    if case.no_action_expected:
        result.recommendation_keywords_correct = True
    else:
        result.recommendation_keywords_correct = (
            len(matched) == len(case.expected_recommendation_keywords)
        )

    # 3. Risk level — at least one action_log entry must have the
    # expected risk. Default "safe" matches the DeviceSpecialist
    # high-level entry that fires every iteration.
    risks_present = {a.risk.value for a in final_state.action_log}
    result.matched_risk = case.expected_risk_level in risks_present
    if case.no_action_expected:
        # Idle cases never log a recommendation; we accept any risk.
        result.risk_level_correct = True
    else:
        result.risk_level_correct = result.matched_risk

    # 4. Loop termination
    result.actual_loop_status = final_state.loop_status.value
    result.actual_escalation_reason = final_state.escalation_reason or ""

    if case.no_action_expected:
        result.loop_terminated_correctly = (
            result.actual_loop_status
            in {"escalated", "success", "max_steps", "running"}
        )
    else:
        status_ok = result.actual_loop_status == case.expected_loop_status
        if case.expected_escalation_reason is None:
            reason_ok = True
        else:
            reason_ok = (
                result.actual_escalation_reason
                == case.expected_escalation_reason
            )
        result.loop_terminated_correctly = status_ok and reason_ok

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _write_csv(results: list[CaseResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = [
        "case_id",
        "fixture",
        "diagnosis_correct",
        "actual_diagnosis",
        "expected_diagnosis",
        "recommendation_keywords_correct",
        "matched_keywords",
        "expected_keywords",
        "risk_level_correct",
        "matched_risk",
        "expected_risk",
        "loop_terminated_correctly",
        "actual_loop_status",
        "expected_loop_status",
        "actual_escalation_reason",
        "expected_escalation_reason",
        "overall_pass",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in results:
            w.writerow(
                [
                    r.case_id,
                    r.fixture,
                    int(r.diagnosis_correct),
                    r.actual_diagnosis,
                    r.expected_diagnosis,
                    int(r.recommendation_keywords_correct),
                    "|".join(r.matched_keywords),
                    "|".join(r.expected_keywords),
                    int(r.risk_level_correct),
                    int(r.matched_risk),
                    r.expected_risk,
                    int(r.loop_terminated_correctly),
                    r.actual_loop_status,
                    r.expected_loop_status,
                    r.actual_escalation_reason,
                    r.expected_escalation_reason or "",
                    int(r.overall_pass()),
                    r.error,
                ]
            )


def _summary(results: list[CaseResult]) -> SimpleNamespace:
    total = len(results)

    def pct(passes: int) -> str:
        return f"{passes}/{total} ({(passes / total * 100.0):.1f}%)" if total else "0/0"

    diag = sum(1 for r in results if r.diagnosis_correct)
    kw = sum(1 for r in results if r.recommendation_keywords_correct)
    risk = sum(1 for r in results if r.risk_level_correct)
    loop = sum(1 for r in results if r.loop_terminated_correctly)
    overall = sum(1 for r in results if r.overall_pass())
    return SimpleNamespace(
        total=total,
        diagnosis=pct(diag),
        keywords=pct(kw),
        risk=pct(risk),
        loop=pct(loop),
        overall=pct(overall),
        overall_count=overall,
    )


def _print_summary(results: list[CaseResult]) -> SimpleNamespace:
    s = _summary(results)
    print()
    print("ZT411 Eval Harness — baseline run")
    print(f"Cases run: {s.total}")
    print(f"diagnosis_correct:        {s.diagnosis}")
    print(f"recommendation_keywords:  {s.keywords}")
    print(f"risk_level_correct:       {s.risk}")
    print(f"loop_terminated_correctly: {s.loop}")
    print(f"Overall pass rate:        {s.overall}")
    return s


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--max-loop-steps",
        type=int,
        default=5,
        help="Per-case orchestrator step cap.",
    )
    parser.add_argument(
        "--results-dir",
        default=str(_HERE / "results"),
        help="Where to write the CSV.",
    )
    parser.add_argument(
        "--pass-threshold",
        type=float,
        default=0.70,
        help="Overall pass-rate threshold for exit 0.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cases = load_cases()
    results: list[CaseResult] = []
    for case in cases:
        r = _score_one(case, max_loop_steps=args.max_loop_steps)
        results.append(r)
        status = "PASS" if r.overall_pass() else "FAIL"
        if r.error:
            print(
                f"[{status}] {r.case_id} ({case.fixture_path}) — "
                f"ERROR: {r.error}"
            )
        else:
            print(
                f"[{status}] {r.case_id} ({case.fixture_path}) — "
                f"diag={r.diagnosis_correct} kw={r.recommendation_keywords_correct} "
                f"risk={r.risk_level_correct} loop={r.loop_terminated_correctly}"
            )

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = Path(args.results_dir) / f"eval_{timestamp}.csv"
    _write_csv(results, csv_path)
    print(f"\nWrote: {csv_path}")

    summary = _print_summary(results)
    pass_rate = summary.overall_count / summary.total if summary.total else 0.0
    return 0 if pass_rate >= args.pass_threshold else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
