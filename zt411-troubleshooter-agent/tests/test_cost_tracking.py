"""
Unit tests for ``zt411_agent.cost_tracking``.

Pure-arithmetic + simple-state tests. No HTTP, no orchestrator wiring;
the SDK usage object is mocked with ``types.SimpleNamespace``.
"""
from __future__ import annotations

import types

import pytest

from zt411_agent.cost_tracking import (
    SessionBudget,
    SessionBudgetExceeded,
    estimate_cost_usd,
)


# ---------------------------------------------------------------------------
# estimate_cost_usd
# ---------------------------------------------------------------------------


class TestEstimateCostUsd:
    """Per-model rate arithmetic. Float multiplications are exact for
    these magnitudes — no tolerance / approx needed.
    """

    def test_opus_4_7_pricing(self):
        # Opus 4.7: $15/M input, $75/M output
        cost = estimate_cost_usd("claude-opus-4-7", input_tokens=1000, output_tokens=500)
        expected = (15.0 / 1_000_000) * 1000 + (75.0 / 1_000_000) * 500
        assert cost == expected
        assert cost == 0.015 + 0.0375

    def test_opus_4_6_pricing(self):
        cost = estimate_cost_usd("claude-opus-4-6", input_tokens=2000, output_tokens=1000)
        expected = (15.0 / 1_000_000) * 2000 + (75.0 / 1_000_000) * 1000
        assert cost == expected

    def test_sonnet_4_6_pricing(self):
        # Sonnet 4.6: $3/M input, $15/M output
        cost = estimate_cost_usd("claude-sonnet-4-6", input_tokens=1000, output_tokens=500)
        expected = (3.0 / 1_000_000) * 1000 + (15.0 / 1_000_000) * 500
        assert cost == expected

    def test_haiku_4_5_pricing(self):
        # Haiku 4.5: $1/M input, $5/M output
        cost = estimate_cost_usd("claude-haiku-4-5", input_tokens=1000, output_tokens=500)
        expected = (1.0 / 1_000_000) * 1000 + (5.0 / 1_000_000) * 500
        assert cost == expected

    def test_zero_tokens_zero_cost(self):
        assert estimate_cost_usd("claude-sonnet-4-6", 0, 0) == 0.0

    def test_unknown_model_falls_back_to_opus(self):
        """Conservative fallback: unknown models use Opus pricing so the
        guardrail never silently undercounts."""
        unknown = estimate_cost_usd("claude-future-7-9", 1000, 500)
        opus = estimate_cost_usd("claude-opus-4-7", 1000, 500)
        assert unknown == opus

    def test_unknown_model_more_expensive_than_sonnet(self):
        """Sanity check on the conservative-fallback choice."""
        unknown = estimate_cost_usd("totally-made-up-model", 1000, 1000)
        sonnet = estimate_cost_usd("claude-sonnet-4-6", 1000, 1000)
        assert unknown > sonnet


# ---------------------------------------------------------------------------
# SessionBudget.record
# ---------------------------------------------------------------------------


def _usage(input_tokens: int, output_tokens: int) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


