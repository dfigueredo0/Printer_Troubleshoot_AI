"""
Session 4.2 — smoke tests for the SSE-streamed FastAPI service.

The streaming-events test is the main one; the other two are sanity
checks. The full PENDING -> CONFIRMED -> EXECUTED HTTP round-trip is
deliberately NOT exercised here — it is covered hermetically by
``test_calibrate_action_full_loop.py``, and adding an HTTP layer to that
test mostly tests HTTP plumbing rather than the agent. Keep the test
concerns separate.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# `service/` lives at the repo's project root, not inside `src/`. The
# `.pth` shipped with the venv only puts `src/` on sys.path, so resolve
# the project root and prepend it before the FastAPI app is imported.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from service.app import app, _SSE_SESSIONS, _infer_intent  # noqa: E402
from zt411_agent.agent.tools import ToolResult  # noqa: E402
from zt411_agent.state import LoopIntent  # noqa: E402
from tests.fixtures.replay import make_fixture_replay  # noqa: E402
import service.app as _app_mod  # noqa: E402


PRINTER_IP = "192.168.99.10"


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


@pytest.fixture(autouse=True)
def _reset_sse_sessions():
    _SSE_SESSIONS.clear()
    yield
    _SSE_SESSIONS.clear()


@pytest.fixture
def offline_service(monkeypatch):
    """Patch every external touchpoint the SSE service exercises during a
    diagnose run, force the planner to tier0, and stub the RAG pipeline.
    """
    replay = make_fixture_replay("zt411_fixture_idle_baseline.json")

    # SNMP / IPP / ZPL — replay
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

    # Stub the calibrate tool so the executed step in the calibrate path
    # does not try to open a real socket if the loop reaches it.
    monkeypatch.setattr(
        "zt411_agent.agent.tools.zpl_zt411_calibrate",
        lambda ip, port=9100: ToolResult(success=True, output={"sent_bytes": 3}),
    )
    monkeypatch.setattr(
        "zt411_agent.agent.device_specialist._ACTION_SETTLE_DELAY_S", 0.0
    )

    # Network probes — fully stubbed (also imported into network_specialist)
    monkeypatch.setattr("zt411_agent.agent.tools.ping", _stub_ping)
    monkeypatch.setattr("zt411_agent.agent.tools.tcp_connect", _stub_tcp_connect)
    monkeypatch.setattr("zt411_agent.agent.tools.dns_lookup", _stub_dns_lookup)
    monkeypatch.setattr("zt411_agent.agent.tools.arp_lookup", _stub_arp_lookup)
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

    # Force the planner's tier-detection probe to fail closed, just in case
    # someone runs the test with a live LLM env var set.
    monkeypatch.setattr("zt411_agent.planner._tcp_reachable", lambda *a, **k: False)

    # Stub the lazy RAG pipeline so we never load the embedding model.
    fake_rag = MagicMock()
    fake_rag.retrieve.return_value = []
    monkeypatch.setattr("service.app._get_rag_pipeline", lambda: fake_rag)

    return replay


# ---------------------------------------------------------------------------
# Intent inference (pure function — no fixtures needed)
# ---------------------------------------------------------------------------


class TestIntentInference:
    def test_blank_labels_routes_to_calibrate(self):
        assert _infer_intent("printer is printing blank labels") == LoopIntent.CALIBRATE

    def test_calibrate_keyword_routes_to_calibrate(self):
        assert _infer_intent("Run calibration please") == LoopIntent.CALIBRATE

    def test_unreachable_routes_to_network(self):
        assert _infer_intent("printer is unreachable") == LoopIntent.DIAGNOSE_NETWORK

    def test_ribbon_routes_to_consumables(self):
        assert _infer_intent("ribbon empty") == LoopIntent.DIAGNOSE_CONSUMABLES

    def test_quality_keyword(self):
        assert _infer_intent("output is faded") == LoopIntent.DIAGNOSE_PRINT_QUALITY

    def test_unrelated_falls_back_to_general(self):
        assert _infer_intent("hello world") == LoopIntent.GENERAL


# ---------------------------------------------------------------------------
# /confirm/{token}
# ---------------------------------------------------------------------------


def test_confirm_unknown_token_returns_404():
    client = TestClient(app)
    r = client.post("/confirm/nonexistent-token-id")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /diagnose SSE streaming
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /snippet/{snippet_id}
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_snippet_cache(monkeypatch):
    """Replace the on-disk snippet cache with an in-memory fixture so the
    test doesn't depend on data/rag_corpus/chunks.jsonl being built."""
    fake = {
        "test_snippet_001": {
            "chunk_id": "test_snippet_001",
            "source": "test_doc",
            "section": "calibration",
            "page": 42,
            "text": "Calibrate the printer by sending ~JC over TCP 9100.",
        }
    }
    monkeypatch.setattr(_app_mod, "_SNIPPET_CACHE", fake)
    monkeypatch.setattr(_app_mod, "_SNIPPET_CACHE_MTIME", 1.0)
    # Block the disk loader so the fake stays in place.
    monkeypatch.setattr(_app_mod, "_load_snippets_from_disk", lambda: None)


