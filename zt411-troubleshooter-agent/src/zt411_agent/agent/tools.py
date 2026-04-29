"""
tools.py — Central tool registry for the ZT411 troubleshooter agent.

Every call that touches the network, OS, or printer device goes through here.

Provides:
  ToolResult      — structured return type for all tools
  ToolSchema      — metadata (name, timeout, rate-limit-key) for each tool
  RateLimiter     — sliding-window counter (per-printer + per-tool)
  OutputRedactor  — regex-based sensitive-data scrubber
  ToolRegistry    — register + execute with timeout, rate-limit, redaction
  ZT411OIDs       — SNMP OID constants for the Zebra ZT411
  Concrete implementations — network / windows / cups / device tool functions
"""
from __future__ import annotations

import logging
import re
import socket
import struct
import subprocess
import sys
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FuturesTimeout
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# Core data structures
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """Standardised return type for every tool function."""

    success: bool
    output: Any = None          # parsed / structured output
    raw: str = ""               # raw string before redaction
    error: str = ""
    duration_ms: float = 0.0

@dataclass
class ToolSchema:
    name: str
    description: str
    rate_limit_key: str = "per_tool"   # "per_printer" | "per_tool"
    timeout: float = 30.0

# ---------------------------------------------------------------------------
# Rate limiter (sliding-window, in-process)
# ---------------------------------------------------------------------------

class RateLimiter:
    """Sliding-window rate limiter.

    Config values from base.yaml:
      tools.rate_limits.per_printer_per_minute: 20
      tools.rate_limits.per_tool_per_minute:    60
    """

    def __init__(
        self,
        per_printer_per_minute: int = 20,
        per_tool_per_minute: int = 60,
    ) -> None:
        self._per_printer = per_printer_per_minute
        self._per_tool = per_tool_per_minute
        self._printer_windows: Dict[str, deque] = defaultdict(deque)
        self._tool_windows: Dict[str, deque] = defaultdict(deque)

    @staticmethod
    def _prune(q: deque, window_s: float = 60.0) -> None:
        now = time.monotonic()
        while q and now - q[0] > window_s:
            q.popleft()

    def check_and_record(self, tool_name: str, printer_ip: str) -> bool:
        """Return True if the call is allowed; False if rate-limited."""
        now = time.monotonic()

        pq = self._printer_windows[printer_ip]
        self._prune(pq)
        if len(pq) >= self._per_printer:
            logger.warning(
                "Rate limit hit: printer=%s limit=%d/min", printer_ip, self._per_printer
            )
            return False

        tq = self._tool_windows[tool_name]
        self._prune(tq)
        if len(tq) >= self._per_tool:
            logger.warning(
                "Rate limit hit: tool=%s limit=%d/min", tool_name, self._per_tool
            )
            return False

        pq.append(now)
        tq.append(now)
        return True

# ---------------------------------------------------------------------------
# Output redactor
# ---------------------------------------------------------------------------

