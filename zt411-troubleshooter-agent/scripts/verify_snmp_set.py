"""
Phase 4.0 — SNMP SET / unpause-transport verification.

Reconnaissance script: try three different mechanisms for programmatically
unpausing a Zebra ZT411 from the workstation. Determine which (if any) the
lab printer accepts so Session 4.1 can pick the right transport for
``snmp_zt411_unpause`` *before* committing to a tool signature.

This script is THROWAWAY. Delete or move to ``scripts/_archive/`` after
Session 4.1 lands and ``snmp_zt411_unpause`` is in tools.py.

Usage::

    # Pre-flight: confirm the existing read tool sees `paused: True`
    python scripts/verify_snmp_set.py --ip 192.168.99.10 --baseline

    # Run one mechanism at a time (re-press front-panel PAUSE between runs)
    python scripts/verify_snmp_set.py --ip 192.168.99.10 --mechanism 1 \
        --write-community private
    python scripts/verify_snmp_set.py --ip 192.168.99.10 --mechanism 2
    python scripts/verify_snmp_set.py --ip 192.168.99.10 --mechanism 3

    # After firing a mechanism, re-read state programmatically
    python scripts/verify_snmp_set.py --ip 192.168.99.10 --readback

The ground-truth success criterion is the printer's front-panel pause
LED going off, NOT the SNMP read-back (which can lag 1–3 s on this
firmware). The ``--readback`` step is a convenience confirmation only.

Important note on Mechanism 1's OID
-----------------------------------
The Phase 4.0 prompt instructs us to test SET against
``1.3.6.1.4.1.683.6.2.3.4.1.7.0`` and refers to it as
``ZT411OIDs.ZBR_PAUSED``. Two things to flag:

* ``tools.py:237`` defines ``ZBR_PAUSED = None`` — there is no dedicated
  SNMP OID for pause state on this firmware. Pause is detected via the
  state bitmask + alert table cross-check (see
  ``snmp_zt411_physical_flags``).
* ``1.3.6.1.4.1.683.*`` is the Printer Working Group enterprise tree.
  The ``ZT411OIDs`` block comment (lines 210–215) says the standard
  Printer-MIB at ``1.3.6.1.2.1.43.*`` is not implemented; the PWG
  enterprise tree is in the same family and we have weak prior evidence
  it would respond.

We test the OID anyway — the whole point of this session is to find out
empirically — and the script also tries the Zebra enterprise OID at
``1.3.6.1.4.1.10642.6.22.0`` (``ZBR_ANY_FAULT``) as a longshot since it
at least lives in the tree this firmware *does* answer. Neither is
expected to work; mechanism 3 (ZPL ~PS) is the highest-prior win.
"""
from __future__ import annotations

import argparse
import asyncio
import socket
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

# Make the agent package importable when running from the repo root or
# from scripts/ — same pattern as session_b6_live_loop.py.
_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from zt411_agent.agent.tools import snmp_zt411_physical_flags  # noqa: E402

# Mechanism 1 candidate OIDs, in test order.
PROMPT_PAUSED_OID = "1.3.6.1.4.1.683.6.2.3.4.1.7.0"   # from Phase 4.0 prompt
ZEBRA_ANY_FAULT_OID = "1.3.6.1.4.1.10642.6.22.0"       # in tree we know responds


# ---------------------------------------------------------------------------
# Read-back helper (uses the existing Phase-2 read tool)
# ---------------------------------------------------------------------------

def readback(ip: str, community: str = "public", retries: int = 3,
             delay_s: float = 1.0) -> Optional[bool]:
    """Re-read paused flag with a small retry loop.

    Returns the final ``paused`` value, or None if the read failed
    every time. Some firmwares delay updating the SNMP-visible state
    1–3 s after the front-panel state changes, so we poll briefly.
    """
    last_paused: Optional[bool] = None
    for attempt in range(1, retries + 1):
        r = snmp_zt411_physical_flags(ip, community)
        if r.success and r.output is not None:
            last_paused = r.output.get("paused")
            print(f"  [readback {attempt}/{retries}] paused={last_paused} "
                  f"raw_bitmask={r.output.get('raw_bitmask')!r}")
            if last_paused is False:
                return False
        else:
            print(f"  [readback {attempt}/{retries}] FAILED: {r.error}")
        if attempt < retries:
            time.sleep(delay_s)
    return last_paused


def baseline_check(ip: str, community: str = "public") -> int:
    """Pre-flight: confirm the read tool sees the printer paused.

    Exit code: 0 if paused, 1 if not paused, 2 if read failed.
    """
    print(f"[baseline] reading physical flags from {ip} (community={community!r})")
    r = snmp_zt411_physical_flags(ip, community)
    if not r.success or r.output is None:
        print(f"[baseline] FAILED: {r.error}")
        print("[baseline] STOP — Phase 2 read regression. Investigate before continuing.")
        return 2
    paused = r.output.get("paused")
    print(f"[baseline] paused={paused} | raw_bitmask={r.output.get('raw_bitmask')!r}")
    print(f"[baseline] full output: {r.output}")
    if paused is True:
        print("[baseline] OK — printer is paused as expected. Proceed to mechanism tests.")
        return 0
    print("[baseline] WARNING — printer is NOT paused. Press the front-panel PAUSE button.")
    return 1


# ---------------------------------------------------------------------------
# Mechanism 1 — SNMP SET
# ---------------------------------------------------------------------------

