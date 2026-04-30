"""
planner.py — LLM planning backend for the ZT411 troubleshooter agent.

Responsibilities
----------------
1. Runtime tier detection
   - Probe connectivity targets defined in base.yaml.
   - Tier 2 (cloud)  → Claude API via Anthropic.
   - Tier 1 (local)  → Ollama (local LLM, e.g. granite4).
   - Tier 0 (offline)→ deterministic rule-based fallback; no LLM required.

2. Planner prompt construction
   - Structured state summary + retrieved RAG snippets as input.
   - Enforces require_citations and disallow_hallucinations config flags.
   - Returns a typed PlannerResponse with citation IDs and risk level.

3. Planner call with JSON schema enforcement + retry
   - Validates required keys before returning.
   - Falls back to pure utility-score routing on total failure.

Usage (from orchestrator)
-------------------------
    from .planner import build_planner, PlannerResponse

    planner = build_planner(cfg)          # once at startup
    plan: PlannerResponse = planner(state, snippets)
"""

from __future__ import annotations

import json
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class RuntimeTier(str, Enum):
    CLOUD = "tier2"
    LOCAL = "tier1"
    OFFLINE = "tier0"


@dataclass
class RagSnippet:
    snippet_id: str
    source: str
    section: str
    text: str
    score: float = 0.0


@dataclass
class PlannerResponse:
    """Structured output returned by the planner to the orchestrator."""

    ranked_specialists: list[str] = field(default_factory=list)
    rationale: str = ""
    citation_ids: list[str] = field(default_factory=list)   # snippet_ids cited
    risk_level: str = "safe"
    success_criteria_met: bool = False
    escalate: bool = False
    escalation_reason: str = ""
    tier_used: RuntimeTier = RuntimeTier.OFFLINE
    raw_response: str = ""


# ---------------------------------------------------------------------------
# Connectivity probe → tier detection
# ---------------------------------------------------------------------------

_DEFAULT_PROBE_TARGETS = [
    ("1.1.1.1", 53),
    ("8.8.8.8", 53),
    ("www.zebra.com", 443),
]
_ANTHROPIC_HOST = ("api.anthropic.com", 443)
_PROBE_TIMEOUT = 2.0   # seconds