def test_snippet_known_id_returns_200(stub_snippet_cache):
    client = TestClient(app)
    r = client.get("/snippet/test_snippet_001")
    assert r.status_code == 200
    body = r.json()
    assert body["snippet_id"] == "test_snippet_001"
    assert "Calibrate" in body["text"]
    assert body["source"] == "test_doc"
    assert body["section"] == "calibration"


def test_snippet_unknown_id_returns_404(stub_snippet_cache):
    client = TestClient(app)
    r = client.get("/snippet/bogus_id_99999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# /diagnose-start
# ---------------------------------------------------------------------------


def test_diagnose_start_returns_html_fragment_with_sse_connect():
    client = TestClient(app)
    # /diagnose-start accepts form-encoded data so HTMX `hx-post`
    # form submissions land cleanly without a serialiser plugin.
    r = client.post(
        "/diagnose-start",
        data={"symptom": "blank labels", "printer_ip": PRINTER_IP},
    )
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    body = r.text
    assert 'hx-ext="sse"' in body
    assert 'sse-connect="/diagnose-stream/' in body
    assert "session-html" in body  # multiple event names in sse-swap
    assert "evidence-html" in body
    assert "action-html" in body
    assert "complete-html" in body
    # Session was created — extract id from response and verify it lives
    # in _SSE_SESSIONS.
    import re
    m = re.search(r"/diagnose-stream/([0-9a-f-]+)", body)
    assert m, f"could not extract session_id from response: {body[:200]}"
    assert m.group(1) in _SSE_SESSIONS


def test_diagnose_streams_events(offline_service):
    """POST /diagnose returns SSE events for a fixture-replayed loop.

    Phase 4.3: each logical event type is emitted twice in a row — once
    as ``{type}-json`` for programmatic clients and once as ``{type}-html``
    for HTMX. Phase 4.4: the calibrate path now suspends with `awaiting`
    instead of running to completion, so the terminator is `awaiting-html`
    (or `complete-html` if a future test scenario reaches that path).
    """
    client = TestClient(app)
    with client.stream(
        "POST",
        "/diagnose",
        json={
            "symptom": "printer is printing blank labels",
            "printer_ip": PRINTER_IP,
            "max_steps": 3,
            "force_tier": "tier0",
        },
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")

        event_lines: list[str] = []
        for line in resp.iter_lines():
            if line.startswith("event:"):
                event_lines.append(line)
            # Bail on either terminator — `awaiting` for the suspend path
            # (calibrate proposal awaiting confirm) and `complete` for any
            # path that runs to a terminal status.
            if line.startswith("event: awaiting-html") or line.startswith(
                "event: complete-html"
            ):
                break

    types = {line.split(":", 1)[1].strip() for line in event_lines}
    for event in ("session", "evidence", "action"):
        assert f"{event}-json" in types, (
            f"missing '{event}-json' event; got {types}"
        )
        assert f"{event}-html" in types, (
            f"missing '{event}-html' event; got {types}"
        )
    # Loop ends with either awaiting (4.4 suspend) or complete (terminal).
    assert (
        "awaiting-html" in types or "complete-html" in types
    ), f"expected terminator event (awaiting/complete); got {types}"
