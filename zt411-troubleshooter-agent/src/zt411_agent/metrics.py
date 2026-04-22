"""
metrics.py — Evaluation metrics for the ZT411 troubleshooter agent.

Public functions
----------------
compute_accuracy(y_true, y_pred)            → float
compute_precision_recall_f1(y_true, y_pred) → dict
compute_confusion_matrix(y_true, y_pred)    → list[list[int]]
format_confusion_matrix(matrix, label_names)→ str
compute_diagnosis_metrics(sessions)         → dict
compute_safety_metrics(sessions)            → dict

Plotting helpers (matplotlib-based, non-interactive Agg backend)
----------------------------------------------------------------
plot_training_history(history, out_path)            → Path
plot_normalized_confusion_matrix(matrix, labels, out_path) → Path
plot_per_domain_f1(per_domain, out_path)            → Path

`sessions` is a list of dicts with the schema produced by eval.py / sample_cases.jsonl.
"""

from __future__ import annotations

from pathlib import Path
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
# Confusion matrix
# ---------------------------------------------------------------------------


def compute_confusion_matrix(
    y_true: Sequence[int],
    y_pred: Sequence[int],
    labels: Sequence[int] | None = None,
) -> list[list[int]]:
    """
    Build an NxN confusion matrix where rows are true labels and columns
    are predicted labels.

    Parameters
    ----------
    y_true : Ground-truth integer labels.
    y_pred : Predicted integer labels.
    labels : Ordered list of label indices.  If None, derived from the union
             of y_true and y_pred.

    Returns
    -------
    list[list[int]] — matrix[i][j] = count of samples with true=labels[i],
                      predicted=labels[j].
    """
    if len(y_true) != len(y_pred):
        raise ValueError("y_true and y_pred must have the same length")

    if labels is None:
        labels = sorted(set(y_true) | set(y_pred))

    label_to_idx = {lab: i for i, lab in enumerate(labels)}
    n = len(labels)
    matrix: list[list[int]] = [[0] * n for _ in range(n)]

    for t, p in zip(y_true, y_pred):
        if t in label_to_idx and p in label_to_idx:
            matrix[label_to_idx[t]][label_to_idx[p]] += 1

    return matrix


def format_confusion_matrix(
    matrix: list[list[int]],
    label_names: Sequence[str],
) -> str:
    """
    Return a human-readable string representation of a confusion matrix.

    Rows = true, columns = predicted.
    """
    n = len(label_names)
    # Column width: max of label length and widest number
    max_count = max(max(row) for row in matrix) if matrix else 0
    num_width = max(len(str(max_count)), 5)
    label_width = max(len(name) for name in label_names)
    col_width = max(num_width, label_width) + 1

    lines = []
    # Header
    header = " " * (label_width + 2) + "".join(name.rjust(col_width) for name in label_names)
    lines.append("Confusion Matrix (rows=true, cols=predicted):")
    lines.append(header)
    lines.append(" " * (label_width + 2) + "-" * (col_width * n))

    # Rows
    for i, name in enumerate(label_names):
        row_str = "".join(str(v).rjust(col_width) for v in matrix[i])
        lines.append(f"{name.rjust(label_width)} |{row_str}")

    return "\n".join(lines)


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


# ---------------------------------------------------------------------------
# Plotting helpers
# ---------------------------------------------------------------------------
# These helpers use matplotlib's non-interactive "Agg" backend so they can
# run in headless environments (CI, docker, servers). Files are written as
# PNG to the requested path and the path is returned.


def _ensure_matplotlib():
    """
    Lazy-import matplotlib and force the non-interactive 'Agg' backend.

    Returns the pyplot module. Raises a clear RuntimeError if matplotlib is
    not installed — callers can decide whether to skip or fail.
    """
    try:
        import matplotlib
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "matplotlib is required to render plots. Install it with "
            "`pip install matplotlib` or `poetry add matplotlib`."
        ) from exc

    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt  # noqa: E402
    return plt


