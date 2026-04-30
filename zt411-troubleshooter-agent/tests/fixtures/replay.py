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


def make_fixture_replay(fixture_path: str | Path) -> Dict[str, Callable[..., ToolResult]]:
    """Bundle all three replay callables for a single fixture.

    Returns a dict ready to drive monkeypatch:

        replay = make_fixture_replay("zt411_fixture_paused.json")
        monkeypatch.setattr("zt411_agent.agent.tools.snmp_get",
                            replay["snmp_get"])
        monkeypatch.setattr("zt411_agent.agent.tools.snmp_walk",
                            replay["snmp_walk"])
        monkeypatch.setattr("zt411_agent.agent.tools.ipp_get_attributes",
                            replay["ipp_get_attributes"])
    """
    return {
        "snmp_get": replay_snmp_get(fixture_path),
        "snmp_walk": replay_snmp_walk(fixture_path),
        "ipp_get_attributes": replay_ipp_get_attributes(fixture_path),
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
