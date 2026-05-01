"""
cost_tracking.py — in-script API spend guardrail.

Leaf utility (no orchestrator / planner / agent imports). Used by the
Session B.6 live loop to bound cumulative Anthropic API spend per
session and abort cleanly before any call that would push spend above
the configured limit.

Design notes
------------
* Pricing rates are stored as per-token (rates / 1_000_000 at definition
  time) so the per-call math is just multiplication. Easy to reason
  about, no rounding surprises.
* ``cost_usd`` and ``remaining_usd`` are properties that recompute from
  the running token totals every call. They are NOT stored fields, so
  there is no way to desync cost from tokens.
* ``record()`` and ``check_or_raise()`` are deliberately separate:
  ``record(usage)`` is called AFTER each API call (to update totals),
  ``check_or_raise()`` is called BEFORE the next call (to abort cleanly
  before another billable request). Combining them would mean the abort
  fires mid-iteration with partial state.
* Unknown model names fall back to Opus pricing — always over-estimate
  cost rather than under, so the guardrail is conservative.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pricing — published per-million-token rates, divided to per-token at
# definition time. Source: Anthropic public price sheet (2026-04).
# ---------------------------------------------------------------------------

_M = 1_000_000.0

# (input_per_token_usd, output_per_token_usd)
_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-7":    (15.0 / _M, 75.0 / _M),
    "claude-opus-4-6":    (15.0 / _M, 75.0 / _M),
    "claude-sonnet-4-6":  ( 3.0 / _M, 15.0 / _M),
    "claude-haiku-4-5":   ( 1.0 / _M,  5.0 / _M),
}

# Fallback for unknown models — Opus rates so we always over-estimate.
_FALLBACK_RATES: tuple[float, float] = _PRICING["claude-opus-4-7"]


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Per-call cost estimate from token counts.

    Returns USD as a float. Unknown model names fall back to Opus
    pricing (conservative — we'd rather over-charge our own budget than
    silently undercount).
    """
    rates = _PRICING.get(model, _FALLBACK_RATES)
    in_rate, out_rate = rates
    return in_rate * input_tokens + out_rate * output_tokens


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class SessionBudgetExceeded(Exception):
    """Raised when cumulative session spend reaches the configured limit."""


# ---------------------------------------------------------------------------
# SessionBudget
# ---------------------------------------------------------------------------


@dataclass
class SessionBudget:
    """Cumulative spend tracker with a hard upper bound.

    Construct once per session, pass ``record`` as the planner's
    ``on_usage`` callback, and call ``check_or_raise()`` before each
    iteration.
    """

    model: str
    limit_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    call_count: int = 0

    # ------------------------------------------------------------------
    # Cost properties (recomputed every access)
    # ------------------------------------------------------------------

    @property
    def cost_usd(self) -> float:
        return estimate_cost_usd(self.model, self.input_tokens, self.output_tokens)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.limit_usd - self.cost_usd)

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def record(self, usage: Any) -> None:
        """Add the input/output tokens of one API call to the running totals.

        Accepts any object with ``input_tokens`` and ``output_tokens``
        integer attributes — the Anthropic SDK's ``response.usage`` type,
        a ``types.SimpleNamespace`` (used by tests), or a custom shim
        that wraps the JSON ``usage`` dict from a raw httpx response.
        """
        in_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        out_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        self.input_tokens += in_tokens
        self.output_tokens += out_tokens
        self.call_count += 1

    # ------------------------------------------------------------------
    # Limit checks
    # ------------------------------------------------------------------

    def is_over_limit(self) -> bool:
        return self.cost_usd >= self.limit_usd

    def check_or_raise(self) -> None:
        """Raise ``SessionBudgetExceeded`` when cumulative spend has hit
        the limit. Safe to call before every API call.
        """
        if self.is_over_limit():
            raise SessionBudgetExceeded(
                f"Session budget exhausted: spent ${self.cost_usd:.4f} / "
                f"limit ${self.limit_usd:.4f} after {self.call_count} call(s) "
                f"on model {self.model}"
            )

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def log_summary(self) -> None:
        logger.info(
            "[budget] model=%s calls=%d in_tokens=%d out_tokens=%d "
            "cost=$%.4f / limit=$%.4f remaining=$%.4f",
            self.model,
            self.call_count,
            self.input_tokens,
            self.output_tokens,
            self.cost_usd,
            self.limit_usd,
            self.remaining_usd,
        )
