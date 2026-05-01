"""
Phase 3 — Session B.6
Live Claude-tier citation verification + cost guardrail.

Runs the full agent loop against the live ZT411 (or a paused-printer
fixture replay if ``--dry-run`` is set), with a real cloud-tier
planner (default: claude-sonnet-4-6 — the only Claude family
permitted on the user's Evaluation-access plan as of 2026-04). Tracks
cumulative API spend in-process and aborts cleanly before any call
that would push spend over the configured limit.

Two operational modes:

* ``--smoke-check`` — non-interactive. Loads config, runs the
  pre-flight model gate, builds the orchestrator, verifies
  ``ANTHROPIC_API_KEY`` is set, and issues a single 1-token Anthropic
  API ping to confirm cloud-tier reachability. Exits 0 on success.
  Designed to run inside Claude Code so the live-run handoff has a
  green light before the human takes over.

* default — interactive live loop. Confirms baseline idle via SNMP,
  prompts the human to press PAUSE on the front panel, runs the
  orchestrator, prompts the human to resume, confirms idle restored.
  Logs to both stdout and ``tests/logs/session_b6_<ts>.log``.

Use ``--dry-run`` to swap interactive prompts and live SNMP/IPP/network
probes for the captured paused-printer fixture; the planner still
makes real cloud-tier API calls — the real-money assertion is the
verified-citation check, and it has to fire against real Claude output.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

# Ensure we can import zt411_agent regardless of where this script is run
# from. The repo layout puts the package at src/zt411_agent under the
# parent dir; an editable install handles imports for any cwd, but if
# the user runs without installing, fall back to a direct sys.path patch.
_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parents[1]   # zt411-troubleshooter-agent/
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from zt411_agent.agent.cups_specialist import CUPSSpecialist
from zt411_agent.agent.device_specialist import DeviceSpecialist
from zt411_agent.agent.network_specialist import NetworkSpecialist
from zt411_agent.agent.orchestrator import Orchestrator
from zt411_agent.agent.tools import ToolResult, snmp_zt411_physical_flags
from zt411_agent.agent.validation_specialist import ValidationSpecialist
from zt411_agent.agent.windows_specialist import WindowsSpecialist
from zt411_agent.cost_tracking import (
    SessionBudget,
    SessionBudgetExceeded,
    estimate_cost_usd,
)
from zt411_agent.planner import RagSnippet, RuntimeTier, build_planner
from zt411_agent.state import AgentState, LoopStatus, OSPlatform


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_PRINTER_IP = "192.168.99.10"
DEFAULT_MODEL = "claude-sonnet-4-6"
PAUSE_FIXTURE_NAME = "zt411_fixture_paused.json"
LOG_DIR = _REPO_ROOT / "tests" / "logs"
CONFIG_BASE = _REPO_ROOT / "configs" / "runtime" / "base.yaml"
CONFIG_CLOUD = _REPO_ROOT / "configs" / "runtime" / "cloud.yaml"

# Models the Evaluation-access plan does NOT permit. Pre-flight gate.
_BLOCKED_MODELS_WITHOUT_FLAG: set[str] = {"claude-opus-4-7", "claude-opus-4-6"}

logger = logging.getLogger("session_b6")


# ---------------------------------------------------------------------------
# Config loading — yaml -> SimpleNamespace tree
# ---------------------------------------------------------------------------


def _ns_from_dict(d: Any) -> Any:
    """Recursively convert a dict into a SimpleNamespace tree so the
    planner / orchestrator can use attribute access (``cfg.runtime.tier``).
    Lists and scalars pass through unchanged.
    """
    if isinstance(d, dict):
        return SimpleNamespace(**{k: _ns_from_dict(v) for k, v in d.items()})
    if isinstance(d, list):
        return [_ns_from_dict(x) for x in d]
    return d


def _load_config(extra_overlay: Path | None = None) -> SimpleNamespace:
    """Load base.yaml (always), optionally overlay cloud.yaml or another
    file, return as a SimpleNamespace tree.

    Shallow merge per top-level section is enough — the real config is
    flat-ish and the planner / orchestrator only reach two levels deep.
    """
    with CONFIG_BASE.open("r", encoding="utf-8") as fh:
        merged: dict[str, Any] = yaml.safe_load(fh) or {}

    if extra_overlay and extra_overlay.exists():
        with extra_overlay.open("r", encoding="utf-8") as fh:
            overlay = yaml.safe_load(fh) or {}
        for top_key, sub in overlay.items():
            if isinstance(sub, dict) and isinstance(merged.get(top_key), dict):
                merged[top_key].update(sub)
            else:
                merged[top_key] = sub

    return _ns_from_dict(merged)


# ---------------------------------------------------------------------------
# Logging setup — dual stdout + file
# ---------------------------------------------------------------------------


def _configure_logging(log_path: Path | None) -> Path | None:
    fmt = "%(asctime)s [%(levelname)s] %(name)s :: %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, mode="w", encoding="utf-8"))

    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers, force=True)
    return log_path


# ---------------------------------------------------------------------------
# Pre-flight model check
# ---------------------------------------------------------------------------


def _preflight_model(model: str, allow_opus: bool) -> None:
    """Refuse to start if the chosen model is blocked on the
    Evaluation-access plan and ``--allow-opus`` was not passed.

    Prints to stderr and raises SystemExit(2) so the failure is visible
    BEFORE any printer / API interaction.
    """
    if model in _BLOCKED_MODELS_WITHOUT_FLAG and not allow_opus:
        print(
            "ERROR: Evaluation-access plan does not permit Opus models. "
            "Use --model claude-sonnet-4-6 (default) or pass --allow-opus "
            "if you have purchased credits.",
            file=sys.stderr,
        )
        raise SystemExit(2)


# ---------------------------------------------------------------------------
# Cloud-tier connectivity probe (smoke check only)
# ---------------------------------------------------------------------------


class CloudProbeError(RuntimeError):
    """Raised by ``_cloud_probe`` with a category + payload so the
    smoke-check can surface a more actionable diagnostic than the raw
    httpx exception text.
    """

    def __init__(self, category: str, status: int, body: str) -> None:
        super().__init__(f"{category} (HTTP {status}): {body[:300]}")
        self.category = category
        self.status = status
        self.body = body


def _cloud_probe(model: str, timeout: float = 10.0) -> dict[str, Any]:
    """Issue one cheap 1-token Claude completion to verify the API key
    works and the chosen model is permitted. Returns usage dict on
    success; raises ``CloudProbeError`` on any failure with a category
    string the caller can branch on (``auth``, ``model_permission``,
    ``credit_balance``, ``http`` for everything else).
    """
    import httpx

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise CloudProbeError("auth", 0, "ANTHROPIC_API_KEY is not set")

    headers = {
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01",
        "x-api-key": api_key,
    }
    body = {
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "ping"}],
    }
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json=body,
        timeout=timeout,
    )
    if resp.status_code == 200:
        return (resp.json() or {}).get("usage", {}) or {}

    # Categorise common failure modes so the smoke check can give the
    # operator an actionable next step.
    text = resp.text or ""
    lowered = text.lower()
    if resp.status_code == 401:
        category = "auth"
    elif resp.status_code == 403 or "model" in lowered and "permission" in lowered:
        category = "model_permission"
    elif "credit balance" in lowered or "credit_balance" in lowered:
        category = "credit_balance"
    else:
        category = "http"
    raise CloudProbeError(category, resp.status_code, text)


# ---------------------------------------------------------------------------
# Dry-run tool patches (replay fixture for SNMP/IPP, stubs for network)
# ---------------------------------------------------------------------------


def _install_dry_run_patches() -> None:
    """Replace SNMP/IPP/network tools with replay/stub callables in
    every module they're imported into. Mirrors the monkeypatching the
    hermetic fixture-replay tests do, but in a script context so it
    persists for the full orchestrator run.
    """
    import zt411_agent.agent.tools as tools_mod
    import zt411_agent.agent.device_specialist as ds_mod
    import zt411_agent.agent.network_specialist as ns_mod

    # tests/fixtures lives at the repo root, not in the importable
    # package — add it to sys.path on demand for dry-run mode.
    tests_dir = _REPO_ROOT
    if str(tests_dir) not in sys.path:
        sys.path.insert(0, str(tests_dir))
    from tests.fixtures.replay import make_fixture_replay   # noqa: PLC0415

    replay = make_fixture_replay(PAUSE_FIXTURE_NAME)

    tools_mod.snmp_get = replay["snmp_get"]
    tools_mod.snmp_walk = replay["snmp_walk"]
    tools_mod.ipp_get_attributes = replay["ipp_get_attributes"]
    ds_mod.ipp_get_attributes = replay["ipp_get_attributes"]

    def _stub_ping(ip, timeout_s=2.0, count=1):
        return ToolResult(
            success=True,
            output={"reachable": True, "latency_ms": 1.0},
            raw=f"ping {ip} ok (stub)",
        )

    def _stub_tcp_connect(ip, port, timeout_s=3.0):
        return ToolResult(success=True, output={"open": True})

    def _stub_dns_lookup(hostname):
        return ToolResult(success=True, output={"ip": DEFAULT_PRINTER_IP, "resolved": True})

    def _stub_arp_lookup(ip):
        return ToolResult(
            success=True,
            output={"mac": "00:07:4D:AB:CD:EF", "found": True},
            raw="stub arp",
        )

    tools_mod.ping = _stub_ping
    tools_mod.tcp_connect = _stub_tcp_connect
    tools_mod.dns_lookup = _stub_dns_lookup
    tools_mod.arp_lookup = _stub_arp_lookup
    ns_mod.ping = _stub_ping
    ns_mod.tcp_connect = _stub_tcp_connect
    ns_mod.dns_lookup = _stub_dns_lookup
    ns_mod.arp_lookup = _stub_arp_lookup


# ---------------------------------------------------------------------------
# Live-mode pause/resume confirmation helpers
# ---------------------------------------------------------------------------


def _read_pause_state(ip: str) -> bool | None:
    """Return device.paused via SNMP. None if the read failed entirely."""
    try:
        r = snmp_zt411_physical_flags(ip)
    except Exception as exc:  # noqa: BLE001
        logger.warning("snmp_zt411_physical_flags raised: %s", exc)
        return None
    if not r.success or r.output is None:
        return None
    return bool(r.output.get("paused"))


def _wait_for_pause(ip: str, retries: int = 1) -> bool:
    """Confirm the printer is paused. Returns True on success.

    Prompts the human, then re-reads SNMP. ``retries`` controls how
    many additional retry rounds are allowed if the first attempt
    returns paused=False (typo on the front panel, etc.).
    """
    for attempt in range(retries + 1):
        prompt = (
            "Press PAUSE on the front panel. Press Enter when the pause LED is lit."
            if attempt == 0
            else "Pause not detected. Press PAUSE again, then Enter."
        )
        input(prompt)
        paused = _read_pause_state(ip)
        if paused:
            logger.info("Pause confirmed via SNMP.")
            return True
        logger.warning("Pause not detected (attempt %d/%d).", attempt + 1, retries + 1)
    return False


def _wait_for_resume(ip: str, retries: int = 1) -> bool:
    for attempt in range(retries + 1):
        prompt = (
            "Press PAUSE again to resume. Press Enter when ready."
            if attempt == 0
            else "Still paused. Press PAUSE to resume, then Enter."
        )
        input(prompt)
        paused = _read_pause_state(ip)
        if paused is False:
            logger.info("Resume confirmed via SNMP.")
            return True
        logger.warning("Resume not detected (attempt %d/%d).", attempt + 1, retries + 1)
    return False


# ---------------------------------------------------------------------------
# Acceptance review
# ---------------------------------------------------------------------------


def _review_state(state: AgentState) -> dict[str, Any]:
    """Inspect terminal state for the four Session B.6 assertions.

    Returns a dict of bools + supporting detail. Caller logs a
    human-readable summary.
    """
    citation_evidence = [ev for ev in state.evidence if ev.source == "planner_citations"]

    # Build the set of every snippet_id the planner cited across the run.
    cited_ids: list[str] = []
    for ev in citation_evidence:
        if ev.snippet_id:
            cited_ids.extend(s.strip() for s in ev.snippet_id.split(",") if s.strip())

    # Resolved tier — recorded by the planner each call. We don't have
    # the per-call tier directly in evidence, but the offline planner
    # never emits planner_citations evidence by design. Presence of any
    # planner_citations evidence is the proxy for "cloud tier resolved".
    cloud_tier_engaged = bool(citation_evidence)

    return {
        "planner_citations_evidence_present": bool(citation_evidence),
        "citation_count": len(citation_evidence),
        "cited_snippet_ids": cited_ids,
        "cloud_tier_engaged": cloud_tier_engaged,
        "loop_status": state.loop_status.value,
        "escalation_reason": state.escalation_reason,
        "loop_counter": state.loop_counter,
        "evidence_count": len(state.evidence),
        "action_log_count": len(state.action_log),
    }


# ---------------------------------------------------------------------------
# Main dispatch — smoke check vs full run
# ---------------------------------------------------------------------------


def _run_smoke_check(args: argparse.Namespace) -> int:
    """Non-interactive sanity check. Returns process exit code."""
    print("Session B.6 smoke check — non-interactive dependency probe.")
    _preflight_model(args.model, allow_opus=args.allow_opus)

    cfg = _load_config(extra_overlay=CONFIG_CLOUD)
    cfg.runtime.tier = "tier2"
    cfg.llm.model = args.model

    if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return 3

    budget = SessionBudget(model=args.model, limit_usd=args.budget_limit)

    # Build the same dependency graph the live run will use, so a
    # construction failure shows up here rather than mid-run.
    specialists = [
        DeviceSpecialist(),
        NetworkSpecialist(),
        CUPSSpecialist(),
        WindowsSpecialist(),
        ValidationSpecialist(),
    ]
    Orchestrator(specialists=specialists, cfg=cfg, max_loop_steps=args.max_steps)

    # Verify cloud-tier reachability with a 1-token call (also confirms
    # the chosen model is permitted on this account).
    try:
        usage = _cloud_probe(args.model)
    except CloudProbeError as exc:
        hint = {
            "auth": "Check that ANTHROPIC_API_KEY is correct and not revoked.",
            "model_permission": (
                "The current account is not permitted to use this model. "
                "Try --model claude-haiku-4-5 or upgrade the account."
            ),
            "credit_balance": (
                "Account has $0 of credit. Add credit (Console → Plans & "
                "Billing) before running the live verification."
            ),
            "http": "Unexpected HTTP error — see body above.",
        }.get(exc.category, "")
        print(
            f"ERROR: cloud-tier probe failed [{exc.category}, HTTP {exc.status}]: {exc.body[:200]}",
            file=sys.stderr,
        )
        if hint:
            print(f"HINT: {hint}", file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: cloud-tier probe failed (unexpected): {exc}", file=sys.stderr)
        return 4

    in_t = int(usage.get("input_tokens", 0) or 0)
    out_t = int(usage.get("output_tokens", 0) or 0)
    cost = estimate_cost_usd(args.model, in_t, out_t)
    print(
        f"Cloud probe OK — model={args.model} input_tokens={in_t} "
        f"output_tokens={out_t} cost=${cost:.6f}"
    )
    print(f"Budget: limit=${budget.limit_usd:.4f} model={budget.model}")
    print("smoke check passed")
    return 0


def _run_full(args: argparse.Namespace) -> int:
    """Interactive live run (or fixture-driven dry run) of the full loop."""
    ip = args.printer_ip
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"session_b6_{timestamp}.log"
    _configure_logging(log_path)

    logger.info("=" * 72)
    logger.info(
        "Session B.6 live loop starting — printer=%s model=%s budget=$%.4f dry_run=%s",
        ip, args.model, args.budget_limit, args.dry_run,
    )
    logger.info("Log file: %s", log_path)

    if args.dry_run:
        logger.info("Installing dry-run tool replay patches (paused fixture).")
        _install_dry_run_patches()

    # Build config, override model, pin tier=cloud.
    cfg = _load_config(extra_overlay=CONFIG_CLOUD)
    cfg.runtime.tier = "tier2"
    cfg.llm.model = args.model
    # Limit retries so a failing key doesn't burn the budget on retry storms.
    if hasattr(cfg.llm, "json_schema"):
        cfg.llm.json_schema.retries = 2

    budget = SessionBudget(model=args.model, limit_usd=args.budget_limit)

    # Idle baseline (live mode only — fixture is, by definition, paused).
    if not args.dry_run:
        baseline_paused = _read_pause_state(ip)
        if baseline_paused is None:
            logger.error("Could not read pause state via SNMP. Aborting.")
            return 5
        if baseline_paused:
            logger.error(
                "Printer is already paused. Resume to idle before running this script."
            )
            return 6
        logger.info("Idle baseline confirmed.")

        if not _wait_for_pause(ip):
            logger.error("Could not detect a front-panel pause. Aborting.")
            return 7
    else:
        logger.info("Dry-run: skipping idle/pause prompts.")

    # Build orchestrator + planner with budget hook.
    specialists = [
        DeviceSpecialist(),
        NetworkSpecialist(),
        CUPSSpecialist(),
        WindowsSpecialist(),
        ValidationSpecialist(),
    ]
    orch = Orchestrator(specialists=specialists, cfg=cfg, max_loop_steps=args.max_steps)
    real_planner = build_planner(cfg, on_usage=budget.record)

    def _wrapped_planner(state: AgentState, snippets: list[RagSnippet]):
        # Pre-call budget gate. Fires BEFORE another billable request,
        # so the abort never includes the call that would have pushed
        # spend over the limit.
        budget.check_or_raise()
        plan = real_planner(state, snippets)
        budget.log_summary()
        return plan

    orch._planner = _wrapped_planner   # type: ignore[assignment]

    initial_state = AgentState(
        os_platform=OSPlatform.WINDOWS,
        symptoms=["printer paused"],
    )
    initial_state.device.ip = ip

    # Seed the planner with one canned snippet so even if the live RAG
    # index is empty/missing the citation path has something to cite.
    seed_snippet = RagSnippet(
        snippet_id="ZT411_OG_pause_p45",
        source="ZT411 Operations Guide",
        section="Pause / Resume",
        text=(
            "Press the PAUSE button on the front panel to resume printing "
            "after a pause condition. Verify no other faults are present."
        ),
        score=0.92,
    )

    final_state: AgentState = initial_state
    abort_reason: str | None = None
    t0 = time.monotonic()

    try:
        final_state = orch.run(initial_state, [seed_snippet])
    except SessionBudgetExceeded as exc:
        logger.error("Session aborted by budget guard: %s", exc)
        abort_reason = f"budget_exceeded: {exc}"
        final_state = initial_state
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error during loop")
        abort_reason = f"exception: {exc!r}"
    finally:
        elapsed = time.monotonic() - t0
        budget.log_summary()
        logger.info("Loop wall time: %.2fs", elapsed)

    review = _review_state(final_state)
    logger.info("=" * 72)
    logger.info("Acceptance review:")
    for k, v in review.items():
        logger.info("  %-40s %s", k, v)
    logger.info("budget.cost_usd: $%.6f / $%.4f", budget.cost_usd, budget.limit_usd)
    if abort_reason:
        logger.info("abort_reason: %s", abort_reason)

    # Live-mode resume prompt.
    if not args.dry_run:
        if not _wait_for_resume(ip):
            logger.error("Resume not detected. Operator: please verify printer state.")
            # Don't fail the whole run for this — the loop completed.
        else:
            logger.info("Idle restored.")

    print(f"\nSession B.6 log: {log_path}")
    print(f"Final cost: ${budget.cost_usd:.6f} / limit ${budget.limit_usd:.4f}")

    # Exit 0 even on budget abort — the abort itself is handled state,
    # not a crash. Only a non-budget exception is a non-zero exit.
    if abort_reason and not abort_reason.startswith("budget_exceeded"):
        return 1
    return 0


# ---------------------------------------------------------------------------
# Argparse + main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="session_b6_live_loop",
        description=(
            "Session B.6 live Claude-tier citation verification with "
            "in-script API spend guardrail."
        ),
    )
    p.add_argument(
        "--budget-limit",
        type=float,
        default=0.10,
        help="Hard limit (USD) on cumulative session API spend. "
             "Default 0.10. Loop aborts cleanly before any call that "
             "would push spend over.",
    )
    p.add_argument(
        "--model",
        type=str,
        default=DEFAULT_MODEL,
        help=(
            "Cloud-tier planner model. Default claude-sonnet-4-6 "
            "(Evaluation-access permits Sonnet, NOT Opus). Pass "
            "--allow-opus to override the Opus block."
        ),
    )
    p.add_argument(
        "--allow-opus",
        action="store_true",
        help="Override the Evaluation-access Opus block. Only set if "
             "the account has paid credits.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Replay the captured paused fixture instead of "
             "interacting with the live printer. Real planner calls "
             "still happen.",
    )
    p.add_argument(
        "--smoke-check",
        action="store_true",
        help="Non-interactive: build deps, verify ANTHROPIC_API_KEY, "
             "issue one 1-token API ping, exit 0 on success.",
    )
    p.add_argument(
        "--printer-ip",
        type=str,
        default=DEFAULT_PRINTER_IP,
        help=f"Printer IP (default {DEFAULT_PRINTER_IP}). Ignored in dry-run.",
    )
    p.add_argument(
        "--max-steps",
        type=int,
        default=5,
        help="Orchestrator max_loop_steps (default 5).",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    _preflight_model(args.model, allow_opus=args.allow_opus)

    # For non-smoke runs, full logging happens inside _run_full so the
    # log file timestamp reflects the actual loop start. For smoke
    # check, route to stdout only (no log file).
    if args.smoke_check:
        _configure_logging(log_path=None)
        return _run_smoke_check(args)
    return _run_full(args)


if __name__ == "__main__":
    sys.exit(main())
