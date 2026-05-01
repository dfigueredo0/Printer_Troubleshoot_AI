# Phase 4.0 — Unpause Transport Findings

**Status:** DRAFT — fill in the matrix and the five-question section after
running `scripts/verify_snmp_set.py` against the lab ZT411
(`192.168.99.10` per Phase 1 lab passport).

**Reconnaissance only.** Do NOT modify `tools.py` based on this document.
Session 4.1 owns the implementation; this doc tells 4.1 _which_ mechanism
to implement.

---

## Pre-flight

| Item                                     | Value                                |
| ---------------------------------------- | ------------------------------------ |
| Printer IP (`PRINTER_IP`)                | `192.168.99.10` _(confirm)_          |
| Workstation IP                           | `192.168.99.21` _(confirm)_          |
| Firmware (per Phase 2)                   | `V92.21.39Z`                         |
| Front-panel pause LED **before testing** | on (paused) — re-press between runs  |
| `Get Community` (front-panel printout)   | _community_                          |
| `Set Community` (`WRITE_COMMUNITY`)      | \*private                            |
| `Trap Community`                         | _trapuser_                           |
| SNMP SET enabled in printer config?      | _fill in (if NO → skip mechanism 1)_ |
| Phase 2 baseline `paused: True` returns? | _fill in_                            |

### Pre-flight verification — supplementary findings (2026-04-30)

Re-ran the pre-flight from `192.168.99.21` after drafting this doc; results
materially change the Phase 2.5 reasoning:

| Probe                                                         | Result                                          |
| ------------------------------------------------------------- | ----------------------------------------------- |
| `ping 192.168.99.10` (Windows)                                | 2/2 replies, sub-1ms                            |
| `socket.create_connection((ip, 9100), 3s)` (Windows)          | open                                            |
| `snmp_get sysDescr` (Windows pysnmp, 10s timeout)             | timeout                                         |
| `snmp_get ZBR_STATE_BITMASK` `10642.2.10.3.7.0` (Windows, 3s) | timeout                                         |
| `snmp_zt411_physical_flags` (Windows)                         | `success=False`, `"...timeout"`                 |
| `verify_snmp_set.py --baseline` (Windows)                     | exit 2, "STOP — Phase 2 read regression"        |
| `ping 192.168.99.10` (WSL Ubuntu)                             | 2/2 replies, ~1ms                               |
| `snmpget -v2c -c public -t 5 -r 0 ... sysDescr` (WSL)         | `Timeout: No Response from 192.168.99.10`       |
| `snmpget ... 10642.2.10.3.7.0` (WSL)                          | `Timeout: No Response from 192.168.99.10`       |

**Correction to the Phase 2.5 reasoning below.** The body of this doc
attributes `snmp_zt411_physical_flags` failure to "the `683.*` tree is
empty on this firmware." That reasoning is **incorrect**:
`snmp_zt411_physical_flags` (`tools.py:786`) reads
`ZBR_STATE_BITMASK = "1.3.6.1.4.1.10642.2.10.3.7.0"` — the **Zebra
`10642.*` tree**, not the PWG `683.*` tree. The 683.* tree being absent
does not directly explain why `physical_flags` returns no data.

**The actual cause is broader.** The printer's SNMP agent is currently
silent on UDP/161 entirely — verified from both Windows pysnmp and WSL
Net-SNMP, both with explicit short timeouts. ICMP and TCP/9100 are
healthy. This means **all four `snmp_zt411_*` tools**
(`snmp_zt411_status`, `snmp_zt411_physical_flags`,
`snmp_zt411_consumables`, `snmp_zt411_alerts`) are non-functional against
this printer right now, not just `physical_flags`.

**Operational conclusion is unchanged:** Phase 2.5 swap to `~HS` is
justified, the ZPL pivot is sound (TCP 9100 works), and Session 4.1's
plan stands. **Two follow-ups to track separately, both out of scope for
4.1:**

1. **Why is the printer's SNMP agent silent now?** Session B.6 (2026-04-30
   per CHANGELOG) is recorded as a successful live run. Either SNMP died
   between B.6 and now (printer web UI toggle? firmware quirk?), or B.6
   was actually run in `--dry-run` mode (which stubs SNMP per
   `session_b6_live_loop.py:235–287`) and SNMP was silently broken
   during B.6 too. Check B.6's mode flag and printer's web UI SNMP
   settings before assuming a regression.
2. **Survey the other three `snmp_zt411_*` call sites.** 4.1 only swaps
   `physical_flags` for `~HS`. The other three may need ZPL alternatives
   or fault-tolerant fallbacks; track as a Phase 2.6 follow-up.

---

## Decision matrix

Re-press the front-panel PAUSE button between every mechanism. Otherwise a
later run "succeeds" only because an earlier one already unpaused the
printer.

