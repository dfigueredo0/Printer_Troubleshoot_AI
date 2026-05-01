# Session B.6 — live-run handoff

Run this on the workstation with the live ZT411 reachable at
`192.168.99.10`. Claude Code completes the script; the human runs the
interactive live verification because the loop prompts on stdin for
front-panel button presses.

## Pre-run checklist

Verify all of these before invoking the script:

1. **Anthropic account has credit.** Go to Console → Plans & Billing
   and confirm the balance is non-zero. The Evaluation-access starter
   credit is $5; per-run cost on Sonnet 4.6 is roughly $0.02–0.05, so
   $1+ remaining is plenty for one live run. **If the balance is $0,
   the smoke check will fail with a `credit_balance` error and the
   live run cannot proceed.**

2. **Printer is healthy and idle.** Display shows `READY`, no fault
   indicators, not currently paused. From the workstation:

       Test-Connection 192.168.99.10 -Count 2

3. **`ANTHROPIC_API_KEY` is exported in the current shell.** Either
   `export ANTHROPIC_API_KEY=...` (bash) / `$env:ANTHROPIC_API_KEY=...`
   (PowerShell), or invoke via dotenv:

       .\.venv\Scripts\dotenv.exe -f .env run -- <command>

4. **Inside the active venv** at `Printer_Troubleshoot_AI\.venv`.

## Smoke check (recommended first)

Before the interactive live run, sanity-check the cloud-tier path with
a single 1-token API call (~$0.00002):

    python scripts\session_b6_live_loop.py --smoke-check

Expected: prints `smoke check passed`, exits 0. If it prints a
`credit_balance` HINT, top up credit and re-run. If it prints an
`auth` or `model_permission` HINT, address that first — the live run
will fail in the same way.

## The live run

    python scripts\session_b6_live_loop.py --budget-limit 0.10

What happens, in order:

1. Script confirms idle baseline via SNMP. Aborts if not idle.
2. Script prompts: *"Press PAUSE on the front panel. Press Enter when
   the pause LED is lit."*
3. **Human action:** press PAUSE on the printer, then Enter in the
   terminal.
4. Script verifies pause via SNMP. If not detected, prompts to retry
   once before aborting.
5. Script runs the orchestrator with the cloud-tier planner
   (Sonnet 4.6). `[budget]` lines stream after each loop step so live
   spend is visible in real time.
6. Loop terminates (success or short-circuit; both are acceptable).
7. Script prompts: *"Press PAUSE again to resume. Press Enter when
   ready."*
8. **Human action:** press PAUSE on the printer, then Enter.
9. Script confirms idle restored, prints final cost summary and log
   file path.

## What success looks like

All of:

- `planner_citations_evidence_present: True` in the acceptance review
  block.
- `citation_count >= 1`.
- `cited_snippet_ids` are real `snippet_id` strings that appear in the
  retrieved snippets that iteration (not hallucinated identifiers).
- `cloud_tier_engaged: True` (i.e., the planner did not silently
  downgrade to Ollama / tier0).
- `budget.cost_usd` strictly less than `budget.limit_usd`.
- Exit code 0.

## Where to find the log

`tests/logs/session_b6_<YYYYMMDD-HHMMSS>.log` — the path is printed at
the end of the run. The log captures every `[budget]` line, the full
orchestrator trace, and the acceptance review.

## Failure handling

If the run fails or shows unexpected output, **do not re-run blindly**.
Open the log file and look for:

- The first `Runtime tier ... by config` / `Planner configured` lines —
  confirm `tier=tier2 model=claude-sonnet-4-6`.
- Any `Cloud planner failed, trying Ollama` warning — that means the
  cloud call 400/401/403'd. Check `--smoke-check` again.
- The `Acceptance review:` block — note which assertion failed and
  read upward for the cause.

Report back with the log path and the failing assertion.

## Retry budget

If the first run produces no citations, re-attempt up to 2 more times
(3 total). After the 3rd, stop and report the log paths so we can
diagnose offline. We are not chasing flakes.

## Updating the CHANGELOG after the live run

Once the live run succeeds, edit `CHANGELOG.md` under the Session B.6
entry:

- Move the `Pending human verification` block content into `Verified`,
  filling in: actual citation count, log path, final
  `budget.cost_usd`.
- Update the `Cost` section with the real spend, model used, and call
  count from the log.
- In the existing Session B `Resolved in Session B.6` sub-entry,
  append the log path so the historical chain stays linkable.

If the run fails after 3 attempts, move the block into `Outstanding`
instead and document what was observed.
