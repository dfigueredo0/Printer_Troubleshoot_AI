"""
Phase 3 — Session B, Step 3
WindowsSpecialist isolated smoke test.

Exercises WindowsSpecialist.act() against the local print spooler on
this Windows host (no monkeypatching, no fixtures). The specialist
talks to the local Spooler service via PowerShell (ps_query_spooler,
ps_enum_printers, ps_enum_jobs, ps_get_driver, ps_get_event_log) — it
does not take an IP for itself; it inspects whichever host it runs on.

Pass/fail criterion: state.windows_info has at least one populated
field (spooler_status or printers list) AND no Python exceptions
were raised. ToolResult(success=False) failures from individual ps_*
tools are acceptable here — they exercise the error path; production
hardening of those tools is a Phase 4 problem.

Run from the package root:
    python scripts/smoke_windows_specialist.py
"""
from __future__ import annotations

import json
import sys
from pprint import pformat

from zt411_agent.agent.windows_specialist import WindowsSpecialist
from zt411_agent.state import AgentState, OSPlatform, DeviceInfo


def main() -> int:
    print("=" * 72)
    print("WindowsSpecialist isolated smoke test")
    print("=" * 72)

    state = AgentState(
        session_id="smoke_windows_specialist",
        os_platform=OSPlatform.WINDOWS,
        symptoms=["printer paused"],
    )
    state.device = DeviceInfo(ip="192.168.99.10")

    specialist = WindowsSpecialist()

    print(f"\nutility score before act(): {specialist.can_handle(state):.3f}")
    print(f"initial windows_info: {state.windows.model_dump()}")

    try:
        result = specialist.act(state)
    except Exception as exc:  # noqa: BLE001
        print(f"\n!!! WindowsSpecialist.act() raised: {exc!r}")
        import traceback
        traceback.print_exc()
        return 2

    next_state: AgentState = result.get("next_state", state)

    print("\n" + "-" * 72)
    print("state.windows_info (after act):")
    print("-" * 72)
    print(pformat(next_state.windows.model_dump(), width=100))

    print("\n" + "-" * 72)
    print(f"state.evidence ({len(next_state.evidence)} item(s)):")
    print("-" * 72)
    for ev in next_state.evidence:
        content = ev.content if len(ev.content) <= 200 else ev.content[:200] + "..."
        print(f"  [{ev.evidence_id}] {ev.specialist} :: {ev.source}")
        print(f"      {content}")

    print("\n" + "-" * 72)
    print(f"state.action_log ({len(next_state.action_log)} entry(ies)):")
    print("-" * 72)
    for a in next_state.action_log:
        print(f"  [{a.entry_id}] {a.specialist} risk={a.risk.value} status={a.status.value}")
        print(f"      action: {a.action}")
        if a.result:
            print(f"      result: {a.result}")

    # Pass/fail summary
    win = next_state.windows
    populated_fields = [
        f for f in (
            ("spooler_running", win.spooler_running is not None),
            ("queue_name",      bool(win.queue_name)),
            ("driver_name",     bool(win.driver_name)),
            ("event_log",       bool(win.event_log_errors) or any(
                ev.source == "event_log" for ev in next_state.evidence
            )),
        )
        if f[1]
    ]

    print("\n" + "=" * 72)
    if populated_fields:
        print(f"PASS — {len(populated_fields)} field(s) populated: "
              f"{[f[0] for f in populated_fields]}")
        return 0
    print("FAIL — no fields in state.windows_info populated; "
          "WindowsSpecialist may not be reaching the local spooler.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
