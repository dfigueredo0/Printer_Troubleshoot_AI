"""
Owns: “did it work?” and “are we allowed to do that?”
Enforces guardrails: read-only first, confirmation for destructive actions, rate limits, privilege checks.
Validates outcomes with observable signals: queue drained + successful test print + device “ready”.
Produces audit trail: decision → evidence → action → result, with citations.
Detects hallucination risk: refuses to claim success without tool output or doc snippet.
Evidence: before/after diffs of state snapshot, tool outputs, confirmation tokens.
"""