def _tcp_reachable(host: str, port: int, timeout: float = _PROBE_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def detect_runtime_tier(
    probe_targets: list[tuple[str, int]] | None = None,
    ollama_host: str = "http://localhost:11434",
    force_tier: str | None = None,
) -> RuntimeTier:
    """
    Probe connectivity and return the highest viable runtime tier.

    Priority: cloud (tier2) > local LLM (tier1) > offline (tier0).
    A forced tier (from config runtime.tier) bypasses probing.
    """
    if force_tier and force_tier not in ("auto",):
        mapping = {"tier2": RuntimeTier.CLOUD, "tier1": RuntimeTier.LOCAL, "tier0": RuntimeTier.OFFLINE}
        if force_tier in mapping:
            logger.info("Runtime tier forced to %s by config.", force_tier)
            return mapping[force_tier]

    # Cloud: need general internet + Anthropic reachable
    targets = probe_targets or _DEFAULT_PROBE_TARGETS
    internet_ok = any(_tcp_reachable(h, p) for h, p in targets)
    anthropic_ok = _tcp_reachable(*_ANTHROPIC_HOST)

    if internet_ok and anthropic_ok:
        logger.info("Connectivity probe: cloud reachable → tier2")
        return RuntimeTier.CLOUD

    # Local: Ollama must be listening
    try:
        parsed_host = ollama_host.removeprefix("http://").removeprefix("https://")
        ollama_ip, ollama_port_str = (parsed_host.split(":") + ["11434"])[:2]
        if _tcp_reachable(ollama_ip, int(ollama_port_str)):
            logger.info("Connectivity probe: Ollama reachable → tier1")
            return RuntimeTier.LOCAL
    except Exception as exc:
        logger.debug("Ollama probe failed: %s", exc)

    logger.info("Connectivity probe: offline → tier0")
    return RuntimeTier.OFFLINE


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

PLANNER_SYSTEM_PROMPT = """\
You are the planning brain of a ZT411 industrial label printer troubleshooting agent.

INPUT: A structured summary of the current agent state plus retrieved knowledge-base
       snippets (each identified by a snippet_id).

OUTPUT: A single JSON object — no markdown fences, no preamble, no explanation.

JSON schema:
{
  "ranked_specialists": ["<name>", ...],
  "rationale": "<one sentence explaining the choice>",
  "citation_ids": ["<snippet_id>", ...],
  "risk_level": "safe|low|medium|config_change|service_restart|reboot|firmware|destructive",
  "success_criteria_met": false,
  "escalate": false,
  "escalation_reason": ""
}

Rules:
- ranked_specialists must contain at least one name from:
    windows_specialist, cups_specialist, network_specialist,
    device_specialist, validation_specialist
- citation_ids MUST reference snippet_ids from the provided snippets.
  Every non-trivial recommendation requires at least one citation.
  If you cannot cite a snippet, lower your confidence and recommend
  information-gathering actions instead.
- risk_level reflects the highest-risk action in ranked_specialists' likely
  next steps, not a worst-case theoretical risk.
- Set success_criteria_met only when queue is drained, device is ready,
  AND a test print has succeeded — all three must be confirmed by tool output.
- Set escalate when no specialist can make progress, or a required human action
  (e.g. physical media reload) has been waiting > 2 loop turns.
"""


def _format_snippets(snippets: list[RagSnippet]) -> str:
    if not snippets:
        return "(no knowledge-base snippets retrieved for this turn)"
    lines = ["Retrieved knowledge-base snippets:"]
    for s in snippets:
        lines.append(
            f"  [{s.snippet_id}] {s.source} § {s.section} (score={s.score:.2f})\n"
            f"    {s.text[:300].replace(chr(10), ' ')}"
        )
    return "\n".join(lines)


def _build_planner_prompt(state: Any, snippets: list[RagSnippet]) -> str:
    """Serialise the agent state into a token-efficient LLM prompt."""
    lines = [
        "=== AGENT STATE ===",
        f"session_id     : {state.session_id}",
        f"os_platform    : {state.os_platform}",
        f"loop_counter   : {state.loop_counter}",
        f"symptoms       : {state.symptoms}",
        f"user_description: {state.user_description}",
        f"last_specialist: {state.last_specialist}",
        f"visited        : {state.visited_specialists}",
        "",
        "--- device ---",
        f"  status     : {state.device.printer_status}",
        f"  alerts     : {state.device.alerts}",
        f"  error_codes: {state.device.error_codes}",
        f"  head_open={state.device.head_open}  media_out={state.device.media_out}"
        f"  ribbon_out={state.device.ribbon_out}  paused={state.device.paused}",
        "",
        "--- network ---",
        f"  reachable={state.network.reachable}  latency_ms={state.network.latency_ms}",
        f"  ports_open={state.network.port_open}",
        "",
        "--- cups ---",
        f"  queue={state.cups.queue_name}  state={state.cups.queue_state}"
        f"  pending_jobs={state.cups.pending_jobs}",
        f"  filter_errors={state.cups.filter_errors}",
        "",
        "--- windows ---",
        f"  spooler={state.windows.spooler_running}  queue={state.windows.queue_name}"
        f"  state={state.windows.queue_state}  pending={state.windows.pending_jobs}",
        "",
        "--- success flags ---",
        f"  queue_drained={state.queue_drained}  test_print_ok={state.test_print_ok}"
        f"  device_ready={state.device_ready}",
        "",
        "--- recent evidence (last 4) ---",
    ]
    for ev in state.evidence[-4:]:
        lines.append(f"  [{ev.specialist}|{ev.source}] {ev.content[:140]}")

    lines += [
        "",
        "--- recent actions (last 3) ---",
    ]
    for act in state.action_log[-3:]:
        lines.append(f"  [{act.specialist}] {act.action} → {act.status} {act.result[:100]}")

    lines += ["", _format_snippets(snippets)]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON schema validation + prompt injection sanitisation
# ---------------------------------------------------------------------------

_INJECTION_PATTERNS = [
    re.compile(r"(ignore|disregard|forget).{0,30}(previous|above|prior).{0,30}instruction", re.I),
    re.compile(r"system\s*prompt", re.I),
    re.compile(r"```.*?```", re.DOTALL),     # strip code fences that might embed commands
]

_REQUIRED_KEYS = {"ranked_specialists", "rationale", "citation_ids", "risk_level",
                  "success_criteria_met", "escalate", "escalation_reason"}

_VALID_SPECIALISTS = {
    "windows_specialist", "cups_specialist", "network_specialist",
    "device_specialist", "validation_specialist",
}

_VALID_RISK_LEVELS = {
    "safe", "low", "medium", "config_change", "service_restart",
    "reboot", "firmware", "destructive",
}


def _sanitise_snippet(text: str) -> str:
    """Strip prompt-injection attempts from a retrieved snippet."""
    for pat in _INJECTION_PATTERNS:
        text = pat.sub("[redacted]", text)
    return text


def _validate_planner_json(raw: str, require_citations: bool) -> dict[str, Any]:
    """
    Parse and validate the LLM's JSON response.
    Raises ValueError with a descriptive message on any schema violation.
    """
    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON parse error: {exc}") from exc

    missing = _REQUIRED_KEYS - data.keys()
    if missing:
        raise ValueError(f"Missing required keys: {missing}")

    # ranked_specialists — must have at least one valid name
    specialists = data.get("ranked_specialists", [])
    if not specialists:
        raise ValueError("ranked_specialists is empty")
    invalid = [s for s in specialists if s not in _VALID_SPECIALISTS]
    if invalid:
        raise ValueError(f"Unknown specialist names: {invalid}")

    # risk_level
    if data.get("risk_level") not in _VALID_RISK_LEVELS:
        raise ValueError(f"Invalid risk_level: {data.get('risk_level')}")

    # citation enforcement
    if require_citations and not data.get("citation_ids"):
        raise ValueError(
            "require_citations=true but citation_ids is empty — "
            "every recommendation must cite a knowledge-base snippet"
        )

    return data


# ---------------------------------------------------------------------------
# Backend implementations
# ---------------------------------------------------------------------------

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"


class _UsageShim:
    """Minimal Anthropic-SDK-compatible usage object built from the JSON
    body of a raw httpx response. The SessionBudget tracker reads
    ``input_tokens`` / ``output_tokens`` off this shape, matching the
    real SDK's ``response.usage`` attribute names.
    """

    __slots__ = ("input_tokens", "output_tokens")

    def __init__(self, raw: dict[str, Any]) -> None:
        self.input_tokens = int(raw.get("input_tokens", 0) or 0)
        self.output_tokens = int(raw.get("output_tokens", 0) or 0)


def _call_claude(
    prompt: str,
    model: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
    require_citations: bool,
    max_retries: int,
    on_usage: Callable[[Any], None] | None = None,
) -> dict[str, Any]:
    """Call the Anthropic Messages API with retry on schema violations.

    Parameters
    ----------
    on_usage : callable | None
        Optional callback invoked with a usage shim
        (``input_tokens`` / ``output_tokens`` attrs) after every
        successful API request that returned billable tokens. Used by
        the Session B.6 budget guardrail. None (the default) preserves
        the historical behavior — no callback fires.
    """
    headers = {
        "Content-Type": "application/json",
        "anthropic-version": ANTHROPIC_API_VERSION,
    }
    # API key is injected by the runtime environment (ANTHROPIC_API_KEY) —
    # the httpx client picks it up automatically via the SDK convention.
    # For direct httpx use we read it from the environment.
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        headers["x-api-key"] = api_key

    body = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": PLANNER_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt}],
    }

    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(1, max_retries + 1):
        try:
            resp = httpx.post(ANTHROPIC_MESSAGES_URL, headers=headers, json=body, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            # Record usage as soon as the HTTP layer succeeded; we want
            # to count tokens for billed calls even if downstream JSON
            # validation raises and forces a retry on the schema.
            if on_usage is not None:
                try:
                    on_usage(_UsageShim(data.get("usage", {}) or {}))
                except Exception as cb_exc:  # noqa: BLE001
                    # The on_usage callback may legitimately raise
                    # (e.g. SessionBudgetExceeded). Let those propagate
                    # so the loop driver can shut down cleanly.
                    raise cb_exc
            text = "".join(
                block.get("text", "")
                for block in data.get("content", [])
                if block.get("type") == "text"
            )
            validated = _validate_planner_json(text, require_citations)
            logger.info("Claude planner OK (attempt %d).", attempt)
            return {**validated, "_raw": text}
        except (httpx.HTTPError, ValueError) as exc:
            last_exc = exc
            logger.warning("Claude attempt %d/%d failed: %s", attempt, max_retries, exc)
            if attempt < max_retries:
                time.sleep(min(2 ** attempt, 8))

    raise RuntimeError(f"Claude API failed after {max_retries} attempts: {last_exc}") from last_exc


def _call_ollama(
    prompt: str,
    host: str,
    model: str,
    temperature: float,
    num_ctx: int,
    timeout: float,
    require_citations: bool,
    max_retries: int,
) -> dict[str, Any]:
    """Call a local Ollama instance (chat completion endpoint)."""
    url = f"{host.rstrip('/')}/api/chat"
    body = {
        "model": model,
        "stream": False,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
        "messages": [
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
    }

    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(1, max_retries + 1):
        try:
            resp = httpx.post(url, json=body, timeout=timeout)
            resp.raise_for_status()
            text = resp.json()["message"]["content"]
            validated = _validate_planner_json(text, require_citations)
            logger.info("Ollama planner OK (attempt %d).", attempt)
            return {**validated, "_raw": text}
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            last_exc = exc
            logger.warning("Ollama attempt %d/%d failed: %s", attempt, max_retries, exc)
            if attempt < max_retries:
                time.sleep(1)

    raise RuntimeError(f"Ollama failed after {max_retries} attempts: {last_exc}") from last_exc


# ---------------------------------------------------------------------------
# Offline (tier-0) deterministic fallback
# ---------------------------------------------------------------------------

_OFFLINE_PRIORITY: list[str] = [
    "device_specialist",
    "network_specialist",
    "cups_specialist",
    "windows_specialist",
    "validation_specialist",
]


def _offline_plan(state: Any) -> dict[str, Any]:
    """
    Rule-based specialist ordering when no LLM is available.
    Scores every worker specialist via can_handle() and picks the top two.
    """
    # Import here to avoid circular imports at module level
    try:
        from .agent.device_specialist import DeviceSpecialist
        from .agent.network_specialist import NetworkSpecialist
        from .agent.cups_specialist import CUPSSpecialist
        from .agent.windows_specialist import WindowsSpecialist

        workers = [
            DeviceSpecialist(),
            NetworkSpecialist(),
            CUPSSpecialist(),
            WindowsSpecialist(),
        ]
        scored = sorted(workers, key=lambda s: s.can_handle(state), reverse=True)
        ranked = [s.name for s in scored if s.can_handle(state) > 0.05]
    except ImportError:
        # Fallback priority list when specialists can't be imported
        ranked = _OFFLINE_PRIORITY[:]

    if not ranked:
        ranked = ["validation_specialist"]

    return {
        "ranked_specialists": ranked,
        "rationale": "offline tier — deterministic utility scoring",
        "citation_ids": [],
        "risk_level": "safe",
        "success_criteria_met": False,
        "escalate": False,
        "escalation_reason": "",
        "_raw": "",
    }


# ---------------------------------------------------------------------------
# Planner factory — the public API
# ---------------------------------------------------------------------------

PlannerFn = Callable[[Any, list[RagSnippet]], PlannerResponse]


def build_planner(cfg: Any, on_usage: Callable[[Any], None] | None = None) -> PlannerFn:
    """
    Factory: read runtime config, detect tier, return a ready-to-call planner fn.

    Parameters
    ----------
    cfg : Settings
        Loaded settings object (from zt411_agent.settings.Settings.load()).
    on_usage : callable | None
        Optional callback fired with a usage object
        (``input_tokens`` / ``output_tokens`` attrs) after every
        successful cloud-tier API call. Used by Session B.6's
        in-script budget guardrail. ``None`` (default) preserves the
        historical no-op behavior. Local (Ollama) and offline (tier0)
        paths never invoke the callback — they cost nothing to run.

    Returns
    -------
    PlannerFn
        Callable(state, snippets) → PlannerResponse
    """
    # Resolve config values with safe fallbacks
    force_tier: str = getattr(getattr(cfg, "runtime", None), "tier", "auto") or "auto"
    planner_backend: str = getattr(getattr(cfg, "llm", None), "planner_backend", "claude") or "claude"
    claude_model: str = getattr(getattr(cfg, "llm", None), "model", "claude-sonnet-4-6") or "claude-sonnet-4-6"
    claude_temp: float = float(getattr(getattr(cfg, "llm", None), "temperature", 0.0) or 0.0)
    claude_max_tokens: int = int(getattr(getattr(cfg, "llm", None), "max_tokens", 1024) or 1024)
    claude_timeout: float = float(getattr(getattr(cfg, "llm", None), "timeout", 30) or 30)
    require_citations: bool = bool(getattr(getattr(cfg, "llm", None), "require_citations", True))
    json_retries: int = int(getattr(getattr(getattr(cfg, "llm", None), "json_schema", None), "retries", 3) or 3)

    ollama_host: str = getattr(getattr(cfg, "ollama", None), "host", "http://localhost:11434") or "http://localhost:11434"
    ollama_model: str = getattr(getattr(cfg, "ollama", None), "model", "granite4") or "granite4"
    ollama_temp: float = float(getattr(getattr(cfg, "ollama", None), "temperature", 0.0) or 0.0)
    ollama_ctx: int = int(getattr(getattr(cfg, "ollama", None), "num_ctx", 8192) or 8192)

    # Detect runtime tier once at startup
    tier = detect_runtime_tier(force_tier=force_tier, ollama_host=ollama_host)

    # Override tier if config explicitly requests a specific backend
    if planner_backend == "ollama" and tier == RuntimeTier.CLOUD:
        logger.info("Config requests ollama backend; downgrading to tier1.")
        tier = RuntimeTier.LOCAL

    logger.info(
        "Planner configured: backend=%s tier=%s model=%s require_citations=%s",
        planner_backend, tier.value, claude_model if tier == RuntimeTier.CLOUD else ollama_model,
        require_citations,
    )

    def _planner(state: Any, snippets: list[RagSnippet] | None = None) -> PlannerResponse:
        nonlocal tier
        snippets = snippets or []

        # Sanitise snippets before they reach the prompt
        clean_snippets = [
            RagSnippet(
                snippet_id=s.snippet_id,
                source=s.source,
                section=s.section,
                text=_sanitise_snippet(s.text),
                score=s.score,
            )
            for s in snippets
        ]

        prompt = _build_planner_prompt(state, clean_snippets)

        raw: dict[str, Any] = {}
        used_tier = tier

        # --- Try cloud first, then local, then offline ---
        if tier == RuntimeTier.CLOUD:
            try:
                raw = _call_claude(
                    prompt=prompt,
                    model=claude_model,
                    temperature=claude_temp,
                    max_tokens=claude_max_tokens,
                    timeout=claude_timeout,
                    require_citations=require_citations,
                    max_retries=json_retries,
                    on_usage=on_usage,
                )
                used_tier = RuntimeTier.CLOUD
            except Exception as exc:
                logger.warning("Cloud planner failed, trying Ollama: %s", exc)
                tier = RuntimeTier.LOCAL   # downgrade for subsequent calls too

        if tier == RuntimeTier.LOCAL and not raw:
            try:
                raw = _call_ollama(
                    prompt=prompt,
                    host=ollama_host,
                    model=ollama_model,
                    temperature=ollama_temp,
                    num_ctx=ollama_ctx,
                    timeout=claude_timeout,
                    require_citations=require_citations,
                    max_retries=json_retries,
                )
                used_tier = RuntimeTier.LOCAL
            except Exception as exc:
                logger.warning("Ollama planner failed, using offline fallback: %s", exc)
                tier = RuntimeTier.OFFLINE

        if tier == RuntimeTier.OFFLINE or not raw:
            raw = _offline_plan(state)
            used_tier = RuntimeTier.OFFLINE

        return PlannerResponse(
            ranked_specialists=raw.get("ranked_specialists", []),
            rationale=raw.get("rationale", ""),
            citation_ids=raw.get("citation_ids", []),
            risk_level=raw.get("risk_level", "safe"),
            success_criteria_met=bool(raw.get("success_criteria_met", False)),
            escalate=bool(raw.get("escalate", False)),
            escalation_reason=raw.get("escalation_reason", ""),
            tier_used=used_tier,
            raw_response=raw.get("_raw", ""),
        )

    return _planner