| Mechanism                      | OID / payload                                    | Worked? | Front-panel LED off? | Notes                                              |
| ------------------------------ | ------------------------------------------------ | ------- | -------------------- | -------------------------------------------------- |
| 1a. SNMP SET, prompt OID       | `1.3.6.1.4.1.683.6.2.3.4.1.7.0` Integer(0)       |         |                      | community used; errInd / errStat                   |
| 1b. SNMP SET, Zebra-enterprise | `1.3.6.1.4.1.10642.6.22.0` Integer(0) (longshot) |         |                      | Zebra tree responds for reads; write is unverified |
| 2. SGD via TCP 9100            | `! U1 setvar "device.pause" "off"\r\n`           |         |                      | port 9100 reachable?                               |
| 3. ZPL via TCP 9100            | `~PS`                                            |         |                      | port 9100 reachable? Zebra-recommended path        |

> **Note on mechanism 1.** The Phase 4.0 prompt referred to OID
> `1.3.6.1.4.1.683.6.2.3.4.1.7.0` as `ZT411OIDs.ZBR_PAUSED`. This
> contradicts `tools.py:237`, which sets `ZBR_PAUSED = None` because no
> dedicated SNMP OID for pause is implemented on this firmware. The
> prompt's OID lives in the Printer Working Group enterprise tree
> (`1.3.6.1.4.1.683.*`); `tools.py:210–215` already documents that the
> standard Printer-MIB at `1.3.6.1.2.1.43.*` is not implemented on this
> firmware, which is weak prior evidence the PWG tree is also empty.
> Test it anyway — that's the recon — but don't be surprised by
> `noSuchName`. Mechanism 1b is a longshot in the Zebra tree we know
> answers, just to rule out "writes work, just not on the prompt's OID."

---

## Five-question writeup

### 1. Which mechanism does `snmp_zt411_unpause` use in Session 4.1?

_Pick the highest-fit mechanism that worked. Preference order if multiple
work: SNMP SET (matches existing `tools.py` shape) > ZPL `~PS` (most
robust, Zebra-recommended) > SGD (verbose, no win over the others)._

Selected: **\<SNMP SET | ZPL ~PS | SGD\>**

The function name stays `snmp_zt411_unpause` for the 4.1 plan even if
the implementation is not actually SNMP — the _interface_ is what the
agent loop sees, and renaming late costs more than the slight misnomer.
Add a docstring noting the actual transport.

### 2. What credential or channel state does it need?

*If SNMP SET: the write community string (record it here, but do not
commit secrets — store the value in env / lab notes, just record the
*name* of the env var here). If 9100: just the port being open.*

Required:

- _fill in_

Connection-prep step (the agent's pre-action check) should verify this
state before attempting unpause.

### 3. What's the read-back verification?

After firing the unpause, re-run `snmp_zt411_physical_flags` and confirm
`paused: False`.

Observed delay between front-panel state change and SNMP-visible flag
change: **\<X seconds\>**.

Implication for Session 4.1: success-criteria check needs a small
retry loop (≥ \<X+1\> seconds total budget), not a single read. The
existing `verify_snmp_set.py --readback` mode demonstrates the loop
shape (3 attempts, 1 s delay).

### 4. What happens if the printer is paused for a real reason and we send unpause?

Test: induce a real fault (lift the printhead — that's the lowest-impact
fault we can reverse), let the printer pause itself, then send the
winning unpause command.

Observed: _fill in — the printer should refuse to leave pause while a
fault is active. If it leaves pause and immediately re-pauses, that's
also acceptable safety behavior. If it leaves pause and stays unpaused
with the fault still active, that's a problem and we need to add an
explicit pre-check for active-fault state in 4.1._

### 5. One-line risk statement

_Fill in once 1–4 are validated. Template:_

> Unpause is safe because (a) the printer refuses to leave pause while a
> real fault is active, (b) no media is consumed by unpausing, and (c)
> the action is trivially reversible by pressing pause again.

---

## Stop conditions encountered

_If any of the following triggered during this session, record here and
do NOT proceed to Session 4.1 until resolved:_

- [ ] Pre-flight step 3 failed (Phase 2 read regression). File a
      separate issue.
- [ ] All three mechanisms failed. Re-scope Phase 4 to "recommend-only";
      `snmp_zt411_unpause` does not get built.
- [ ] Printer entered an unrecoverable state. Hand off to the lab
      printer owner; do not try more writes.

---

## Session 4.1 handoff line

Copy the following line (with `<TRANSPORT>` and `<DATE>` filled in) to the
top of the Session 4.1 design doc when you write it:

> **Unpause transport: `<SNMP SET | ZPL ~PS | SGD>`. Verification: `<YYYY-MM-DD>`.
> See `docs/phase4/snmp_set_findings.md`.**