class OutputRedactor:
    """Line-level regex redactor for sensitive data in tool output."""

    _BUILTIN = [
        r"(?i).*(password|passwd|secret|token|api.?key|auth).*",
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",  # email
    ]

    def __init__(
        self,
        patterns: Optional[List[str]] = None,
        enable: bool = True,
    ) -> None:
        self._enable = enable
        raw = (patterns or []) + self._BUILTIN
        self._compiled = [re.compile(p, re.IGNORECASE) for p in raw]

    def redact(self, text: str) -> str:
        if not self._enable or not text:
            return text
        out: List[str] = []
        for line in text.splitlines():
            for pat in self._compiled:
                if pat.search(line):
                    line = "[REDACTED]"
                    break
            out.append(line)
        return "\n".join(out)

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Wraps tool functions with rate-limiting, timeout, and redaction."""

    def __init__(
        self,
        rate_limiter: Optional[RateLimiter] = None,
        redactor: Optional[OutputRedactor] = None,
        default_timeout: float = 30.0,
    ) -> None:
        self._rate_limiter = rate_limiter or RateLimiter()
        self._redactor = redactor or OutputRedactor()
        self._default_timeout = default_timeout
        self._tools: Dict[str, Tuple[Callable[..., ToolResult], ToolSchema]] = {}
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tool")

    def register(self, schema: ToolSchema, fn: Callable[..., ToolResult]) -> None:
        self._tools[schema.name] = (fn, schema)

    def execute(self, tool_name: str, printer_ip: str, **kwargs: Any) -> ToolResult:
        """Execute a registered tool with all safety wrappers applied."""
        if tool_name not in self._tools:
            return ToolResult(success=False, error=f"unknown tool: {tool_name}")

        fn, schema = self._tools[tool_name]

        if not self._rate_limiter.check_and_record(tool_name, printer_ip):
            return ToolResult(success=False, error="rate_limited")

        timeout = schema.timeout or self._default_timeout
        t0 = time.monotonic()
        try:
            future = self._executor.submit(fn, **kwargs)
            result: ToolResult = future.result(timeout=timeout)
        except _FuturesTimeout:
            elapsed = (time.monotonic() - t0) * 1000
            return ToolResult(success=False, error="timeout", duration_ms=elapsed)
        except Exception as exc:  # noqa: BLE001
            elapsed = (time.monotonic() - t0) * 1000
            logger.error("Tool %s raised: %s", tool_name, exc, exc_info=True)
            return ToolResult(success=False, error=str(exc), duration_ms=elapsed)

        result.duration_ms = (time.monotonic() - t0) * 1000
        if result.raw:
            result.raw = self._redactor.redact(result.raw)

        return result

# ---------------------------------------------------------------------------
# ZT411 SNMP OID constants
# ---------------------------------------------------------------------------

class ZT411OIDs:
    # ===================================================================
    # Standard MIBs (kept for cross-vendor compatibility, but mostly NOT
    # implemented on this Zebra firmware — see notes below).
    # ===================================================================

    # System group (SNMPv2-MIB) — this works
    SYS_DESCR    = "1.3.6.1.2.1.1.1.0"
    SYS_OBJECT   = "1.3.6.1.2.1.1.2.0"
    SYS_UPTIME   = "1.3.6.1.2.1.1.3.0"
    SYS_NAME     = "1.3.6.1.2.1.1.5.0"

    # Host-Resources MIB — partially populated
    HR_DEVICE_STATUS = "1.3.6.1.2.1.25.3.2.1.5.1"  # always returns 1 (unknown) on this firmware

    # Standard Printer-MIB (RFC 3805) — NOT IMPLEMENTED on this firmware.
    # Walks of 1.3.6.1.2.1.43.* return 0 rows. Kept here for reference
    # only; queries against these OIDs will not return useful data.
    PRT_NAME           = "1.3.6.1.2.1.43.5.1.1.16.1"   # returns nothing
    PRT_ALERT_DESCR    = "1.3.6.1.2.1.43.18.1.1.8"     # returns nothing
    PRT_MARKER_SUPPLIES = "1.3.6.1.2.1.43.11.1.1.6"    # returns nothing

    # ===================================================================
    # Zebra enterprise tree (1.3.6.1.4.1.10642) — verified empirically
    # against ZT411 firmware V92.21.39Z on 2026-04-27.
    # ===================================================================

    # --- Identity (verified, all match Phase 1 lab passport) ---
    ZBR_MODEL    = "1.3.6.1.4.1.10642.1.1.0"   # 'ZTC ZT411-203dpi ZPL'
    ZBR_FIRMWARE = "1.3.6.1.4.1.10642.1.2.0"   # 'V92.21.39Z'
    ZBR_SERIAL   = "1.3.6.1.4.1.10642.1.4.0"   # '99N254700554'
    ZBR_LINK_OS  = "1.3.6.1.4.1.10642.1.18.0"  # '7.4'
    ZBR_MFG      = "1.3.6.1.4.1.10642.1.11.0"  # 'Zebra Technologies'

    ZBR_HEAD_OPEN  = "1.3.6.1.4.1.10642.2.1.1.0"
    ZBR_MEDIA_OUT  = "1.3.6.1.4.1.10642.6.23.0"
    ZBR_RIBBON_OUT = "1.3.6.1.4.1.10642.6.24.0"
    ZBR_ANY_FAULT  = "1.3.6.1.4.1.10642.6.22.0"  # 1=ready, 2=any not-ready (incl. pause)

    # PAUSED has no dedicated SNMP OID. Detect via:
    #   - bitmask part1 == 0 AND part2 has 0x10000 set, OR
    #   - alert table row with group=1, code=11 AND no other severity>=3 alerts
    ZBR_PAUSED = None

    # --- State bitmask (verified) ---
    # Format: 's1,s2,HHHHHHHH_part1,HHHHHHHH_part2'
    # Part 1 = physical fault bits (OR'd):
    #   0x01 = MEDIA_OUT, 0x02 = RIBBON_OUT, 0x04 = HEAD_OPEN
    # Part 2 = composite "not-ready" bit:
    #   0x10000 = printer is in any not-ready state (faults OR pause)
    ZBR_STATE_BITMASK = "1.3.6.1.4.1.10642.2.10.3.7.0"
    ZBR_STATE_BITMASK_LONG = "1.3.6.1.4.1.10642.2.10.3.6.0"  # same data + padding

    ZBR_BIT_MEDIA_OUT  = 0x01
    ZBR_BIT_RIBBON_OUT = 0x02
    ZBR_BIT_HEAD_OPEN  = 0x04
    ZBR_BIT_NOT_READY  = 0x10000  # in part 2; set on any fault OR pause

    # 'yes' at idle, 'no' for any not-ready state (including pause).
    # Equivalent to ZBR_ANY_FAULT, just string-typed instead of int.
    ZBR_READY = "1.3.6.1.4.1.10642.2.10.3.8.0"

    # --- Alert table (verified live-state, not a log) ---
    # Active faults appear as rows; rows are removed when condition clears.
    # Filter by severity>=3 to ignore the persistent boot informational entry (id=1).
    # Schema: .1=id, .2=severity, .3=type, .4=group, .5=code, .6=timestamp(uptime ticks)
    ZBR_ALERT_TABLE = "1.3.6.1.4.1.10642.10.31.1"

    # Severity values
    ZBR_SEVERITY_INFO     = 1   # ignore for live-state checks
    ZBR_SEVERITY_CRITICAL = 3   # active fault

    # Group values (verified)
    ZBR_GROUP_INFO   = 1   # informational / pause companion
    ZBR_GROUP_MEDIA  = 2
    ZBR_GROUP_RIBBON = 3
    ZBR_GROUP_HEAD   = 4

    # (group, code) -> fault name (verified)
    #   (4, 5)  = HEAD_OPEN
    #   (2, 1)  = MEDIA_OUT
    #   (3, 2)  = RIBBON_OUT
    #   (1, 11) = PAUSED  (auto-emitted alongside any physical fault;
    #                      standalone if no other severity>=3 alert exists)

    # --- Provisional / deprecated ---
    # These respond but always read 0/empty even during faults. Do not use.
    ZBR_ERROR_CODE = "1.3.6.1.4.1.10642.2.3.4.1.0"   # always 0
    ZBR_ERROR_TEXT = "1.3.6.1.4.1.10642.2.3.4.2.0"   # always '' 

# ---------------------------------------------------------------------------
# Zebra error-code → KB citation mapping
# ---------------------------------------------------------------------------

_ZBR_ERROR_KB: Dict[str, Dict[str, str]] = {
    "001": {
        "title": "Head Open",
        "description": "Printhead latch is open. Close and latch the printhead.",
        "doc_ref": "ZT411_OG_p45",
    },
    "002": {
        "title": "Media Out",
        "description": "No media loaded or media ran out. Load media and recalibrate.",
        "doc_ref": "ZT411_OG_p52",
    },
    "003": {
        "title": "Ribbon Out",
        "description": "Ribbon depleted. Install a new ribbon roll and re-thread.",
        "doc_ref": "ZT411_OG_p58",
    },
    "004": {
        "title": "Ribbon In / Wrong Ribbon",
        "description": "Ribbon installed but printer is configured for direct thermal.",
        "doc_ref": "ZT411_OG_p61",
    },
    "005": {
        "title": "Media Jam / Paper Jam",
        "description": "Media jammed inside the printer. Open, clear, and recalibrate.",
        "doc_ref": "ZT411_OG_p67",
    },
    "007": {
        "title": "CRC Error (Firmware)",
        "description": "Firmware CRC mismatch. Re-flash firmware via ZDownloader.",
        "doc_ref": "ZT411_FW_p12",
    },
    "010": {
        "title": "Printhead Over Temperature",
        "description": "Allow printer to cool. Check ambient temperature.",
        "doc_ref": "ZT411_OG_p73",
    },
    "011": {
        "title": "Printhead Under Temperature",
        "description": "Move printer to a warmer environment. Minimum operating 5 °C.",
        "doc_ref": "ZT411_OG_p73",
    },
    "015": {
        "title": "RFID Error",
        "description": "RFID module fault. Run RFID calibration from the front panel.",
        "doc_ref": "ZT411_OG_p89",
    },
    "020": {
        "title": "Cutter Fault",
        "description": "Cutter jam or motor fault. Clear jam and power-cycle.",
        "doc_ref": "ZT411_OG_p81",
    },
}

def map_error_code_to_kb(code: str) -> Dict[str, str]:
    """Return a KB citation dict for a given Zebra error code string.

    Strips leading zeros for lookup; returns a generic entry if unknown.
    """
    key = code.lstrip("0") or "0"
    padded = key.zfill(3)
    entry = _ZBR_ERROR_KB.get(padded)
    if entry:
        return {"code": code, **entry}
    return {
        "code": code,
        "title": f"Unknown error code {code}",
        "description": "Refer to Zebra support or the ZT411 Operations Guide appendix.",
        "doc_ref": "ZT411_OG_appendix",
    }

# ---------------------------------------------------------------------------
# Helper — run subprocess safely
# ---------------------------------------------------------------------------

def _run(
    cmd: List[str],
    timeout: float = 15.0,
    input_text: Optional[str] = None,
) -> Tuple[int, str, str]:
    """Run a subprocess; return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError as exc:
        return -2, "", f"executable not found: {exc}"
    except Exception as exc:  # noqa: BLE001
        return -3, "", str(exc)

