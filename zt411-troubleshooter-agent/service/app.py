"""
service/app.py — FastAPI service for the ZT411 Troubleshooter Agent.

Endpoints
---------
GET  /health                           — Liveness + runtime mode
GET  /sessions                         — List sessions (admin)
POST /sessions                         — Create a new troubleshooting session
GET  /sessions/{session_id}            — Get current session state
DELETE /sessions/{session_id}          — Delete a session
POST /sessions/{session_id}/diagnose   — Run the agent loop
POST /sessions/{session_id}/confirm    — Consume a confirmation token
GET  /sessions/{session_id}/audit      — Full audit trail (action_log + evidence)
GET  /sessions/{session_id}/export     — Downloadable session JSON
POST /retrieve                         — RAG snippet retrieval (for UI citation panel)
GET  /metrics                          — Prometheus metrics text

Design notes
------------
* Sessions are persisted via SessionStore (strategy pattern).
  - USE_DB=true + DATABASE_URL  → PostgreSQL-backed store (production)
  - otherwise                   → in-memory MemorySessionStore (tests / offline)
* The agent loop runs synchronously on POST /diagnose. Offload to RQ/Celery in prod.
* Confirmation tokens are stored on AgentState and consumed once.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request, Form
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel

from src.zt411_agent.state import (
    AgentState,
    ActionStatus,
    LoopIntent,
    LoopStatus,
    OSPlatform,
)
from src.zt411_agent.logging_utils import configure_logging
from src.zt411_agent.db import get_session_store

configure_logging()
logger = logging.getLogger(__name__)

app = FastAPI(
    title="ZT411 Troubleshooter Agent",
    description="Evidence-grounded agentic troubleshooting for the Zebra ZT411.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static assets (CSS + vendored htmx). Mounted relative to the cwd so
# `make serve` (run from zt411-troubleshooter-agent/) finds them.
from pathlib import Path as _P  # noqa: E402

_STATIC_DIR = _P(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the single-page frontend."""
    index_path = _STATIC_DIR / "index.html"
    if not index_path.exists():
        return HTMLResponse(
            "<h1>Frontend not found</h1>"
            "<p>service/static/index.html missing.</p>",
            status_code=500,
        )
    return FileResponse(str(index_path))

# ---------------------------------------------------------------------------
# Session store (DB-backed when USE_DB=true, in-memory otherwise)
# ---------------------------------------------------------------------------

_store = get_session_store()

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

REQUESTS = Counter("requests_total", "Total HTTP requests", ["method", "endpoint", "status"])
DIAGNOSE_DURATION = Histogram(
    "diagnose_duration_seconds",
    "Time spent running the agent loop",
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60],
)
LOOP_STEPS = Histogram(
    "agent_loop_steps",
    "Number of loop iterations per session",
    buckets=[1, 2, 3, 5, 8, 10, 15],
)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    symptoms: list[str] = []
    os_platform: str = "unknown"
    device_ip: str = "unknown"
    user_description: str = ""


class DiagnoseRequest(BaseModel):
    force_tier: str = "auto"  # auto|tier0|tier1|tier2
    max_steps: int = 10


class ConfirmRequest(BaseModel):
    token: str


class RetrieveRequest(BaseModel):
    query: str
    top_k: int = 6


# ---------------------------------------------------------------------------
# Middleware — request counting
# ---------------------------------------------------------------------------