async def _snmp_set(ip: str, oid: str, community: str,
                    value_int: int, timeout_s: int = 5) -> Tuple[Optional[str], Optional[str]]:
    """SNMPv2c SET an Integer value on a single OID. Returns (errInd, errStat)."""
    from pysnmp.hlapi.v3arch.asyncio import (
        CommunityData,
        ContextData,
        Integer,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        set_cmd,
    )
    transport = await UdpTransportTarget.create(
        (ip, 161), timeout=timeout_s, retries=1
    )
    err_ind, err_stat, err_idx, var_binds = await set_cmd(
        SnmpEngine(),
        CommunityData(community, mpModel=1),
        transport,
        ContextData(),
        ObjectType(ObjectIdentity(oid), Integer(value_int)),
    )
    err_ind_s = str(err_ind) if err_ind else None
    err_stat_s = err_stat.prettyPrint() if err_stat and int(err_stat) != 0 else None
    for vb in var_binds:
        print(f"  varBind: {vb.prettyPrint()}")
    return err_ind_s, err_stat_s


def mechanism_1(ip: str, write_community: str) -> None:
    """Try SNMP SET on the Phase-4.0-prompt OID, then on a Zebra-enterprise fallback."""
    candidates = [
        ("prompt-PWG", PROMPT_PAUSED_OID),
        ("zebra-any-fault", ZEBRA_ANY_FAULT_OID),
    ]
    for label, oid in candidates:
        print(f"\n[mechanism-1:{label}] SET {oid} = Integer(0)  community={write_community!r}")
        try:
            err_ind, err_stat = asyncio.run(
                _snmp_set(ip, oid, write_community, 0)
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  EXCEPTION: {exc!r}")
            continue
        print(f"  errInd={err_ind!r}  errStat={err_stat!r}")
        if err_ind is None and err_stat is None:
            print("  -> SET accepted at protocol level. WALK TO THE PRINTER:")
            print("     did the front-panel pause LED go off? (record in findings.md)")
        elif err_stat in ("noSuchName", "noAccess"):
            print("  -> OID not writable on this firmware. Move on.")
        elif err_stat in ("authorizationError", "genErr"):
            print("  -> community/ACL rejection. If you used a non-default community,")
            print("     re-run with --write-community private as a sanity check.")
        else:
            print("  -> other failure (timeout / parse). Move on.")


# ---------------------------------------------------------------------------
# Mechanism 2 — SGD over raw TCP 9100
# ---------------------------------------------------------------------------

def mechanism_2(ip: str, port: int = 9100, timeout_s: float = 5.0) -> None:
    """Send Zebra SGD ``setvar device.pause off`` over raw TCP 9100."""
    cmd = b'! U1 setvar "device.pause" "off"\r\n'
    print(f"\n[mechanism-2] SGD setvar device.pause off -> {ip}:{port}")
    print(f"  payload: {cmd!r}")
    try:
        with socket.create_connection((ip, port), timeout=timeout_s) as s:
            s.sendall(cmd)
        print("  -> sent. SGD is fire-and-forget; no response expected.")
        print("  -> WALK TO THE PRINTER: did the pause LED go off within ~2 s?")
    except (socket.timeout, ConnectionRefusedError, OSError) as exc:
        print(f"  -> port {port} unreachable: {exc!r}")
        print("  -> mechanism 2 unavailable. Skip.")


# ---------------------------------------------------------------------------
# Mechanism 3 — ZPL ~PS over raw TCP 9100
# ---------------------------------------------------------------------------

def mechanism_3(ip: str, port: int = 9100, timeout_s: float = 5.0) -> None:
    """Send ZPL ``~PS`` (Print Start / resume from pause) over raw TCP 9100."""
    cmd = b'~PS'
    print(f"\n[mechanism-3] ZPL ~PS -> {ip}:{port}")
    print(f"  payload: {cmd!r}")
    try:
        with socket.create_connection((ip, port), timeout=timeout_s) as s:
            s.sendall(cmd)
        print("  -> sent. ZPL is fire-and-forget; no response expected.")
        print("  -> WALK TO THE PRINTER: did the pause LED go off within ~2 s?")
    except (socket.timeout, ConnectionRefusedError, OSError) as exc:
        print(f"  -> port {port} unreachable: {exc!r}")
        print("  -> mechanism 3 unavailable. If 9100 is closed, ALL writes")
        print("     for this lab go through SNMP only. Highly unusual for Zebra.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Phase 4.0 unpause-transport reconnaissance. "
                    "Re-pause the printer between mechanism runs.",
    )
    p.add_argument("--ip", required=True,
                   help="Printer IP (e.g. 192.168.99.10).")
    p.add_argument("--community", default="public",
                   help="SNMP read community for baseline/readback (default: public).")
    p.add_argument("--write-community", default="private",
                   help="SNMP write community for mechanism 1 "
                        "(default: private — capture from front panel printout).")
    p.add_argument("--mechanism", type=int, choices=(1, 2, 3),
                   help="Run a single mechanism (1=SNMP SET, 2=SGD, 3=ZPL ~PS).")
    p.add_argument("--baseline", action="store_true",
                   help="Pre-flight: confirm read tool sees paused=True.")
    p.add_argument("--readback", action="store_true",
                   help="After firing a mechanism, re-read paused state with retry.")
    args = p.parse_args()

    if not (args.baseline or args.readback or args.mechanism):
        p.error("specify at least one of --baseline / --mechanism N / --readback")

    if args.baseline:
        rc = baseline_check(args.ip, args.community)
        if rc == 2:
            return rc

    if args.mechanism == 1:
        mechanism_1(args.ip, args.write_community)
    elif args.mechanism == 2:
        mechanism_2(args.ip)
    elif args.mechanism == 3:
        mechanism_3(args.ip)

    if args.readback:
        print("\n[readback] re-reading paused flag with 3-attempt retry...")
        result = readback(args.ip, args.community)
        print(f"[readback] final paused={result}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