def plot_training_history(
    history: dict[str, Sequence[float]],
    out_path: str | Path,
    title: str = "Training History",
) -> Path:
    """
    Render a two-panel figure: (left) train/val loss, (right) train/val accuracy.

    Parameters
    ----------
    history : dict with keys "epoch", "train_loss", "val_loss", "train_acc",
              "val_acc". Any missing series is silently skipped (the panel
              will still render the series that are present).
    out_path : destination PNG path. Parent directories are created as needed.
    title   : figure suptitle.

    Returns
    -------
    Path to the written PNG.
    """
    plt = _ensure_matplotlib()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    epochs = list(history.get("epoch") or [])
    if not epochs:
        # Derive from whichever series is longest
        for key in ("train_loss", "val_loss", "train_acc", "val_acc"):
            series = history.get(key)
            if series:
                epochs = list(range(1, len(series) + 1))
                break

    fig, (ax_loss, ax_acc) = plt.subplots(1, 2, figsize=(12, 4.5))

    # -- Loss panel ---------------------------------------------------------
    if history.get("train_loss"):
        ax_loss.plot(epochs, history["train_loss"], label="train_loss", marker="o", markersize=3)
    if history.get("val_loss"):
        ax_loss.plot(epochs, history["val_loss"], label="val_loss", marker="s", markersize=3)
    ax_loss.set_xlabel("Epoch")
    ax_loss.set_ylabel("Cross-entropy loss")
    ax_loss.set_title("Loss")
    ax_loss.grid(True, alpha=0.3)
    if ax_loss.has_data():
        ax_loss.legend()

    # -- Accuracy panel -----------------------------------------------------
    if history.get("train_acc"):
        ax_acc.plot(epochs, history["train_acc"], label="train_acc", marker="o", markersize=3)
    if history.get("val_acc"):
        ax_acc.plot(epochs, history["val_acc"], label="val_acc", marker="s", markersize=3)
    ax_acc.set_xlabel("Epoch")
    ax_acc.set_ylabel("Accuracy")
    ax_acc.set_title("Accuracy")
    ax_acc.set_ylim(0.0, 1.02)
    ax_acc.grid(True, alpha=0.3)
    if ax_acc.has_data():
        ax_acc.legend()

    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_normalized_confusion_matrix(
    matrix: Sequence[Sequence[int]],
    label_names: Sequence[str],
    out_path: str | Path,
    title: str = "Normalized Confusion Matrix",
    cmap: str = "Blues",
) -> Path:
    """
    Render a row-normalized confusion matrix heatmap (each row sums to 1).

    Cells are annotated with the fraction (e.g. "0.98"). Rows with zero
    true examples are shown as all zeros rather than NaN so the figure is
    always renderable.

    Parameters
    ----------
    matrix      : NxN count matrix (rows=true, cols=predicted).
    label_names : length-N list of class names, in the same order as the matrix.
    out_path    : destination PNG path.
    title       : figure title.
    cmap        : matplotlib colormap name.

    Returns
    -------
    Path to the written PNG.
    """
    plt = _ensure_matplotlib()
    import numpy as np  # local import — numpy is already a core dep

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cm = np.asarray(matrix, dtype=float)
    row_sums = cm.sum(axis=1, keepdims=True)
    # Avoid division-by-zero for empty rows; leave them as zeros.
    with np.errstate(invalid="ignore", divide="ignore"):
        norm = np.where(row_sums > 0, cm / row_sums, 0.0)

    n = len(label_names)
    fig, ax = plt.subplots(figsize=(max(5, 0.9 * n + 2), max(4, 0.9 * n + 1.5)))
    im = ax.imshow(norm, cmap=cmap, vmin=0.0, vmax=1.0, aspect="auto")

    ax.set_xticks(range(n))
    ax.set_yticks(range(n))
    ax.set_xticklabels(label_names, rotation=45, ha="right")
    ax.set_yticklabels(label_names)
    ax.set_xlabel("Predicted")
    ax.set_ylabel("True")
    ax.set_title(title)

    # Annotate cells. Choose a contrasting text color based on cell intensity.
    threshold = 0.5
    for i in range(n):
        for j in range(n):
            val = norm[i, j]
            ax.text(
                j, i, f"{val:.2f}",
                ha="center", va="center",
                color="white" if val > threshold else "black",
                fontsize=9,
            )

    cbar = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label("Fraction of true class")

    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path


def plot_per_domain_f1(
    per_domain: dict[str, dict[str, float]],
    out_path: str | Path,
    title: str = "Per-Domain F1",
) -> Path:
    """
    Render a grouped bar chart of precision / recall / F1 per domain.

    Parameters
    ----------
    per_domain : mapping of domain name → {"precision", "recall", "f1"} floats,
                 as produced by eval.evaluate().
    out_path   : destination PNG path.
    title      : figure title.

    Returns
    -------
    Path to the written PNG.
    """
    plt = _ensure_matplotlib()
    import numpy as np

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    domains = list(per_domain.keys())
    precision = [per_domain[d].get("precision", 0.0) for d in domains]
    recall = [per_domain[d].get("recall", 0.0) for d in domains]
    f1 = [per_domain[d].get("f1", 0.0) for d in domains]

    x = np.arange(len(domains))
    width = 0.27

    fig, ax = plt.subplots(figsize=(max(6, 1.2 * len(domains) + 2), 4.5))
    ax.bar(x - width, precision, width, label="precision")
    ax.bar(x, recall, width, label="recall")
    ax.bar(x + width, f1, width, label="F1")

    ax.set_xticks(x)
    ax.set_xticklabels(domains, rotation=20, ha="right")
    ax.set_ylim(0.0, 1.05)
    ax.set_ylabel("Score")
    ax.set_title(title)
    ax.grid(True, axis="y", alpha=0.3)
    ax.legend()

    # Annotate F1 bars with their value for quick reading.
    for xi, v in zip(x + width, f1):
        ax.text(xi, v + 0.01, f"{v:.2f}", ha="center", va="bottom", fontsize=8)

    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out_path