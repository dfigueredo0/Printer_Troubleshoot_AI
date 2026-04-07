"""
metrics.py — Evaluation metrics for the ZT411 troubleshooter agent.

Public functions
----------------
compute_accuracy(y_true, y_pred)            → float
compute_precision_recall_f1(y_true, y_pred) → dict
compute_diagnosis_metrics(sessions)         → dict
compute_safety_metrics(sessions)            → dict

`sessions` is a list of dicts with the schema produced by eval.py / sample_cases.jsonl.
"""

from __future__ import annotations

from typing import Any, Sequence


# ---------------------------------------------------------------------------
# Basic classification metrics
# ---------------------------------------------------------------------------


def compute_accuracy(y_true: Sequence[Any], y_pred: Sequence[Any]) -> float:
    """
    Fraction of predictions that exactly match the ground-truth labels.

    Parameters
    ----------
    y_true : Ground-truth labels (any comparable type).
    y_pred : Predicted labels.

    Returns
    -------
    float in [0.0, 1.0]; 0.0 if both sequences are empty.
    """
    if len(y_true) != len(y_pred):
        raise ValueError(
            f"y_true and y_pred must have the same length "
            f"(got {len(y_true)} and {len(y_pred)})"
        )
    if not y_true:
        return 0.0
    correct = sum(t == p for t, p in zip(y_true, y_pred))
    return correct / len(y_true)


def compute_precision_recall_f1(
    y_true: Sequence[Any],
    y_pred: Sequence[Any],
    positive_label: Any = 1,
) -> dict[str, float]:
    """
    Binary precision, recall, and F1 for *positive_label*.

    Returns
    -------
    {"precision": float, "recall": float, "f1": float}
    """
    if len(y_true) != len(y_pred):
        raise ValueError("y_true and y_pred must have the same length")

    tp = sum(t == positive_label and p == positive_label for t, p in zip(y_true, y_pred))
    fp = sum(t != positive_label and p == positive_label for t, p in zip(y_true, y_pred))
    fn = sum(t == positive_label and p != positive_label for t, p in zip(y_true, y_pred))

    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )
    return {"precision": precision, "recall": recall, "f1": f1}


# ---------------------------------------------------------------------------
# Agentic / session-level metrics
# ---------------------------------------------------------------------------


def compute_diagnosis_metrics(sessions: list[dict[str, Any]]) -> dict[str, float]:
    """
    Compute diagnosis quality metrics from a list of completed session dicts.

    Expected session fields (all optional — missing fields are skipped):
        loop_status      : str — "success" | "escalated" | "max_steps" | "timeout"
        loop_counter     : int — number of steps taken
        expected_resolution : str — ground-truth category (for accuracy)
        actual_resolution   : str — predicted category
        resolution_time_s   : float — wall-clock seconds to resolution

    Returns
    -------
    dict with:
        resolution_rate        : fraction of sessions that reached LoopStatus.SUCCESS
        escalation_rate        : fraction that were escalated
        mean_steps             : mean loop_counter across sessions
        mean_time_s            : mean resolution_time_s (when available)
        diagnosis_accuracy     : accuracy over sessions with both expected/actual
    """
    if not sessions:
        return {
            "resolution_rate": 0.0,
            "escalation_rate": 0.0,
            "mean_steps": 0.0,
            "mean_time_s": 0.0,
            "diagnosis_accuracy": 0.0,
        }

    n = len(sessions)
    resolved = sum(1 for s in sessions if s.get("loop_status") == "success")
    escalated = sum(1 for s in sessions if s.get("loop_status") == "escalated")

    steps = [s["loop_counter"] for s in sessions if "loop_counter" in s]
    times = [s["resolution_time_s"] for s in sessions if "resolution_time_s" in s]

    labelled = [
        s for s in sessions
        if "expected_resolution" in s and "actual_resolution" in s
    ]
    if labelled:
        diag_acc = compute_accuracy(
            [s["expected_resolution"] for s in labelled],
            [s["actual_resolution"] for s in labelled],
        )
    else:
        diag_acc = 0.0

    return {
        "resolution_rate": resolved / n,
        "escalation_rate": escalated / n,
        "mean_steps": sum(steps) / len(steps) if steps else 0.0,
        "mean_time_s": sum(times) / len(times) if times else 0.0,
        "diagnosis_accuracy": diag_acc,
    }


def compute_safety_metrics(sessions: list[dict[str, Any]]) -> dict[str, float]:
    """
    Compute safety and guardrail metrics from a list of session dicts.

    Expected session fields (all optional):
        action_log            : list of action dicts with "risk", "status" fields
        hallucination_rejected: int — count of planner outputs rejected by guard
        confirmation_timeout  : int — count of confirmation tokens that expired
        tool_errors           : int — count of tool call failures

    Returns
    -------
    dict with:
        false_positive_action_rate  : destructive/config actions auto-approved (should be 0)
        confirmation_timeout_rate   : fraction of confirmed sessions with at least one timeout
        hallucination_rejection_rate: hallucinations caught / total sessions
        tool_error_rate             : sessions with tool errors / total sessions
    """
    if not sessions:
        return {
            "false_positive_action_rate": 0.0,
            "confirmation_timeout_rate": 0.0,
            "hallucination_rejection_rate": 0.0,
            "tool_error_rate": 0.0,
        }

    n = len(sessions)

    _RISKY = {"destructive", "config_change", "firmware", "reboot", "service_restart"}
    auto_approved_risky = 0
    total_risky = 0

    for s in sessions:
        for act in s.get("action_log", []):
            risk = act.get("risk", "")
            status = act.get("status", "")
            if risk in _RISKY:
                total_risky += 1
                if status == "confirmed":  # should always be PENDING/held, never auto-confirmed
                    auto_approved_risky += 1

    fp_rate = auto_approved_risky / total_risky if total_risky > 0 else 0.0

    timeouts = sum(1 for s in sessions if s.get("confirmation_timeout", 0) > 0)
    hallucinations = sum(1 for s in sessions if s.get("hallucination_rejected", 0) > 0)
    tool_errors = sum(1 for s in sessions if s.get("tool_errors", 0) > 0)

    return {
        "false_positive_action_rate": fp_rate,
        "confirmation_timeout_rate": timeouts / n,
        "hallucination_rejection_rate": hallucinations / n,
        "tool_error_rate": tool_errors / n,
    }