# ===========================================================================
# NETWORK TOOLS
# ===========================================================================

def ping(ip: str, timeout_s: float = 2.0, count: int = 1) -> ToolResult:
    """ICMP echo probe.  Returns output={'reachable': bool, 'latency_ms': float|None}."""
    if _IS_WINDOWS:
        cmd = ["ping", "-n", str(count), "-w", str(int(timeout_s * 1000)), ip]
    else:
        cmd = ["ping", "-c", str(count), "-W", str(max(1, int(timeout_s))), ip]

    rc, stdout, stderr = _run(cmd, timeout=timeout_s + 3)
    raw = stdout + stderr

    reachable = rc == 0
    latency_ms: Optional[float] = None

    if reachable:
        # Try to parse latency from output
        # Windows: "Average = 5ms"  Linux: "time=5.12 ms"
        for pattern in (r"time[<=](\d+\.?\d*)\s*ms", r"Average\s*=\s*(\d+)ms"):
            m = re.search(pattern, raw, re.IGNORECASE)
            if m:
                try:
                    latency_ms = float(m.group(1))
                except ValueError:
                    pass
                break

    return ToolResult(
        success=True,
        output={"reachable": reachable, "latency_ms": latency_ms},
        raw=raw,
    )

def tcp_connect(ip: str, port: int, timeout_s: float = 3.0) -> ToolResult:
    """TCP SYN probe — returns output={'open': bool}."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect((ip, port))
        sock.close()
        return ToolResult(success=True, output={"open": True})
    except (socket.timeout, ConnectionRefusedError, OSError) as exc:
        return ToolResult(success=True, output={"open": False}, error=str(exc))
    finally:
        try:
            sock.close()
        except OSError:
            pass

def dns_lookup(hostname: str) -> ToolResult:
    """Resolve hostname → IP.  Returns output={'ip': str, 'resolved': bool}."""
    try:
        ip = socket.gethostbyname(hostname)
        return ToolResult(success=True, output={"ip": ip, "resolved": True})
    except socket.gaierror as exc:
        return ToolResult(
            success=True,
            output={"ip": "", "resolved": False},
            error=str(exc),
        )

def arp_lookup(ip: str) -> ToolResult:
    """Query the local ARP cache for a MAC address.

    Returns output={'mac': str, 'found': bool}.
    """
    if _IS_WINDOWS:
        cmd = ["arp", "-a", ip]
    else:
        cmd = ["arp", "-n", ip]

    rc, stdout, stderr = _run(cmd, timeout=5.0)
    raw = stdout + stderr

    # Parse MAC from output — matches hex pairs separated by : or -
    mac_pat = re.compile(r"([0-9a-fA-F]{2}[:\-][0-9a-fA-F]{2}[:\-][0-9a-fA-F]{2}"
                         r"[:\-][0-9a-fA-F]{2}[:\-][0-9a-fA-F]{2}[:\-][0-9a-fA-F]{2})")
    m = mac_pat.search(stdout)
    if m:
        mac = m.group(1).upper().replace("-", ":")
        return ToolResult(success=True, output={"mac": mac, "found": True}, raw=raw)

    return ToolResult(success=True, output={"mac": "", "found": False}, raw=raw)

# OUI prefix → vendor (subset of common printer / network vendors)
_OUI_TABLE: Dict[str, str] = {
    "00:07:4D": "Zebra Technologies",
    "00:1C:7E": "Zebra Technologies",
    "84:24:8D": "Zebra Technologies",
    "48:A4:72": "Zebra Technologies",
    "00:0D:4B": "Roku / Zebra",
    "00:50:56": "VMware",
    "00:0C:29": "VMware",
    "08:00:27": "VirtualBox",
    "00:04:0D": "Avocent",
    "00:60:B0": "Hewlett-Packard",
    "00:17:A4": "Hewlett-Packard",
    "00:1A:4B": "Cisco Systems",
    "00:23:F8": "Cisco Systems",
}

def oui_vendor(mac: str) -> ToolResult:
    """Look up the vendor prefix of a MAC address.

    Returns output={'vendor': str, 'oui': str}.
    """
    normalised = mac.upper().replace("-", ":")
    oui = ":".join(normalised.split(":")[:3]) if ":" in normalised else ""
    vendor = _OUI_TABLE.get(oui, "unknown")
    return ToolResult(success=True, output={"vendor": vendor, "oui": oui})

# ---------------------------------------------------------------------------
# SNMP helpers
# ---------------------------------------------------------------------------

def _snmp_available() -> bool:
    try:
        import pysnmp 
        return True
    except ImportError:
        return False

def _parse_snmp_value(value: Any) -> Any:
    if hasattr(value, "asOctets"):
        raw_bytes = value.asOctets()
        try:
            return raw_bytes.decode("utf-8").strip("\x00").strip()
        except UnicodeDecodeError:
            return raw_bytes.hex()

    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return str(value)

def snmp_get(
    ip: str,
    oid: str,
    community: str = "public",
    timeout_s: int = 5,
    port: int = 161,
) -> ToolResult:
    """SNMPv2c GET for a single OID.

    Returns output={'value': <parsed>} or error on failure.
    Requires pysnmp ≥ 7.x (uses async API under the hood).
    """
    import asyncio

    if not _snmp_available():
        return ToolResult(success=False, error="pysnmp not installed")

    try:
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            get_cmd,
        )
    except ImportError:
        return ToolResult(success=False, error="pysnmp hlapi unavailable")

    async def _do_get():
        transport = await UdpTransportTarget.create(
            (ip, port), timeout=timeout_s, retries=1
        )
        return await get_cmd(
            SnmpEngine(),
            CommunityData(community, mpModel=1),
            transport,
            ContextData(),
            ObjectType(ObjectIdentity(oid)),
        )

    try:
        errorIndication, errorStatus, errorIndex, varBinds = asyncio.run(_do_get())

        if errorIndication:
            return ToolResult(success=False, error=str(errorIndication))
        if errorStatus:
            return ToolResult(
                success=False,
                error=f"SNMP error {errorStatus.prettyPrint()} at {errorIndex}",
            )

        for varBind in varBinds:
            value = varBind[1]
            # Decode OctetString as UTF-8 if it looks printable, else hex
            if hasattr(value, "asOctets"):
                raw_bytes = value.asOctets()
                try:
                    decoded = raw_bytes.decode("utf-8").strip("\x00").strip()
                    return ToolResult(success=True, output={"value": decoded})
                except UnicodeDecodeError:
                    return ToolResult(success=True, output={"value": raw_bytes.hex()})
            return ToolResult(success=True, output={"value": int(value)})

        return ToolResult(success=False, error="no varbinds returned")
    except Exception as exc:  # noqa: BLE001
        return ToolResult(success=False, error=str(exc))

def snmp_walk(
    ip: str,
    oid_prefix: str,
    community: str = "public",
    timeout_s: int = 10,
    port: int = 161,
    max_rows: int = 50,
) -> ToolResult:
    """SNMPv2c GETNEXT walk from oid_prefix.

    Returns output={'rows': [{oid: str, value: any}, ...]}
    """
    import asyncio

    if not _snmp_available():
        return ToolResult(success=False, error="pysnmp not installed")

    try:
        from pysnmp.hlapi.v3arch.asyncio import (
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            walk_cmd,
        )
    except ImportError:
        return ToolResult(success=False, error="pysnmp hlapi unavailable")

    async def _do_walk() -> Tuple[List[Dict[str, Any]], Optional[str]]:
        collected: List[Dict[str, Any]] = []
        transport = await UdpTransportTarget.create(
            (ip, port), timeout=timeout_s, retries=1
        )
        async for errorIndication, errorStatus, errorIndex, varBinds in walk_cmd(
            SnmpEngine(),
            CommunityData(community, mpModel=1),
            transport,
            ContextData(),
            ObjectType(ObjectIdentity(oid_prefix)),
            lexicographicMode=False,
        ):
            if errorIndication:
                return collected, str(errorIndication)
            if errorStatus:
                return collected, f"{errorStatus.prettyPrint()} at {errorIndex}"

            for varBind in varBinds:
                oid_str = str(varBind[0])
                parsed = _parse_snmp_value(varBind[1])
                collected.append({"oid": oid_str, "value": parsed})

            if len(collected) >= max_rows:
                break

        return collected, None

    try:
        rows, err = asyncio.run(_do_walk())
        if err is not None:
            return ToolResult(success=False, error=err)
        return ToolResult(success=True, output={"rows": rows})
    except Exception as exc:  # noqa: BLE001
        return ToolResult(success=False, error=str(exc))

# ===========================================================================
# DEVICE TOOLS  (ZT411-specific SNMP + IPP)
# ===========================================================================

def snmp_zt411_status(
    ip: str,
    community: str = "public",
) -> ToolResult:
    """Read ZT411 printer status via SNMP.

    Queries both standard Printer-MIB and Zebra enterprise OIDs.
    Returns output dict with keys: printer_status, hr_status, model,
    firmware, serial, mac, sys_descr.
    """
    o = ZT411OIDs
    results: Dict[str, Any] = {}

    for key, oid in [
        ("sys_descr", o.SYS_DESCR),
        ("sys_name", o.SYS_NAME),
        ("hr_status", o.HR_PRINTER_STATUS),
        ("prt_name", o.PRT_GENERAL_PRINTER_NAME),
        ("zbr_model", o.ZBR_MODEL),
        ("zbr_firmware", o.ZBR_FIRMWARE),
        ("zbr_serial", o.ZBR_SERIAL),
    ]:
        r = snmp_get(ip, oid, community)
        if r.success and r.output:
            results[key] = r.output.get("value")

    # Map hrPrinterStatus integer to string
    _hr_map = {3: "idle", 4: "printing", 5: "warmup", 1: "other", 2: "unknown"}
    if "hr_status" in results:
        try:
            results["printer_status"] = _hr_map.get(int(results["hr_status"]), "unknown")
        except (TypeError, ValueError):
            results["printer_status"] = "unknown"

    if not results:
        return ToolResult(success=False, error="no SNMP response from device")
    return ToolResult(success=True, output=results)

def snmp_zt411_physical_flags(
    ip: str,
    community: str = "public",
) -> ToolResult:
    """Read ZT411 physical condition flags via Zebra state bitmask.
    
    Reads the state bitmask at ZBR_STATE_BITMASK and decodes bit
    positions for media_out, ribbon_out, head_open. Pause has no
    SNMP representation on this firmware (V92.21.39Z) — caller must
    use ipp_get_attributes() and check printer-state for that.
    
    Returns output={'head_open': bool, 'media_out': bool,
                    'ribbon_out': bool, 'paused': None,
                    'raw_bitmask': str}
    """
    o = ZT411OIDs
    flags: Dict[str, Optional[bool]] = {
        "head_open": None,
        "media_out": None,
        "ribbon_out": None,
        "paused": None,           # always None — not in SNMP on this firmware
    }

    r = snmp_get(ip, o.ZBR_STATE_BITMASK, community)
    if not r.success or r.output is None:
        return ToolResult(
            success=False,
            output=flags,
            error=f"could not read state bitmask: {r.error}",
        )

    raw = str(r.output.get("value", ""))
    # Parse comma-string: '1,1,00000000,000100XX'
    parts = raw.split(",")
    if len(parts) < 4:
        return ToolResult(
            success=False,
            output={**flags, "raw_bitmask": raw},
            error=f"unexpected bitmask format: {raw!r}",
        )
    try:
        bits = int(parts[3], 16)
    except ValueError:
        return ToolResult(
            success=False,
            output={**flags, "raw_bitmask": raw},
            error=f"could not parse hex from bitmask field: {parts[3]!r}",
        )

    flags["media_out"]  = bool(bits & o.ZBR_BIT_MEDIA_OUT)
    flags["ribbon_out"] = bool(bits & o.ZBR_BIT_RIBBON_OUT)
    flags["head_open"]  = bool(bits & o.ZBR_BIT_HEAD_OPEN)

    return ToolResult(
        success=True,
        output={**flags, "raw_bitmask": raw, "bits_set": hex(bits)},
    )

def snmp_zt411_consumables(ip: str, community: str = "public") -> ToolResult:
    """
    Report consumable presence on ZT411 firmware V92.21.39Z.

    This firmware does NOT expose ribbon/media level percentages via SNMP
    (verified by induce-and-diff against a Phase 1 lab unit on 2026-04-28).
    Returns boolean presence only. For finer-grained estimation, the agent
    should fall back to IPP marker-* attributes if available, or escalate
    to a physical inspection.
    """
    media = snmp_get(ip, ZT411OIDs.ZBR_MEDIA_OUT, community)   # 1=present, 2=empty
    ribbon = snmp_get(ip, ZT411OIDs.ZBR_RIBBON_OUT, community)

    return ToolResult(
        success=(media.success and ribbon.success),
        output={
            "media":  "present" if media.value == 1  else "empty" if media.value == 2  else "unknown",
            "ribbon": "present" if ribbon.value == 1 else "empty" if ribbon.value == 2 else "unknown",
            "supports_levels": False,
            "note": "ZT411 firmware V92.21.39Z exposes presence only; no level data via SNMP",
            "sources_queried": ["ZBR_MEDIA_OUT", "ZBR_RIBBON_OUT"],
        },
    )

def snmp_zt411_alerts(
    ip: str,
    community: str = "public",
) -> ToolResult:
    """Walk Zebra's alert table for active alerts.
    
    Returns output={'alerts': [...], 'count': N, 'raw': [...]}
    
    Each alert dict has: id, severity, type, group, code, timestamp.
    Empty list at idle. Pause does not populate the alert table on
    this firmware — that's expected, not a bug.
    """
    o = ZT411OIDs

    r = snmp_walk(ip, o.ZBR_ALERT_TABLE, community, max_rows=200)
    if not r.success:
        return ToolResult(success=False, error=f"alert table walk failed: {r.error}")

    rows = r.output.get("rows", []) if r.output else []

    # Group columns by alert ID (the trailing OID component).
    # OIDs look like: 1.3.6.1.4.1.10642.10.31.1.<col>.<id>
    by_id: Dict[str, Dict[int, Any]] = defaultdict(dict)
    for row in rows:
        oid = row["oid"]
        # Strip the table prefix and split column.id
        if not oid.startswith(o.ZBR_ALERT_TABLE + "."):
            continue
        suffix = oid[len(o.ZBR_ALERT_TABLE) + 1:]
        try:
            col_str, alert_id = suffix.split(".", 1)
            col = int(col_str)
        except (ValueError, IndexError):
            continue
        by_id[alert_id][col] = row["value"]

    COL_NAMES = {1: "id", 2: "severity", 3: "type", 4: "group",
                 5: "code", 6: "timestamp"}

    alerts = []
    for alert_id, cols in sorted(by_id.items(), key=lambda kv: int(kv[0])
                                 if kv[0].isdigit() else 0):
        alert = {COL_NAMES.get(c, f"col_{c}"): v for c, v in cols.items()}
        alerts.append(alert)

    return ToolResult(
        success=True,
        output={"alerts": alerts, "count": len(alerts), "raw_rows": rows},
    )

def ipp_get_attributes(ip: str, port: int = 631) -> ToolResult:
    """Read IPP printer attributes via GET-PRINTER-ATTRIBUTES request.

    Crafts a minimal IPP/1.1 binary request over HTTP.
    Returns output={'attributes': {name: value}} or error.
    """
    try:
        import httpx  # type: ignore[import]
    except ImportError:
        return ToolResult(success=False, error="httpx not installed")

    # Build minimal IPP GET-PRINTER-ATTRIBUTES request (RFC 8011)
    printer_uri = f"ipp://{ip}:{port}/ipp/print"
    uri_bytes = printer_uri.encode("utf-8")

    def _ipp_str(tag: int, name: bytes, value: bytes) -> bytes:
        return (
            struct.pack(">B", tag)
            + struct.pack(">H", len(name)) + name
            + struct.pack(">H", len(value)) + value
        )

    payload = (
        b"\x01\x01"                          # IPP version 1.1
        b"\x00\x0b"                          # op: Get-Printer-Attributes
        b"\x00\x00\x00\x01"                  # request-id: 1
        b"\x01"                              # begin-attribute-group: operation
        + _ipp_str(0x47, b"attributes-charset", b"utf-8")
        + _ipp_str(0x48, b"attributes-natural-language", b"en")
        + _ipp_str(0x45, b"printer-uri", uri_bytes)
        + _ipp_str(0x44, b"requesting-user-name", b"agent")
        + b"\x03"                            # end-of-attributes
    )

    try:
        response = httpx.post(
            f"http://{ip}:{port}/ipp/print",
            content=payload,
            headers={"Content-Type": "application/ipp"},
            timeout=10.0,
        )
        raw = response.content
    except Exception as exc:  # noqa: BLE001
        return ToolResult(success=False, error=f"IPP request failed: {exc}")

    # Minimal parse: scan for text keyword=value pairs in the binary response
    attrs: Dict[str, str] = {}
    try:
        # Skip 8-byte header; scan for attribute name/value sequences
        pos = 8
        while pos < len(raw):
            tag = raw[pos]
            pos += 1
            if tag in (0x01, 0x02, 0x03, 0x04, 0x05, 0x06):
                continue  # group/delimiter tags
            if pos + 4 > len(raw):
                break
            name_len = struct.unpack(">H", raw[pos : pos + 2])[0]
            pos += 2
            name = raw[pos : pos + name_len].decode("utf-8", errors="replace")
            pos += name_len
            if pos + 2 > len(raw):
                break
            val_len = struct.unpack(">H", raw[pos : pos + 2])[0]
            pos += 2
            val_raw = raw[pos : pos + val_len]
            pos += val_len
            try:
                val_str = val_raw.decode("utf-8", errors="replace").strip("\x00")
            except Exception:  # noqa: BLE001
                val_str = val_raw.hex()
            if name:
                attrs[name] = val_str
    except Exception as exc:  # noqa: BLE001
        logger.debug("IPP parse error (partial result ok): %s", exc)

    return ToolResult(success=True, output={"attributes": attrs}, raw=repr(attrs))

# ===========================================================================
# WINDOWS TOOLS
# ===========================================================================

def _ps_run(
    command: str,
    timeout_s: float = 15.0,
) -> ToolResult:
    """Execute a PowerShell command; returns ToolResult with raw stdout/stderr."""
    if not _IS_WINDOWS:
        return ToolResult(
            success=False, error="PowerShell tools only available on Windows"
        )
    cmd = [
        "powershell.exe",
        "-NonInteractive",
        "-NoProfile",
        "-OutputFormat", "Text",
        "-Command", command,
    ]
    rc, stdout, stderr = _run(cmd, timeout=timeout_s)
    raw = (stdout + "\n" + stderr).strip()
    if rc == 0:
        return ToolResult(success=True, output=stdout.strip(), raw=raw)
    return ToolResult(success=False, output=stdout.strip(), raw=raw, error=stderr.strip())

def ps_query_spooler() -> ToolResult:
    """Query the Windows Print Spooler service status.

    Returns output={'running': bool, 'status': str, 'start_type': str}.
    """
    r = _ps_run(
        "Get-Service -Name Spooler | Select-Object Status,StartType | ConvertTo-Json"
    )
    if not r.success:
        # Fallback to sc.exe
        rc, stdout, _ = _run(["sc", "query", "Spooler"], timeout=10.0)
        running = "RUNNING" in stdout.upper()
        return ToolResult(
            success=True,
            output={"running": running, "status": stdout.strip(), "start_type": "unknown"},
            raw=stdout,
        )

    raw_json = r.output or ""
    import json as _json
    try:
        data = _json.loads(raw_json)
        status = str(data.get("Status", "")).strip()
        start_type = str(data.get("StartType", "")).strip()
        running = status.lower() in ("4", "running")
        return ToolResult(
            success=True,
            output={"running": running, "status": status, "start_type": start_type},
            raw=raw_json,
        )
    except _json.JSONDecodeError:
        running = "running" in raw_json.lower() or "4" in raw_json
        return ToolResult(
            success=True,
            output={"running": running, "status": raw_json, "start_type": "unknown"},
            raw=raw_json,
        )

def ps_enum_printers() -> ToolResult:
    """Enumerate Windows printers via Get-Printer.

    Returns output={'printers': [{'name': str, 'driver': str, 'port': str,
                                    'shared': bool, 'published': bool,
                                    'printer_status': int}]}.
    """
    cmd = (
        "Get-Printer | Select-Object Name,DriverName,PortName,Shared,Published,PrinterStatus"
        " | ConvertTo-Json -Compress"
    )
    r = _ps_run(cmd)
    if not r.success:
        return ToolResult(success=False, error=r.error, raw=r.raw)

    import json as _json
    try:
        raw = r.output or "[]"
        # PowerShell returns a single object (not array) if only one printer
        data = _json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        printers = [
            {
                "name": str(p.get("Name", "")),
                "driver": str(p.get("DriverName", "")),
                "port": str(p.get("PortName", "")),
                "shared": bool(p.get("Shared", False)),
                "published": bool(p.get("Published", False)),
                "printer_status": int(p.get("PrinterStatus", 0)),
            }
            for p in (data or [])
        ]
        return ToolResult(success=True, output={"printers": printers}, raw=raw)
    except (_json.JSONDecodeError, TypeError) as exc:
        return ToolResult(success=False, error=str(exc), raw=r.raw)

def ps_enum_jobs(queue_name: str) -> ToolResult:
    """Enumerate print jobs in a Windows queue.

    Returns output={'jobs': [{'id': int, 'document': str, 'status': str,
                               'user': str, 'size': int}]}.
    """
    safe_queue = queue_name.replace("'", "''")
    cmd = (
        f"Get-PrintJob -PrinterName '{safe_queue}'"
        " | Select-Object Id,Document,JobStatus,UserName,Size | ConvertTo-Json -Compress"
    )
    r = _ps_run(cmd)
    if not r.success:
        return ToolResult(success=False, error=r.error, raw=r.raw)

    import json as _json
    try:
        raw = r.output or "[]"
        data = _json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        jobs = [
            {
                "id": int(j.get("Id", 0)),
                "document": str(j.get("Document", "")),
                "status": str(j.get("JobStatus", "")),
                "user": str(j.get("UserName", "")),
                "size": int(j.get("Size", 0)),
            }
            for j in (data or [])
        ]
        return ToolResult(success=True, output={"jobs": jobs}, raw=raw)
    except (_json.JSONDecodeError, TypeError) as exc:
        return ToolResult(success=False, error=str(exc), raw=r.raw)

def ps_get_driver(queue_name: str) -> ToolResult:
    """Retrieve driver metadata for a Windows print queue.

    Returns output={'name': str, 'version': str, 'isolation': str, 'provider': str}.
    """
    safe_queue = queue_name.replace("'", "''")
    cmd = (
        f"$p = Get-Printer -Name '{safe_queue}';"
        " Get-PrinterDriver -Name $p.DriverName"
        " | Select-Object Name,DriverVersion,PrinterEnvironment,MajorVersion,MinorVersion,Provider"
        " | ConvertTo-Json -Compress"
    )
    r = _ps_run(cmd)
    if not r.success:
        return ToolResult(success=False, error=r.error, raw=r.raw)

    import json as _json
    try:
        data = _json.loads(r.output or "{}")
        version = f"{data.get('MajorVersion','')}.{data.get('MinorVersion','')}"
        return ToolResult(
            success=True,
            output={
                "name": str(data.get("Name", "")),
                "version": version.strip("."),
                "isolation": str(data.get("PrinterEnvironment", "")),
                "provider": str(data.get("Provider", "")),
            },
            raw=r.output or "",
        )
    except (_json.JSONDecodeError, TypeError) as exc:
        return ToolResult(success=False, error=str(exc), raw=r.raw)

def ps_get_event_log(last_n: int = 50) -> ToolResult:
    """Read recent PrintService/Admin errors from the Windows event log.

    Returns output={'errors': [str]} — message strings of Error/Warning events.
    """
    cmd = (
        f"Get-WinEvent -LogName 'Microsoft-Windows-PrintService/Admin'"
        f" -MaxEvents {last_n} -ErrorAction SilentlyContinue"
        " | Where-Object {$_.Level -le 3}"
        " | Select-Object -ExpandProperty Message"
    )
    r = _ps_run(cmd, timeout_s=20.0)
    errors = [line.strip() for line in (r.output or "").splitlines() if line.strip()]
    if not errors and not r.success:
        return ToolResult(success=False, error=r.error, raw=r.raw)
    return ToolResult(success=True, output={"errors": errors}, raw=r.raw)

def ps_restart_service(service_name: str) -> ToolResult:
    """Restart a Windows service by name.

    Requires the process to run as Administrator.
    Returns output={'restarted': bool}.
    """
    safe = service_name.replace("'", "''")
    cmd = f"Restart-Service -Name '{safe}' -Force -PassThru | Select-Object Status | ConvertTo-Json"
    r = _ps_run(cmd, timeout_s=30.0)
    if not r.success:
        return ToolResult(success=False, error=r.error, raw=r.raw)

    import json as _json
    try:
        data = _json.loads(r.output or "{}")
        status = str(data.get("Status", "")).lower()
        return ToolResult(
            success=True,
            output={"restarted": status in ("4", "running")},
            raw=r.raw,
        )
    except (_json.JSONDecodeError, TypeError):
        # Treat non-error exit as success
        return ToolResult(success=True, output={"restarted": True}, raw=r.raw)

def ps_cancel_job(queue_name: str, job_id: int) -> ToolResult:
    """Cancel a specific print job by ID.

    Returns output={'cancelled': bool}.
    """
    safe_queue = queue_name.replace("'", "''")
    cmd = f"Remove-PrintJob -PrinterName '{safe_queue}' -ID {job_id}"
    r = _ps_run(cmd, timeout_s=15.0)
    return ToolResult(
        success=r.success,
        output={"cancelled": r.success},
        error=r.error,
        raw=r.raw,
    )

def ps_set_printer_online(queue_name: str) -> ToolResult:
    """Bring a Windows print queue back online.

    Returns output={'online': bool}.
    """
    safe_queue = queue_name.replace("'", "''")
    cmd = (
        f"Set-Printer -Name '{safe_queue}' -DeviceType Print;"
        f" (Get-Printer -Name '{safe_queue}').PrinterStatus"
    )
    r = _ps_run(cmd, timeout_s=15.0)
    online = r.success and "offline" not in (r.output or "").lower()
    return ToolResult(
        success=r.success,
        output={"online": online},
        error=r.error,
        raw=r.raw,
    )

# ===========================================================================
# CUPS TOOLS
# ===========================================================================

def _cups_available() -> bool:
    rc, _, _ = _run(["which", "lpstat"], timeout=3.0)
    return rc == 0 or _run(["lpstat", "--help"], timeout=3.0)[0] in (0, 1)

def lpstat_v() -> ToolResult:
    """Run lpstat -v to list CUPS printer queues and their device URIs.

    Returns output={'queues': [{'name': str, 'device_uri': str}]}.
    """
    rc, stdout, stderr = _run(["lpstat", "-v"], timeout=10.0)
    raw = stdout + stderr
    queues: List[Dict[str, str]] = []
    # Format: "device for <name>: <uri>"
    for line in stdout.splitlines():
        m = re.match(r"device for ([^:]+):\s+(.+)", line.strip())
        if m:
            queues.append({"name": m.group(1).strip(), "device_uri": m.group(2).strip()})
    if rc != 0 and not queues:
        return ToolResult(success=False, error=stderr.strip(), raw=raw)
    return ToolResult(success=True, output={"queues": queues}, raw=raw)

def lpstat_p() -> ToolResult:
    """Run lpstat -p to get printer status (idle, processing, stopped).

    Returns output={'printers': [{'name': str, 'state': str}]}.
    """
    rc, stdout, stderr = _run(["lpstat", "-p"], timeout=10.0)
    raw = stdout + stderr
    printers: List[Dict[str, str]] = []
    # Format: "printer <name> is <state>. enabled since ..."
    for line in stdout.splitlines():
        m = re.match(r"printer ([^\s]+)\s+is\s+(\S+)", line.strip(), re.IGNORECASE)
        if m:
            printers.append({"name": m.group(1), "state": m.group(2).lower()})
    if rc != 0 and not printers:
        return ToolResult(success=False, error=stderr.strip(), raw=raw)
    return ToolResult(success=True, output={"printers": printers}, raw=raw)

def lpstat_jobs(queue_name: str) -> ToolResult:
    """Run lpstat -o <queue> to enumerate pending jobs.

    Returns output={'jobs': [{'id': str, 'user': str, 'size': str, 'time': str}]}.
    """
    rc, stdout, stderr = _run(["lpstat", "-o", queue_name], timeout=10.0)
    raw = stdout + stderr
    jobs: List[Dict[str, str]] = []
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            jobs.append({
                "id": parts[0],
                "user": parts[1] if len(parts) > 1 else "",
                "size": parts[2] if len(parts) > 2 else "",
                "time": " ".join(parts[3:]),
            })
    if rc not in (0,) and not jobs:
        return ToolResult(success=False, error=stderr.strip(), raw=raw)
    return ToolResult(success=True, output={"jobs": jobs}, raw=raw)

def cups_error_log(n_lines: int = 100) -> ToolResult:
    """Read the last n_lines from the CUPS error_log.

    Returns output={'lines': [str], 'filter_errors': [str]}.
    """
    log_paths = [
        "/var/log/cups/error_log",
        "/usr/local/var/log/cups/error_log",  # macOS Homebrew
    ]
    for path in log_paths:
        try:
            with open(path, "r", errors="replace") as fh:
                all_lines = fh.readlines()
            tail = [l.rstrip() for l in all_lines[-n_lines:]]
            filter_errors = [l for l in tail if re.search(r"\b(Error|emerg|crit|alert)\b", l, re.I)]
            return ToolResult(
                success=True,
                output={"lines": tail, "filter_errors": filter_errors},
                raw="\n".join(tail),
            )
        except (FileNotFoundError, PermissionError):
            continue

    # Fallback: journalctl
    rc, stdout, stderr = _run(
        ["journalctl", "-u", "cups", "--no-pager", "-n", str(n_lines)], timeout=10.0
    )
    if rc == 0:
        lines = stdout.splitlines()
        filter_errors = [l for l in lines if re.search(r"\bError\b", l, re.I)]
        return ToolResult(success=True, output={"lines": lines, "filter_errors": filter_errors}, raw=stdout)

    return ToolResult(success=False, error="CUPS error_log not accessible", raw=stderr)

def lpinfo_m() -> ToolResult:
    """Run lpinfo -m to list available PPD/driver models.

    Returns output={'models': [{'uri': str, 'description': str}]}.
    """
    rc, stdout, stderr = _run(["lpinfo", "-m"], timeout=20.0)
    raw = stdout + stderr
    models: List[Dict[str, str]] = []
    for line in stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            models.append({"uri": parts[0], "description": parts[1]})
    if rc != 0 and not models:
        return ToolResult(success=False, error=stderr.strip(), raw=raw)
    zebra_models = [m for m in models if "zebra" in m["description"].lower() or "zt" in m["description"].lower()]
    return ToolResult(success=True, output={"models": models, "zebra_models": zebra_models}, raw=raw)

def lpoptions(queue_name: str) -> ToolResult:
    """Run lpoptions -p <queue> -l to list driver options and current values.

    Returns output={'options': {name: value}}.
    """
    rc, stdout, stderr = _run(["lpoptions", "-p", queue_name, "-l"], timeout=10.0)
    raw = stdout + stderr
    options: Dict[str, str] = {}
    for line in stdout.splitlines():
        if ":" in line:
            key, _, rest = line.partition(":")
            # rest may look like: "Custom.WIDTHxHEIGHT/Custom WIDTHxHEIGHT *Letter/Letter ..."
            # extract the currently-selected value (prefixed with *)
            m = re.search(r"\*([^\s/]+)", rest)
            options[key.strip()] = m.group(1) if m else rest.strip()
    if rc != 0 and not options:
        return ToolResult(success=False, error=stderr.strip(), raw=raw)
    return ToolResult(success=True, output={"options": options}, raw=raw)

def cupsenable(queue_name: str) -> ToolResult:
    """Re-enable a stopped CUPS queue via cupsenable.

    Returns output={'enabled': bool}.
    """
    rc, stdout, stderr = _run(["cupsenable", queue_name], timeout=10.0)
    raw = stdout + stderr
    return ToolResult(
        success=rc == 0,
        output={"enabled": rc == 0},
        error=stderr.strip() if rc != 0 else "",
        raw=raw,
    )

def restart_cups() -> ToolResult:
    """Restart the CUPS service via systemctl or launchctl.

    Returns output={'restarted': bool}.
    """
    if _IS_WINDOWS:
        return ToolResult(success=False, error="CUPS restart not applicable on Windows")

    # Try systemctl (Linux)
    rc, stdout, stderr = _run(["systemctl", "restart", "cups"], timeout=20.0)
    if rc == 0:
        return ToolResult(success=True, output={"restarted": True}, raw=stdout + stderr)

    # macOS fallback
    rc2, stdout2, stderr2 = _run(
        ["launchctl", "stop", "org.cups.cupsd"], timeout=10.0
    )
    _run(["launchctl", "start", "org.cups.cupsd"], timeout=10.0)
    raw = stdout + stderr + stdout2 + stderr2
    return ToolResult(success=rc2 == 0, output={"restarted": rc2 == 0}, raw=raw)

def test_print(queue_name: str, file_path: str = "/dev/null") -> ToolResult:
    """Send a test print job to a CUPS queue.

    Uses lp -d <queue> <file>; defaults to /dev/null which generates a blank job
    on most CUPS configurations.
    Returns output={'job_id': str}.
    """
    rc, stdout, stderr = _run(
        ["lp", "-d", queue_name, "-t", "agent-test", file_path], timeout=10.0
    )
    raw = stdout + stderr
    job_id = ""
    m = re.search(r"request id is ([^\s]+)", stdout, re.IGNORECASE)
    if m:
        job_id = m.group(1)
    return ToolResult(
        success=rc == 0,
        output={"job_id": job_id},
        error=stderr.strip() if rc != 0 else "",
        raw=raw,
    )

# ===========================================================================
# Default registry instance
# ===========================================================================

def _build_default_registry() -> ToolRegistry:
    registry = ToolRegistry(
        rate_limiter=RateLimiter(per_printer_per_minute=20, per_tool_per_minute=60),
        redactor=OutputRedactor(enable=True),
        default_timeout=30.0,
    )

    _reg_entries: List[Tuple[ToolSchema, Callable[..., ToolResult]]] = [
        # Network
        (ToolSchema("ping", "ICMP probe", "per_printer", 5.0), ping),
        (ToolSchema("tcp_connect", "TCP port probe", "per_printer", 5.0), tcp_connect),
        (ToolSchema("dns_lookup", "DNS resolution", "per_tool", 5.0), dns_lookup),
        (ToolSchema("arp_lookup", "ARP cache lookup", "per_printer", 5.0), arp_lookup),
        (ToolSchema("oui_vendor", "MAC OUI vendor lookup", "per_tool", 2.0), oui_vendor),
        (ToolSchema("snmp_get", "SNMP GET", "per_printer", 10.0), snmp_get),
        (ToolSchema("snmp_walk", "SNMP WALK", "per_printer", 15.0), snmp_walk),
        # Device
        (ToolSchema("snmp_zt411_status", "ZT411 SNMP status", "per_printer", 15.0), snmp_zt411_status),
        (ToolSchema("snmp_zt411_physical_flags", "ZT411 physical flags", "per_printer", 10.0), snmp_zt411_physical_flags),
        (ToolSchema("snmp_zt411_consumables", "ZT411 consumables", "per_printer", 15.0), snmp_zt411_consumables),
        (ToolSchema("snmp_zt411_alerts", "ZT411 alerts", "per_printer", 10.0), snmp_zt411_alerts),
        (ToolSchema("ipp_get_attributes", "IPP printer attributes", "per_printer", 10.0), ipp_get_attributes),
        # Windows
        (ToolSchema("ps_query_spooler", "Query Spooler service", "per_tool", 10.0), ps_query_spooler),
        (ToolSchema("ps_enum_printers", "Enumerate Windows printers", "per_tool", 15.0), ps_enum_printers),
        (ToolSchema("ps_enum_jobs", "Enumerate print jobs", "per_tool", 10.0), ps_enum_jobs),
        (ToolSchema("ps_get_driver", "Get printer driver info", "per_tool", 10.0), ps_get_driver),
        (ToolSchema("ps_get_event_log", "Read PrintService event log", "per_tool", 20.0), ps_get_event_log),
        (ToolSchema("ps_restart_service", "Restart Windows service", "per_tool", 30.0), ps_restart_service),
        (ToolSchema("ps_cancel_job", "Cancel print job", "per_tool", 15.0), ps_cancel_job),
        (ToolSchema("ps_set_printer_online", "Set printer online", "per_tool", 15.0), ps_set_printer_online),
        # CUPS
        (ToolSchema("lpstat_v", "lpstat -v (list queues)", "per_tool", 10.0), lpstat_v),
        (ToolSchema("lpstat_p", "lpstat -p (printer status)", "per_tool", 10.0), lpstat_p),
        (ToolSchema("lpstat_jobs", "lpstat -o (list jobs)", "per_tool", 10.0), lpstat_jobs),
        (ToolSchema("cups_error_log", "Read CUPS error_log", "per_tool", 5.0), cups_error_log),
        (ToolSchema("lpinfo_m", "lpinfo -m (list drivers)", "per_tool", 20.0), lpinfo_m),
        (ToolSchema("lpoptions", "lpoptions (queue options)", "per_tool", 10.0), lpoptions),
        (ToolSchema("cupsenable", "cupsenable (re-enable queue)", "per_tool", 10.0), cupsenable),
        (ToolSchema("restart_cups", "Restart CUPS service", "per_tool", 25.0), restart_cups),
        (ToolSchema("test_print", "Send test print job", "per_printer", 10.0), test_print),
    ]

    for schema, fn in _reg_entries:
        registry.register(schema, fn)

    return registry

#: Module-level singleton registry — import and use directly in specialists.
registry: ToolRegistry = _build_default_registry()