@app.middleware("http")
async def _count_requests(request: Request, call_next):
    start = time.monotonic()
    response = await call_next(request)
    REQUESTS.labels(
        method=request.method,
        endpoint=request.url.path,
        status=str(response.status_code),
    ).inc()
    return response


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health.json")
def health_json():
    """Programmatic health (JSON). Used by tests and external monitors."""
    store_type = type(_store).__name__
    return {
        "status": "ok",
        "service": "zt411-troubleshooter-agent",
        "runtime": "service",
        "tier": "auto",
        "store": store_type,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/health", response_class=HTMLResponse)
def health():
    """HTML health, for HTMX polling from the frontend status indicator."""
    return '<span class="healthy">&#x25CF; Connected</span>'


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@app.get("/sessions")
def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    return {"sessions": _store.list_sessions(limit=limit, offset=offset)}


@app.post("/sessions", status_code=201)
def create_session(req: CreateSessionRequest):
    _platform_map = {
        "windows": OSPlatform.WINDOWS,
        "linux": OSPlatform.LINUX,
        "macos": OSPlatform.MACOS,
    }
    platform = _platform_map.get(req.os_platform.lower(), OSPlatform.UNKNOWN)

    state = AgentState(
        os_platform=platform,
        symptoms=req.symptoms,
        user_description=req.user_description,
    )
    state.device.ip = req.device_ip

    _store.save(state)

    logger.info(
        "Session created",
        extra={
            "session_id": state.session_id,
            "os_platform": str(platform),
            "symptoms": req.symptoms,
        },
    )
    return {"session_id": state.session_id, "created_at": state.created_at.isoformat()}


@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    state = _get_or_404(session_id)
    return _state_to_dict(state)


@app.delete("/sessions/{session_id}", status_code=204)
def delete_session(session_id: str):
    if not _store.delete(session_id):
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")


# ---------------------------------------------------------------------------
# Diagnose — run the agent loop
# ---------------------------------------------------------------------------


@app.post("/sessions/{session_id}/diagnose")
def diagnose(session_id: str, req: DiagnoseRequest):
    state = _get_or_404(session_id)

    start = time.monotonic()
    try:
        state = _run_agent_loop(state, force_tier=req.force_tier, max_steps=req.max_steps)
    except Exception as exc:
        logger.exception("Agent loop error: %s", exc, extra={"session_id": session_id})
        raise HTTPException(status_code=500, detail=f"Agent loop error: {exc}") from exc
    finally:
        duration = time.monotonic() - start
        DIAGNOSE_DURATION.observe(duration)
        LOOP_STEPS.observe(state.loop_counter)

    _store.save(state)
    return {
        "session_id": session_id,
        "loop_status": state.loop_status.value,
        "loop_counter": state.loop_counter,
        "escalation_reason": state.escalation_reason,
        "is_resolved": state.is_resolved(),
    }


# ---------------------------------------------------------------------------
# Confirm action token
# ---------------------------------------------------------------------------


@app.post("/sessions/{session_id}/confirm")
def confirm_action(session_id: str, req: ConfirmRequest):
    state = _get_or_404(session_id)
    entry_id = state.consume_confirmation_token(req.token)
    if entry_id is None:
        raise HTTPException(status_code=404, detail="Token not found or already consumed.")

    for entry in state.action_log:
        if entry.entry_id == entry_id:
            state.update_action_status(entry.entry_id, ActionStatus.CONFIRMED)
            state.add_evidence(
                specialist="api",
                source="human_confirmation",
                content=f"Action '{entry.action}' confirmed via API token by operator.",
            )
            _store.save(state)
            logger.info(
                "Action confirmed",
                extra={"session_id": session_id, "entry_id": entry_id, "action": entry.action},
            )
            return {"confirmed": True, "entry_id": entry_id, "action": entry.action}

    raise HTTPException(status_code=404, detail="Action log entry not found for token.")


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


@app.get("/sessions/{session_id}/audit")
def get_audit(session_id: str):
    state = _get_or_404(session_id)
    return {
        "session_id": session_id,
        "action_log": [_action_to_dict(a) for a in state.action_log],
        "evidence": [_evidence_to_dict(e) for e in state.evidence],
        "snapshot_diffs": [_diff_to_dict(d) for d in state.snapshot_diffs],
        "loop_counter": state.loop_counter,
        "loop_status": state.loop_status.value,
    }


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


@app.get("/sessions/{session_id}/export")
def export_session(session_id: str):
    state = _get_or_404(session_id)
    return _state_to_dict(state)


# ---------------------------------------------------------------------------
# RAG retrieval
# ---------------------------------------------------------------------------


@app.post("/retrieve")
def retrieve(req: RetrieveRequest):
    try:
        rag = _get_rag_pipeline()
        snippets = rag.retrieve(req.query)
        return {
            "query": req.query,
            "snippets": [
                {
                    "snippet_id": s.snippet_id,
                    "source": s.source,
                    "section": s.section,
                    "text": s.text,
                    "score": s.score,
                }
                for s in snippets[: req.top_k]
            ],
        }
    except Exception as exc:
        logger.warning("RAG retrieval failed: %s", exc)
        return {"query": req.query, "snippets": [], "error": str(exc)}


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------


@app.get("/metrics", response_class=PlainTextResponse)
def metrics():
    return PlainTextResponse(
        generate_latest().decode("utf-8"),
        media_type=CONTENT_TYPE_LATEST,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_or_404(session_id: str) -> AgentState:
    state = _store.get(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
    return state


def _run_agent_loop(state: AgentState, force_tier: str = "auto", max_steps: int = 10) -> AgentState:
    from src.zt411_agent.agent.orchestrator import Orchestrator
    from src.zt411_agent.agent.device_specialist import DeviceSpecialist
    from src.zt411_agent.agent.network_specialist import NetworkSpecialist
    from src.zt411_agent.agent.cups_specialist import CUPSSpecialist
    from src.zt411_agent.agent.windows_specialist import WindowsSpecialist
    from src.zt411_agent.agent.validation_specialist import ValidationSpecialist

    specialists = [
        DeviceSpecialist(),
        NetworkSpecialist(),
        CUPSSpecialist(),
        WindowsSpecialist(),
        ValidationSpecialist(),
    ]

    cfg = _build_cfg(force_tier=force_tier)
    orch = Orchestrator(specialists=specialists, cfg=cfg, max_loop_steps=max_steps)

    snippets = []
    try:
        rag = _get_rag_pipeline()
        query = " ".join(state.symptoms) + " " + state.user_description
        snippets = rag.retrieve(query.strip())
    except Exception as exc:
        logger.debug("RAG unavailable during diagnose: %s", exc)

    return orch.run(state, rag_snippets=snippets)


def _build_cfg(force_tier: str = "auto"):
    try:
        import yaml
        from pathlib import Path

        base = yaml.safe_load(Path("configs/runtime/base.yaml").read_text())

        class _Obj:
            pass

        cfg = _Obj()

        r = _Obj()
        r.tier = force_tier if force_tier != "auto" else base.get("runtime", {}).get("tier", "auto")
        r.mode = base.get("runtime", {}).get("mode", "auto")
        cfg.runtime = r

        l = _Obj()
        llm_cfg = base.get("llm", {})
        l.planner_backend = llm_cfg.get("planner_backend", "claude")
        l.model = llm_cfg.get("model", "claude-sonnet-4-6")
        l.temperature = llm_cfg.get("temperature", 0.0)
        l.max_tokens = llm_cfg.get("max_tokens", 1024)
        l.timeout = llm_cfg.get("timeout", 30)
        l.require_citations = llm_cfg.get("require_citations", True)
        js = _Obj()
        js.retries = llm_cfg.get("json_schema", {}).get("retries", 3)
        l.json_schema = js
        cfg.llm = l

        o = _Obj()
        ollama_cfg = base.get("ollama", {})
        o.host = ollama_cfg.get("host", "http://localhost:11434")
        o.model = ollama_cfg.get("model", "granite4")
        o.temperature = ollama_cfg.get("temperature", 0.0)
        o.num_ctx = ollama_cfg.get("num_ctx", 8192)
        cfg.ollama = o

        return cfg
    except Exception as exc:
        logger.warning("Could not load base.yaml, using defaults: %s", exc)
        return _default_cfg(force_tier)


def _default_cfg(force_tier: str = "tier0"):
    class _Obj:
        pass

    cfg = _Obj()
    r = _Obj()
    r.tier = force_tier
    cfg.runtime = r
    l = _Obj()
    l.planner_backend = "claude"
    l.model = "claude-sonnet-4-6"
    l.temperature = 0.0
    l.max_tokens = 1024
    l.timeout = 30.0
    l.require_citations = False
    js = _Obj()
    js.retries = 2
    l.json_schema = js
    cfg.llm = l
    o = _Obj()
    o.host = "http://localhost:11434"
    o.model = "granite4"
    o.temperature = 0.0
    o.num_ctx = 8192
    cfg.ollama = o
    return cfg


_rag_pipeline = None


def _get_rag_pipeline():
    global _rag_pipeline
    if _rag_pipeline is None:
        from src.zt411_agent.agent.rag import RAGPipeline
        cfg = _build_cfg()
        _rag_pipeline = RAGPipeline.from_config(cfg)
    return _rag_pipeline


# ---------------------------------------------------------------------------
# RAG snippet lookup (Phase 4.3 — citation-expansion endpoint)
# ---------------------------------------------------------------------------

# Module-level cache keyed by chunks.jsonl mtime so the file is re-read
# only when it actually changes. The corpus is small enough to load
# entirely into memory; for demo scale this is the simplest design that
# answers /snippet/{id} in O(1) after the first call.
from pathlib import Path as _Path  # noqa: E402

_SNIPPET_CACHE: dict[str, dict[str, Any]] = {}
_SNIPPET_CACHE_MTIME: float = 0.0
_CHUNKS_PATH = _Path("data/rag_corpus/chunks.jsonl")


def _load_snippets_from_disk() -> None:
    """Populate _SNIPPET_CACHE from the chunks.jsonl. Called on demand."""
    global _SNIPPET_CACHE_MTIME
    if not _CHUNKS_PATH.exists():
        _SNIPPET_CACHE.clear()
        _SNIPPET_CACHE_MTIME = 0.0
        return
    mtime = _CHUNKS_PATH.stat().st_mtime
    if mtime == _SNIPPET_CACHE_MTIME and _SNIPPET_CACHE:
        return
    fresh: dict[str, dict[str, Any]] = {}
    with _CHUNKS_PATH.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            cid = row.get("chunk_id")
            if cid:
                fresh[cid] = row
    _SNIPPET_CACHE.clear()
    _SNIPPET_CACHE.update(fresh)
    _SNIPPET_CACHE_MTIME = mtime


@app.get("/snippet/{snippet_id}")
async def get_snippet(snippet_id: str):
    """Return the full RAG snippet text for a given snippet_id.

    Used by the frontend's citation-expansion UI. Reads from the same
    chunks.jsonl the Retriever uses, so any ID emitted as a citation
    during a diagnose run is resolvable here.
    """
    _load_snippets_from_disk()
    row = _SNIPPET_CACHE.get(snippet_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"snippet {snippet_id!r} not found"
        )
    return {
        "snippet_id": snippet_id,
        "text": row.get("text", ""),
        "source": row.get("source", ""),
        "section": row.get("section", ""),
        "page": row.get("page"),
    }


def _state_to_dict(state: AgentState) -> dict:
    return {
        "session_id": state.session_id,
        "created_at": state.created_at.isoformat(),
        "os_platform": state.os_platform.value,
        "symptoms": state.symptoms,
        "user_description": state.user_description,
        "loop_status": state.loop_status.value,
        "loop_counter": state.loop_counter,
        "escalation_reason": state.escalation_reason,
        "is_resolved": state.is_resolved(),
        "queue_drained": state.queue_drained,
        "test_print_ok": state.test_print_ok,
        "device_ready": state.device_ready,
        "visited_specialists": state.visited_specialists,
        "confirmation_tokens": state.confirmation_tokens,
        "action_log": [_action_to_dict(a) for a in state.action_log],
        "evidence": [_evidence_to_dict(e) for e in state.evidence],
        "snapshot_diffs": [_diff_to_dict(d) for d in state.snapshot_diffs],
        "device": state.device.model_dump(),
        "network": state.network.model_dump(),
        "cups": state.cups.model_dump(),
        "windows": state.windows.model_dump(),
    }


def _action_to_dict(a) -> dict:
    return {
        "entry_id": a.entry_id,
        "specialist": a.specialist,
        "action": a.action,
        "risk": a.risk.value,
        "status": a.status.value,
        "confirmation_token": a.confirmation_token,
        "result": a.result,
        "timestamp": a.timestamp.isoformat(),
    }


def _evidence_to_dict(e) -> dict:
    return {
        "evidence_id": e.evidence_id,
        "specialist": e.specialist,
        "source": e.source,
        "snippet_id": e.snippet_id,
        "content": e.content,
        "timestamp": e.timestamp.isoformat(),
    }


def _diff_to_dict(d) -> dict:
    return {
        "field": d.field,
        "before": d.before,
        "after": d.after,
        "confirmed_by": d.confirmed_by,
        "timestamp": d.timestamp.isoformat(),
    }


# ===========================================================================
# Session 4.2 — Server-Sent Events streaming endpoints
# ===========================================================================
#
# These endpoints sit alongside the per-session REST routes above. They
# exist because the SPA (Session 4.3) needs to watch evidence and
# action_log entries land in real time, not poll the audit endpoint after
# the loop returns.
#
# Session storage is intentionally a separate in-memory dict from the
# DB-backed `_store`. SSE sessions live for the lifetime of the FastAPI
# process and are NOT for production use — explicit demo scope.
# ---------------------------------------------------------------------------


@dataclass
class SseSession:
    """One streaming diagnose session.

    Holds the AgentState, an asyncio queue of pre-formatted SSE event
    strings, the loop's background task handle, and a `completed` flag
    the SSE generator polls so it can drain the queue and shut down.

    Phase 4.4 (loop pause/resume): the emission counters are per-session,
    not per-bridge-call, because the bridge re-enters on resume and must
    not re-emit history. `max_steps` and `force_tier` are stashed too so
    the resume path can re-launch the bridge with the same config.
    """
    state: AgentState
    event_queue: "asyncio.Queue[str]" = field(default_factory=asyncio.Queue)
    task: Optional[asyncio.Task] = None
    completed: bool = False
    last_evidence_emitted: int = 0
    last_action_status: dict[str, str] = field(default_factory=dict)
    max_steps: int = 10
    force_tier: str = "auto"
    # When True (HTMX flow via /diagnose-stream/{id}) the SSE generator
    # keeps the connection open across AWAITING_CONFIRMATION so resume
    # via /confirm/{token} can push more events. When False (legacy POST
    # /diagnose) the generator drains and closes once the bridge returns.
    keep_open_on_suspend: bool = True


_SSE_SESSIONS: dict[str, SseSession] = {}


def _format_sse(event_type: str, data: dict) -> str:
    """Format one SSE event. Trailing double newline terminates the event."""
    payload = json.dumps({"type": event_type, **data})
    return f"event: {event_type}\ndata: {payload}\n\n"


def _html_escape(s: Any) -> str:
    """Minimal HTML escape — agent content is never trusted as HTML."""
    s = str(s)
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


# SSE `data:` lines must not contain raw newlines — each newline starts a
# new SSE field. For HTML fragments we collapse whitespace to a single
# space so the entire fragment lives on one logical data line.
def _sse_safe_html(html: str) -> str:
    return " ".join(html.split())


def _format_sse_pair(event_type: str, data: dict, html: str) -> str:
    """Emit two SSE events back-to-back: a JSON form and an HTML form.

    Programmatic clients listen for ``{event_type}-json`` and parse
    ``data`` as JSON. The HTMX frontend listens for ``{event_type}-html``
    and HTMX swaps ``data`` directly into the DOM.
    """
    json_payload = json.dumps({"type": event_type, **data})
    safe_html = _sse_safe_html(html)
    return (
        f"event: {event_type}-json\n"
        f"data: {json_payload}\n\n"
        f"event: {event_type}-html\n"
        f"data: {safe_html}\n\n"
    )


def _render_evidence_html(evidence: dict) -> str:
    """Render an evidence_log entry as an HTML fragment for HTMX."""
    citation_html = ""
    snippet_id = evidence.get("snippet_id") or ""
    if snippet_id:
        # Use the snippet ID as both label and lookup key. The button
        # GETs /snippet/{id} on click and writes the JSON-rendered text
        # into the sibling .snippet-text container.
        short = _html_escape(snippet_id[:12])
        sid = _html_escape(snippet_id)
        citation_html = (
            f'<button class="citation" '
            f'hx-get="/snippet/{sid}" '
            f'hx-target="next .snippet-text" hx-swap="innerHTML">'
            f'[{short}]</button>'
            f'<div class="snippet-text"></div>'
        )
    return (
        f'<div class="evidence-row" '
        f'data-specialist="{_html_escape(evidence.get("specialist", ""))}">'
        f'<span class="specialist-tag">{_html_escape(evidence.get("specialist", ""))}</span>'
        f'<span class="source-tag">{_html_escape(evidence.get("source", ""))}</span>'
        f'<span class="content">{_html_escape(evidence.get("content", ""))}</span>'
        f'{citation_html}'
        f'</div>'
    )


def _render_action_html(action: dict) -> str:
    """Render an action_log entry. status drives color via CSS class.

    Always carries `hx-swap-oob="outerHTML"` so HTMX replaces the existing
    row in place by entry_id on every status change. The first time an
    entry's HTML lands the OOB target doesn't exist yet — HTMX falls
    through to the normal sse-swap append path. Subsequent renders match
    by id and update in place.
    """
    status = action.get("status", "")
    confirm_button = ""
    if status == "pending" and action.get("confirmation_token"):
        token = _html_escape(action["confirmation_token"])
        confirm_button = (
            f'<button class="confirm-btn" '
            f'hx-post="/confirm/{token}" '
            f'hx-swap="none">Confirm</button>'
        )
    return (
        f'<div class="action-row status-{_html_escape(status)}" '
        f'id="action-{_html_escape(action.get("entry_id", ""))}" '
        f'hx-swap-oob="outerHTML">'
        f'<span class="action-name">{_html_escape(action.get("action", ""))}</span>'
        f'<span class="risk-tag">{_html_escape(action.get("risk", ""))}</span>'
        f'<span class="status-tag">{_html_escape(status)}</span>'
        f'<span class="result">{_html_escape(action.get("result", ""))}</span>'
        f'{confirm_button}'
        f'</div>'
    )


def _render_complete_html(data: dict) -> str:
    outcome = _html_escape(data.get("outcome", "?"))
    n_evidence = data.get("evidence_count", 0)
    n_actions = data.get("action_count", 0)
    resolved = "resolved" if data.get("is_resolved") else "not resolved"
    return (
        f'<div class="loop-complete">'
        f'Loop complete — outcome: <strong>{outcome}</strong> '
        f'({resolved}; {n_evidence} evidence, {n_actions} actions)'
        f'</div>'
    )


def _render_session_html(data: dict) -> str:
    sid = _html_escape(data.get("session_id", ""))
    intent = _html_escape(data.get("intent", ""))
    return (
        f'<div class="session-banner">'
        f'Session <code>{sid[:8]}</code> — intent: <strong>{intent}</strong>'
        f'</div>'
    )


def _render_error_html(data: dict) -> str:
    return (
        f'<div class="loop-error">Error: '
        f'{_html_escape(data.get("message", "unknown"))}</div>'
    )


def _render_awaiting_html(data: dict) -> str:
    """Rendered when the orchestrator yields with AWAITING_CONFIRMATION.
    Acts as the visual "we paused here, click Confirm above" banner."""
    msg = _html_escape(data.get("message", "awaiting user confirmation"))
    return f'<div class="loop-awaiting">⏸ {msg}</div>'


# Keyword-based intent inference. This is deliberately simple — the
# demo's "blank labels" symptom deterministically routes to CALIBRATE,
# and the GENERAL fallback runs everything (correct, just slower) for
# any symptom we don't recognise.
_INTENT_KEYWORDS: dict[LoopIntent, list[str]] = {
    LoopIntent.CALIBRATE: [
        "blank label", "blank labels", "unprinted", "missing print",
        "calibrate", "calibration",
    ],
    LoopIntent.DIAGNOSE_CONSUMABLES: ["ribbon", "media", "out of"],
    LoopIntent.DIAGNOSE_NETWORK: [
        "unreachable", "offline", "can't connect", "cant connect", "timeout",
    ],
    LoopIntent.DIAGNOSE_PRINT_QUALITY: ["faded", "blurry", "smudge", "quality"],
}


def _infer_intent(symptom: str) -> LoopIntent:
    """TODO(phase5): replace with a real classifier. Substring matching
    is sufficient for the demo — the calibrate path triggers on a
    deterministic phrase, and unrecognised symptoms fall back to GENERAL
    which still runs every specialist (just slower)."""
    s = (symptom or "").lower()
    for intent, keywords in _INTENT_KEYWORDS.items():
        if any(k in s for k in keywords):
            return intent
    return LoopIntent.GENERAL


class StreamDiagnoseRequest(BaseModel):
    symptom: str
    printer_ip: str = "192.168.99.10"  # demo default — lab printer
    max_steps: int = 10
    force_tier: str = "auto"


def _create_sse_session(symptom: str, printer_ip: str) -> tuple[str, SseSession]:
    """Create an SSE session and register it in the in-memory store."""
    session_id = str(uuid.uuid4())
    state = AgentState(
        os_platform=OSPlatform.UNKNOWN,
        symptoms=[symptom],
        user_description=symptom,
        loop_intent=_infer_intent(symptom),
    )
    state.device.ip = printer_ip
    session = SseSession(state=state)
    _SSE_SESSIONS[session_id] = session
    return session_id, session


class DiagnoseStartRequest(BaseModel):
    symptom: str
    printer_ip: str = "192.168.99.10"


@app.post("/diagnose-start", response_class=HTMLResponse)
async def diagnose_start(
    symptom: str = Form(...),
    printer_ip: str = Form("192.168.99.10"),
):
    """Create a session and return an HTML fragment that opens the SSE
    stream. The diagnose loop itself runs inside /diagnose-stream/{id}.
    HTMX form submissions expect HTML responses, not open SSE streams.
    Splitting into "create + return wiring" and "stream events" lets a
    one-shot form post both create the session and embed the live
    stream connector that does the actual streaming.
    """
    session_id, session = _create_sse_session(symptom, printer_ip)
    intent = session.state.loop_intent.value
    # `sse-swap` accepts a comma-separated list of event names. The
    # leading element is the JSON-formatted complete event so HTMX swaps
    # the html-formatted ones into the live feed; OOB swaps inside the
    # html fragments handle action-row replacement by entry_id.
    html = (
        f'<div hx-ext="sse" '
        f'sse-connect="/diagnose-stream/{session_id}" '
        f'sse-swap="session-html,evidence-html,action-html,awaiting-html,complete-html,error-html" '
        f'hx-swap="beforeend">'
        f'  <div class="session-header">Session <code>{session_id[:8]}</code> '
        f'(intent: <strong>{_html_escape(intent)}</strong>)</div>'
        f'  <div class="event-feed"></div>'
        f'</div>'
    )
    return HTMLResponse(html)


@app.post("/diagnose")
async def diagnose_post(req: StreamDiagnoseRequest):
    """Backwards-compat one-shot SSE endpoint.

    Phase 4.2 clients (and the existing smoke tests) POST here directly
    and consume the stream from the same response. Phase 4.3 added the
    two-step /diagnose-start + /diagnose-stream/{id} flow for HTMX. Both
    paths share the same session machinery; this one just creates a
    session inline and pipes its stream back.

    `keep_open_on_suspend=False` because there is no resume path through
    this endpoint — once the loop suspends with AWAITING_CONFIRMATION the
    stream closes cleanly. The HTMX flow uses /diagnose-stream/{id} which
    DOES keep the stream open so /confirm/{token} can resume.
    """
    session_id, session = _create_sse_session(req.symptom, req.printer_ip)
    return _stream_session_response(
        session_id, session,
        max_steps=req.max_steps, force_tier=req.force_tier,
        keep_open_on_suspend=False,
    )


@app.get("/diagnose-stream/{session_id}")
async def diagnose_stream_get(session_id: str):
    """Open the SSE stream for a session previously created via
    /diagnose-start. Idempotent re-entry would technically work but is
    not the intended flow — one connection per session.
    """
    session = _SSE_SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(404, f"session {session_id} not found")
    if session.task is not None:
        # Already streaming. Returning a 409 keeps the protocol honest
        # about "one connection per session" without requiring the
        # caller to track connection state.
        raise HTTPException(409, "session already streaming")
    return _stream_session_response(
        session_id, session,
        max_steps=10, force_tier="auto",
        keep_open_on_suspend=True,
    )


def _stream_session_response(
    session_id: str,
    session: SseSession,
    *,
    max_steps: int,
    force_tier: str,
    keep_open_on_suspend: bool = True,
) -> StreamingResponse:
    """Start the agent loop in a worker thread and return an SSE
    StreamingResponse that drains the session's event_queue.
    """
    session.keep_open_on_suspend = keep_open_on_suspend
    loop = asyncio.get_event_loop()
    session.task = asyncio.create_task(
        _run_loop_with_events(
            session, max_steps=max_steps, force_tier=force_tier, loop=loop,
        )
    )

    async def event_generator():
        # First event hands the session_id back so the client can POST
        # to /confirm/{token} once it sees a token in an action event.
        session_data = {
            "session_id": session_id,
            "intent": session.state.loop_intent.value,
        }
        yield _format_sse_pair(
            "session", session_data, _render_session_html(session_data),
        )
        while True:
            try:
                event = await asyncio.wait_for(
                    session.event_queue.get(), timeout=2.0
                )
                yield event
                if session.completed and session.event_queue.empty():
                    break
            except asyncio.TimeoutError:
                # The bridge has gone idle. Three cases:
                # 1. Loop running, just slow — emit keepalive and continue.
                # 2. Loop terminal (completed=True) — flag drives normal exit.
                # 3. Loop suspended (AWAITING_CONFIRMATION). For the HTMX
                #    flow we keep waiting; for the legacy /diagnose POST
                #    (keep_open_on_suspend=False) we drain and close.
                if (
                    not session.keep_open_on_suspend
                    and session.task is not None
                    and session.task.done()
                    and session.event_queue.empty()
                    and not session.completed
                ):
                    break
                yield ": keepalive\n\n"

    return StreamingResponse(
        event_generator(), media_type="text/event-stream"
    )


async def _run_loop_with_events(
    session: SseSession,
    max_steps: int,
    force_tier: str,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Bridge between the synchronous agent loop and the asyncio queue.

    The orchestrator runs in a thread; this coroutine polls state for
    new evidence + new and *updated* action_log entries every 200ms and
    emits SSE events for each. Per-entry status snapshots let us detect
    in-place mutations (Phase 4.3 update_action_status) — without that,
    the EXECUTED → VERIFYING → RESOLVED transitions would be invisible
    to the frontend.

    Phase 4.4: the bridge can be re-invoked on the same session after a
    confirmation. Emission counters live on the session so the resume
    pass does not re-emit already-streamed history. When the orchestrator
    yields with AWAITING_CONFIRMATION the bridge emits an `awaiting`
    event and returns *without* setting `session.completed` — the SSE
    generator stays alive on the queue's keepalive until the resume
    path pushes more events.
    """
    state = session.state
    # Stash the args so a resume path can re-launch with the same config.
    session.max_steps = max_steps
    session.force_tier = force_tier

    def emit_pair(event_type: str, data: dict, html: str) -> None:
        # asyncio.Queue.put_nowait is not thread-safe; use call_soon_threadsafe
        # so the worker thread can hand events to the asyncio loop.
        loop.call_soon_threadsafe(
            session.event_queue.put_nowait,
            _format_sse_pair(event_type, data, html),
        )

    def flush_new_state() -> None:
        for ev in state.evidence[session.last_evidence_emitted:]:
            d = _evidence_to_dict(ev)
            emit_pair("evidence", d, _render_evidence_html(d))
        session.last_evidence_emitted = len(state.evidence)

        # Emit on every new entry AND on every status change of an
        # existing entry. The per-entry_id status snapshot survives
        # across resumes — already-emitted statuses don't re-fire.
        for action in state.action_log:
            current_status = (
                action.status.value
                if hasattr(action.status, "value")
                else str(action.status)
            )
            previous = session.last_action_status.get(action.entry_id)
            if previous == current_status:
                continue
            d = _action_to_dict(action)
            emit_pair("action", d, _render_action_html(d))
            session.last_action_status[action.entry_id] = current_status

    # Kick the orchestrator off in a worker thread.
    loop_future = asyncio.to_thread(
        _run_agent_loop, state, force_tier, max_steps,
    )
    loop_task = asyncio.ensure_future(loop_future)

    # Poll for state changes while the loop runs.
    try:
        while not loop_task.done():
            flush_new_state()
            await asyncio.sleep(0.2)
        flush_new_state()
        await loop_task  # surface any exception
    except Exception as exc:  # noqa: BLE001
        logger.exception("SSE loop error: %s", exc)
        emit_pair("error", {"message": str(exc)}, _render_error_html({"message": str(exc)}))
        # Treat unexpected exceptions as terminal — fall through to the
        # complete path so the SSE generator can drain and close.
        session.completed = True
        complete_data = {
            "outcome": "error",
            "evidence_count": len(state.evidence),
            "action_count": len(state.action_log),
            "is_resolved": False,
        }
        emit_pair("complete", complete_data, _render_complete_html(complete_data))
        return

    # Loop returned cleanly — was it a suspension or terminal?
    if state.loop_status == LoopStatus.AWAITING_CONFIRMATION:
        awaiting_data = {
            "outcome": state.loop_status.value,
            "message": "awaiting user confirmation",
        }
        emit_pair("awaiting", awaiting_data, _render_awaiting_html(awaiting_data))
        # Do NOT set session.completed — the SSE generator stays open on
        # keepalive frames until the /confirm/{token} endpoint relaunches
        # the bridge and resumes pushing events.
        return

    # Terminal outcome (SUCCESS, ESCALATED, MAX_STEPS, ...). Mark the
    # session done and emit the complete event.
    session.completed = True
    outcome = (
        state.loop_status.value
        if hasattr(state.loop_status, "value")
        else str(state.loop_status)
    )
    complete_data = {
        "outcome": outcome,
        "evidence_count": len(state.evidence),
        "action_count": len(state.action_log),
        "is_resolved": state.is_resolved(),
    }
    emit_pair("complete", complete_data, _render_complete_html(complete_data))


@app.post("/confirm/{token}")
async def confirm_token_stream(token: str):
    """Consume a confirmation token, flip its action to CONFIRMED.

    The agent loop polls for CONFIRMED action_log entries on its next
    iteration (per Session 4.1 Step 4) and executes them — there is no
    direct response stream from this endpoint. EXECUTED events flow
    through the existing /diagnose connection.

    Edge cases:
        404 — token unknown
        400 — token already consumed (or its action is no longer PENDING)
        410 — session already completed; the loop is no longer watching
    """
    for session_id, session in _SSE_SESSIONS.items():
        if token not in session.state.confirmation_tokens:
            continue
        if session.completed:
            raise HTTPException(
                status_code=410,
                detail="session already completed; no longer accepting confirmations",
            )

        entry_id = session.state.consume_confirmation_token(token)
        if entry_id is None:
            raise HTTPException(
                status_code=400,
                detail="token already consumed or invalid",
            )

        for entry in session.state.action_log:
            if entry.entry_id != entry_id:
                continue
            if entry.status != ActionStatus.PENDING:
                # Token was valid but the entry has moved on — surface
                # the conflict rather than silently re-confirming.
                raise HTTPException(
                    status_code=400,
                    detail=f"action no longer pending (status={entry.status.value})",
                )
            session.state.update_action_status(
                entry.entry_id, ActionStatus.CONFIRMED
            )
            session.state.add_evidence(
                specialist="api",
                source="human_confirmation",
                content=f"Action '{entry.action}' confirmed via SSE token by operator.",
            )

            # Resume the orchestrator. Phase 4.4: the loop suspended with
            # loop_status=AWAITING_CONFIRMATION; flipping it back to RUNNING
            # makes the orchestrator's while-predicate true again, and a
            # fresh bridge task pushes the resumed events down the same
            # SSE stream the user is watching. Per-session emission counters
            # keep the stream from re-emitting history.
            session.state.loop_status = LoopStatus.RUNNING
            loop = asyncio.get_event_loop()
            session.task = asyncio.create_task(
                _run_loop_with_events(
                    session,
                    max_steps=session.max_steps,
                    force_tier=session.force_tier,
                    loop=loop,
                )
            )
            return {
                "status": "approved",
                "action": entry.action,
                "session_id": session_id,
                "entry_id": entry_id,
                "resumed": True,
            }
        # Should not happen — confirmation_tokens stores entry_ids that
        # exist in action_log. If we get here, state is corrupt.
        raise HTTPException(
            status_code=500,
            detail=f"internal: entry {entry_id} missing from action_log",
        )

    raise HTTPException(status_code=404, detail="token not found")
