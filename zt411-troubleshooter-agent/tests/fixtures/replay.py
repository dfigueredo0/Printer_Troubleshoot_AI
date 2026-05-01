"""
Fixture replay helpers for offline SNMP/IPP testing.

Reads captured fixtures at tests/fixtures/snmp_walks/zt411_fixture_*.json
and exposes drop-in replacements for snmp_get / snmp_walk /
ipp_get_attributes that match the real signatures in
src/zt411_agent/agent/tools.py.

Each fixture contains a full IPP attribute dump plus SNMP walks of
10642.1.*, 10642.2.*, 10642.6.*, and 10642.10.* at a known printer
state. Walks are merged into a flat OID -> value map at load time so
gets and prefix walks can be served from the same dict.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict

# Re-use the production ToolResult so fixtures are signature-compatible.
from zt411_agent.agent.tools import ToolResult


_WALK_KEY_PREFIX = "snmp_walk_"


def _load_fixture(fixture_path: str | Path) -> Dict[str, Any]:
    path = Path(fixture_path)
    if not path.is_absolute():
        # Resolve relative to this file so tests can pass a bare filename.
        candidate = Path(__file__).parent / "snmp_walks" / path.name
        if candidate.exists():
            path = candidate
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _flatten_walks(fixture: Dict[str, Any]) -> Dict[str, Any]:
    """Merge all `snmp_walk_*` sections into a single OID->value dict."""
    flat: Dict[str, Any] = {}
    for key, section in fixture.items():
        if not key.startswith(_WALK_KEY_PREFIX):
            continue
        rows = (section or {}).get("rows", [])
        for row in rows:
            oid = row.get("oid")
            if oid is None:
                continue
            flat[str(oid)] = row.get("value")
    return flat


def replay_snmp_get(fixture_path: str | Path) -> Callable[..., ToolResult]:
    """Build a snmp_get replacement bound to one fixture's data.

    The returned callable matches the real signature
        snmp_get(ip, oid, community="public", timeout_s=5, port=161)
    and serves values from the fixture's flattened OID map. OIDs not
    present in the fixture (e.g. the standard SNMPv2-MIB system group,
    which is not captured) yield ToolResult(success=False, ...).
    """
    fixture = _load_fixture(fixture_path)
    flat = _flatten_walks(fixture)

    def _get(
        ip: str,
        oid: str,
        community: str = "public",
        timeout_s: int = 5,
        port: int = 161,
    ) -> ToolResult:
        if oid in flat:
            return ToolResult(success=True, output={"value": flat[oid]})
        return ToolResult(
            success=False,
            output=None,
            error=f"oid {oid} not present in fixture",
        )

    return _get


def replay_snmp_walk(fixture_path: str | Path) -> Callable[..., ToolResult]:
    """Build a snmp_walk replacement bound to one fixture's data.

    Matches the real signature
        snmp_walk(ip, oid_prefix, community="public", timeout_s=10,
                  port=161, max_rows=50)
    and returns every fixture row whose OID starts with `oid_prefix`,
    matching only on dotted-label boundaries (so a request for
    "1.3.6.1.4.1.10642.10.31.1" never accidentally matches
    "1.3.6.1.4.1.10642.10.310.*").
    """
    fixture = _load_fixture(fixture_path)
    flat = _flatten_walks(fixture)

    def _walk(
        ip: str,
        oid_prefix: str,
        community: str = "public",
        timeout_s: int = 10,
        port: int = 161,
        max_rows: int = 50,
    ) -> ToolResult:
        prefix = oid_prefix.rstrip(".")
        rows = []
        for oid in sorted(flat.keys(), key=_oid_sort_key):
            if oid == prefix or oid.startswith(prefix + "."):
                rows.append({"oid": oid, "value": flat[oid]})
                if len(rows) >= max_rows:
                    break
        return ToolResult(success=True, output={"rows": rows})

    return _walk


def replay_ipp_get_attributes(fixture_path: str | Path) -> Callable[..., ToolResult]:
    """Build an ipp_get_attributes replacement bound to one fixture's data.

    Matches the real signature
        ipp_get_attributes(ip, port=631)
    and reproduces the production ToolResult shape:
        ToolResult(success=True,
                   output={"attributes": {name: value, ...}},
                   raw=repr(attrs))
    """
    fixture = _load_fixture(fixture_path)
    attrs: Dict[str, str] = (
        (fixture.get("ipp") or {}).get("attributes") or {}
    )

    def _ipp(ip: str, port: int = 631) -> ToolResult:
        return ToolResult(
            success=True,
            output={"attributes": dict(attrs)},
            raw=repr(attrs),
        )

    return _ipp


# ---------------------------------------------------------------------------
# ZPL ~HS replay (Phase 2.5+ — replaces snmp_zt411_physical_flags as the
# device-specialist read path on hardware where SNMP is unreachable). One
# canned response per known fixture state, keyed off the fixture stem.
# Field semantics per Zebra ZPL Programming Guide §~HS:
#   line 1: comm_iface, paper_out_flag, pause_flag, label_length_dots, ...
#   line 2: function_settings, _, head_up_flag, ribbon_out_flag, ...
# ---------------------------------------------------------------------------

_HS_BY_STATE: Dict[str, str] = {
    # idle / no-fault baseline
    "idle_baseline":  "030,0,0,0308,000,0,0,0,000,0,0,0\n"
                      "001,0,0,0,1,2,6,0,00000000,1,000\n0000,0",
    "post_test_idle": "030,0,0,0308,000,0,0,0,000,0,0,0\n"
                      "001,0,0,0,1,2,6,0,00000000,1,000\n0000,0",
    # user-pressed pause, no other faults
    "paused":         "030,0,1,0308,000,0,0,0,000,0,0,0\n"
                      "001,0,0,0,1,2,6,0,00000000,1,000\n0000,0",
    # auto-pause from a physical fault (paused=True AND fault flag)
    "head_open":      "030,0,1,0308,000,0,0,0,000,0,0,0\n"
                      "001,0,1,0,1,2,6,0,00000000,1,000\n0000,0",
    "media_out":      "030,1,1,0308,000,0,0,0,000,0,0,0\n"
                      "001,0,0,0,1,2,6,0,00000000,1,000\n0000,0",
    "ribbon_out":     "030,0,1,0308,000,0,0,0,000,0,0,0\n"
                      "001,0,0,1,1,2,6,0,00000000,1,000\n0000,0",
}


def _state_stem(fixture_path: str | Path) -> str:
    """Map fixture filename to its state key in _HS_BY_STATE.

    Strips the standard `zt411_fixture_` prefix and `.json` suffix so
    a fixture path like `zt411_fixture_head_open.json` resolves to
    `head_open`.
    """
    name = Path(fixture_path).stem
    if name.startswith("zt411_fixture_"):
        name = name[len("zt411_fixture_"):]
    return name


def replay_zpl_zt411_host_status(
    fixture_path: str | Path,
) -> Callable[..., ToolResult]:
    """Build a zpl_zt411_host_status replacement bound to one fixture's state.

    Looks up a canned `~HS` response for the fixture's state stem,
    parses it through the production parser, and returns the resulting
    ToolResult so consumers see the exact same dict shape they would in
    production. Unknown states return success=False with a clear error.
    """
    stem = _state_stem(fixture_path)
    response = _HS_BY_STATE.get(stem)

    # Defer the parser import to call time — keeps the test module's
    # import graph lean and avoids a circular if production code ever
    # starts importing test fixtures by accident.
    from zt411_agent.agent.tools import _parse_host_status

    def _host_status(ip: str, port: int = 9100) -> ToolResult:
        if response is None:
            return ToolResult(
                success=False,
                error=f"no canned ~HS response for fixture state {stem!r}",
            )
        try:
            flags = _parse_host_status(response)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc), raw=response)
        return ToolResult(success=True, output=flags, raw=response)

    return _host_status


# ---------------------------------------------------------------------------
# ZPL ~HI replay (Phase 4.2 — replaces snmp_zt411_status as the
# device-specialist identity read on hardware where SNMP is unreachable).
# Identity does not change with printer state, so a single canned
# response covers every fixture stem. Format per Zebra ZPL Programming
# Guide §~HI (lab-tested 2026-04-30 against firmware V92.21.39Z):
#     ZT411-200dpi,V92.21.39Z,8,8176KB
# ---------------------------------------------------------------------------

_HI_RESPONSE: str = "ZT411-200dpi,V92.21.39Z,8,8176KB"


def replay_zpl_zt411_host_identification(
    fixture_path: str | Path,  # noqa: ARG001 — accepted for API symmetry
) -> Callable[..., ToolResult]:
    """Build a zpl_zt411_host_identification replacement.

    The same identity response is returned for every fixture state
    (printer model + firmware are state-independent). Parses through
    the production parser logic so consumers see the exact production
    dict shape.
    """
    def _host_id(ip: str, port: int = 9100) -> ToolResult:
        parts = [p.strip() for p in _HI_RESPONSE.split(",")]
        if len(parts) < 4:
            return ToolResult(
                success=False,
                error=f"~HI returned {len(parts)} fields, expected 4",
                raw=_HI_RESPONSE,
            )
        try:
            memory_kb = int(parts[3].rstrip("KB").rstrip("kb"))
        except ValueError:
            memory_kb = -1
        return ToolResult(
            success=True,
            output={
                "model": parts[0],
                "firmware": parts[1],
                "memory_option": parts[2],
                "memory_kb": memory_kb,
                "raw_response": _HI_RESPONSE,
            },
            raw=_HI_RESPONSE,
        )

    return _host_id


# ---------------------------------------------------------------------------
# ZPL ~HQES replay (Phase 4.2 — replaces snmp_zt411_alerts on hardware
# where SNMP is unreachable). One canned response per fixture state.
# Lab-tested format (firmware V92.21.39Z, 2026-04-30):
#       PRINTER STATUS
#        ERRORS:         0 00000000 00000000
#        WARNINGS:       0 00000000 00000000
# Faults map to errors_count=1 with a non-zero bitmask byte. Bitmask
# decoding is deferred (phase 5); the count is what the demo uses.
# ---------------------------------------------------------------------------

_HQES_BY_STATE: Dict[str, str] = {
    # Healthy / no-fault: zero counts, zero bitmasks.
    "idle_baseline":
        "  PRINTER STATUS\n   ERRORS:         0 00000000 00000000\n"
        "   WARNINGS:       0 00000000 00000000\n",
    "post_test_idle":
        "  PRINTER STATUS\n   ERRORS:         0 00000000 00000000\n"
        "   WARNINGS:       0 00000000 00000000\n",
    # User-pressed pause: pause is not an "error" or "warning" per
    # ~HQES on this firmware — the counts stay zero. (~HS surfaces it.)
    "paused":
        "  PRINTER STATUS\n   ERRORS:         0 00000000 00000000\n"
        "   WARNINGS:       0 00000000 00000000\n",
    # Physical faults — errors_count=1, plausible non-zero bitmask byte.
    # The exact bitmask bit position is firmware-defined and decoded in
    # phase 5; for the demo, the count is what matters.
    "head_open":
        "  PRINTER STATUS\n   ERRORS:         1 00000004 00000000\n"
        "   WARNINGS:       0 00000000 00000000\n",
    "media_out":
        "  PRINTER STATUS\n   ERRORS:         1 00000001 00000000\n"
        "   WARNINGS:       0 00000000 00000000\n",
    "ribbon_out":
        "  PRINTER STATUS\n   ERRORS:         1 00000002 00000000\n"
        "   WARNINGS:       0 00000000 00000000\n",
}


def replay_zpl_zt411_extended_status(
    fixture_path: str | Path,
) -> Callable[..., ToolResult]:
    """Build a zpl_zt411_extended_status replacement bound to one fixture's state.

    Returns the same dict shape as the production parser. Unknown
    states fall back to "healthy" (zero counts) — this keeps tests that
    add fixtures later from breaking before someone fills in the
    canonical response.
    """
    stem = _state_stem(fixture_path)
    response = _HQES_BY_STATE.get(stem) or _HQES_BY_STATE["idle_baseline"]

    def _ext_status(ip: str, port: int = 9100) -> ToolResult:
        lines = response.strip().splitlines()
        out: Dict[str, Any] = {
            "errors_count": -1,
            "warnings_count": -1,
            "errors_bitmask_1": "",
            "errors_bitmask_2": "",
            "warnings_bitmask_1": "",
            "warnings_bitmask_2": "",
            "raw_response": response,
        }
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("ERRORS:"):
                tokens = stripped.replace("ERRORS:", "").split()
                if len(tokens) >= 3:
                    try:
                        out["errors_count"] = int(tokens[0])
                    except ValueError:
                        pass
                    out["errors_bitmask_1"] = tokens[1]
                    out["errors_bitmask_2"] = tokens[2]
            elif stripped.startswith("WARNINGS:"):
                tokens = stripped.replace("WARNINGS:", "").split()
                if len(tokens) >= 3:
                    try:
                        out["warnings_count"] = int(tokens[0])
                    except ValueError:
                        pass
                    out["warnings_bitmask_1"] = tokens[1]
                    out["warnings_bitmask_2"] = tokens[2]
        if out["errors_count"] == -1 and out["warnings_count"] == -1:
            return ToolResult(
                success=False,
                error="~HQES response did not contain ERRORS: or WARNINGS: lines",
                raw=response,
            )
        return ToolResult(success=True, output=out, raw=response)

    return _ext_status


def make_fixture_replay(fixture_path: str | Path) -> Dict[str, Callable[..., ToolResult]]:
    """Bundle all replay callables for a single fixture.

    Returns a dict ready to drive monkeypatch:

        replay = make_fixture_replay("zt411_fixture_paused.json")
        monkeypatch.setattr("zt411_agent.agent.tools.snmp_get",
                            replay["snmp_get"])
        monkeypatch.setattr("zt411_agent.agent.tools.snmp_walk",
                            replay["snmp_walk"])
        monkeypatch.setattr("zt411_agent.agent.tools.ipp_get_attributes",
                            replay["ipp_get_attributes"])
        # Phase 2.5: device_specialist now reads physical flags via ZPL,
        # so also patch zpl_zt411_host_status in both namespaces (the
        # function is imported into device_specialist's namespace).
        monkeypatch.setattr("zt411_agent.agent.tools.zpl_zt411_host_status",
                            replay["zpl_zt411_host_status"])
        monkeypatch.setattr(
            "zt411_agent.agent.device_specialist.zpl_zt411_host_status",
            replay["zpl_zt411_host_status"],
        )
        # Phase 4.2: identity + alerts moved to ZPL ~HI / ~HQES — same
        # dual-namespace patching pattern.
        monkeypatch.setattr("zt411_agent.agent.tools.zpl_zt411_host_identification",
                            replay["zpl_zt411_host_identification"])
        monkeypatch.setattr(
            "zt411_agent.agent.device_specialist.zpl_zt411_host_identification",
            replay["zpl_zt411_host_identification"],
        )
        monkeypatch.setattr("zt411_agent.agent.tools.zpl_zt411_extended_status",
                            replay["zpl_zt411_extended_status"])
        monkeypatch.setattr(
            "zt411_agent.agent.device_specialist.zpl_zt411_extended_status",
            replay["zpl_zt411_extended_status"],
        )
    """
    return {
        "snmp_get": replay_snmp_get(fixture_path),
        "snmp_walk": replay_snmp_walk(fixture_path),
        "ipp_get_attributes": replay_ipp_get_attributes(fixture_path),
        "zpl_zt411_host_status": replay_zpl_zt411_host_status(fixture_path),
        "zpl_zt411_host_identification":
            replay_zpl_zt411_host_identification(fixture_path),
        "zpl_zt411_extended_status":
            replay_zpl_zt411_extended_status(fixture_path),
    }


def _oid_sort_key(oid: str) -> tuple:
    """Numeric, lexicographic-by-component OID sort.

    Plain string sort orders "1.10" before "1.2", which would corrupt
    walk ordering; comparing as int tuples gives the canonical SNMP order.
    """
    parts = []
    for chunk in oid.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(chunk)
    return tuple(parts)