class TestSessionBudgetRecord:
    def test_initial_state_is_zero(self):
        b = SessionBudget(model="claude-sonnet-4-6", limit_usd=0.10)
        assert b.input_tokens == 0
        assert b.output_tokens == 0
        assert b.call_count == 0
        assert b.cost_usd == 0.0

    def test_record_accumulates_tokens_and_call_count(self):
        b = SessionBudget(model="claude-sonnet-4-6", limit_usd=1.00)
        b.record(_usage(100, 50))
        b.record(_usage(200, 80))
        b.record(_usage(50, 25))
        assert b.input_tokens == 350
        assert b.output_tokens == 155
        assert b.call_count == 3

    def test_cost_usd_property_recomputes_from_tokens(self):
        b = SessionBudget(model="claude-sonnet-4-6", limit_usd=1.00)
        b.record(_usage(1000, 500))
        # Sonnet: $3/M in + $15/M out, computed via the same path the
        # property uses so float arithmetic is identical.
        single_call = estimate_cost_usd("claude-sonnet-4-6", 1000, 500)
        assert b.cost_usd == single_call
        b.record(_usage(1000, 500))
        # Doubled
        assert b.cost_usd == estimate_cost_usd("claude-sonnet-4-6", 2000, 1000)

    def test_remaining_usd_property(self):
        b = SessionBudget(model="claude-sonnet-4-6", limit_usd=0.05)
        b.record(_usage(1000, 500))  # cost = 0.0105
        assert b.remaining_usd == pytest.approx(0.05 - 0.0105, abs=1e-12)

    def test_remaining_usd_clamped_to_zero(self):
        b = SessionBudget(model="claude-sonnet-4-6", limit_usd=0.001)
        b.record(_usage(10_000, 5_000))  # well over limit
        assert b.remaining_usd == 0.0

    def test_record_handles_missing_attrs_as_zero(self):
        """Defensive: a partially-populated usage shape (e.g. an empty
        dict that was wrapped in SimpleNamespace) shouldn't crash."""
        b = SessionBudget(model="claude-sonnet-4-6", limit_usd=0.10)
        b.record(types.SimpleNamespace())
        assert b.input_tokens == 0
        assert b.output_tokens == 0
        assert b.call_count == 1


# ---------------------------------------------------------------------------
# SessionBudget.is_over_limit + check_or_raise
# ---------------------------------------------------------------------------


class TestSessionBudgetLimit:
    def test_under_limit_does_not_raise(self):
        b = SessionBudget(model="claude-sonnet-4-6", limit_usd=0.10)
        b.record(_usage(100, 50))
        assert not b.is_over_limit()
        b.check_or_raise()  # should be a no-op

    def test_is_over_limit_flips_at_threshold(self):
        """The check is `>=` per spec — exactly at the limit counts as over."""
        # Construct tokens that hit the limit exactly. Sonnet rates:
        # 3/M input + 15/M output. 1000 in + 0 out -> $0.003. Limit = $0.003.
        b = SessionBudget(
            model="claude-sonnet-4-6",
            limit_usd=0.003,
        )
        b.record(_usage(1000, 0))
        assert b.cost_usd == 0.003
        assert b.is_over_limit() is True
        with pytest.raises(SessionBudgetExceeded):
            b.check_or_raise()

    def test_is_over_limit_strictly_above(self):
        b = SessionBudget(model="claude-sonnet-4-6", limit_usd=0.001)
        b.record(_usage(10_000, 5_000))
        assert b.is_over_limit() is True

    def test_check_or_raise_message_contains_diagnostics(self):
        """The exception message must surface enough information to
        diagnose the abort without re-deriving anything from logs."""
        b = SessionBudget(model="claude-sonnet-4-6", limit_usd=0.001)
        b.record(_usage(2000, 1000))   # $0.006 + $0.015 = $0.021
        with pytest.raises(SessionBudgetExceeded) as excinfo:
            b.check_or_raise()
        msg = str(excinfo.value)
        assert "claude-sonnet-4-6" in msg
        assert "0.001" in msg                 # limit
        assert "1" in msg                     # call_count
        assert "$" in msg                     # currency marker
        # Cost was 0.021 — exception should mention it (some
        # representation of 0.021 must appear).
        assert "0.0210" in msg or "0.021" in msg

    def test_log_summary_does_not_raise(self, caplog):
        """log_summary is best-effort logging — it should never throw
        even on a freshly-constructed budget with zero tokens."""
        import logging
        caplog.set_level(logging.INFO, logger="zt411_agent.cost_tracking")
        b = SessionBudget(model="claude-sonnet-4-6", limit_usd=0.10)
        b.log_summary()
        b.record(_usage(1000, 500))
        b.log_summary()
        # A pair of [budget] log records should have been emitted.
        budget_records = [
            r for r in caplog.records if "[budget]" in r.getMessage()
        ]
        assert len(budget_records) == 2
