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

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel

from src.zt411_agent.state import AgentState, ActionStatus, OSPlatform
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


@app.get("/health")
def health():
    store_type = type(_store).__name__
    return {
        "status": "ok",
        "service": "zt411-troubleshooter-agent",
        "runtime": "service",
        "tier": "auto",
        "store": store_type,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


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
            entry.status = ActionStatus.CONFIRMED
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
