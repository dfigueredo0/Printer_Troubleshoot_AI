# MIT License

"""
eval.py — Evaluate the ZT411 troubleshooter baseline model.

Two evaluation modes:
  1. Classifier mode (default): loads the trained classifier from
     data/cache/classifier.pkl and evaluates on the held-out val split.
  2. Centroid mode (--centroid): uses cosine similarity to domain centroids
     as a zero-shot baseline.  Evaluates on the full dataset.

Writes to reports/:
  * eval.json                  — numeric results + confusion matrix
  * confusion_matrix.png       — row-normalized confusion matrix heatmap
  * per_domain_f1.png          — precision/recall/F1 bar chart per domain

Usage:
    python -m zt411_agent.eval              # classifier on held-out val split
    python -m zt411_agent.eval --centroid   # zero-shot centroid baseline
    python -m zt411_agent.eval --full       # classifier on full dataset
"""

import argparse
import json
import pickle
import sys
from pathlib import Path

import numpy as np

from .data.dataset import TroubleshootingDataset
from .metrics import (
    compute_accuracy,
    compute_confusion_matrix,
    compute_precision_recall_f1,
    format_confusion_matrix,
    plot_normalized_confusion_matrix,
    plot_per_domain_f1,
)
from .models.baseline import BaselineModel
from .settings import Settings

# Must match train.py and gen_synth_data.py
DOMAIN_LABELS = {
    "network": 0,
    "device": 1,
    "windows": 2,
    "cups": 3,
    "validation": 4,
}
LABEL_DOMAINS = {v: k for k, v in DOMAIN_LABELS.items()}


def _build_domain_centroids(model: BaselineModel) -> np.ndarray:
    """Build reference embeddings for each specialist domain."""
    domain_texts = {
        "network": "network connectivity IP ping firewall port TCP VLAN DNS subnet routing",
        "device": "printer hardware sensor printhead ribbon media USB LED error calibration jam",
        "windows": "Windows driver spooler print queue registry event log ZDesigner service",
        "cups": "CUPS Linux lp queue filter PPD cupsd print system backend apparmor",
        "validation": "validation confirmation escalation guardrail safety recheck verify barcode scan failed escalate field service",
    }
    centroids = np.zeros((len(DOMAIN_LABELS), 384))
    for domain, label in DOMAIN_LABELS.items():
        emb = np.asarray(model.model.encode(domain_texts[domain]), dtype=np.float64)
        centroids[label] = emb / (np.linalg.norm(emb) + 1e-9)
    return centroids


def predict_centroid(model: BaselineModel, text: str, centroids: np.ndarray) -> int:
    """Return the predicted domain label using cosine similarity to centroids."""
    emb = np.asarray(model.model.encode(text), dtype=np.float64)
    emb = emb / (np.linalg.norm(emb) + 1e-9)
    return int(np.argmax(centroids @ emb))


def predict_classifier(text: str, model: BaselineModel, W: np.ndarray, b: np.ndarray) -> int:
    """Return the predicted domain label using the trained classifier."""
    emb = np.asarray(model.model.encode(text), dtype=np.float64)
    emb = emb / (np.linalg.norm(emb) + 1e-9)
    logits = emb @ W + b
    return int(np.argmax(logits))


def evaluate(y_true: list[int], y_pred: list[int]) -> dict:
    """Compute accuracy, per-domain P/R/F1, and macro F1."""
    acc = compute_accuracy(y_true, y_pred)

    per_domain = {}
    for domain, label in DOMAIN_LABELS.items():
        binary_true = [1 if t == label else 0 for t in y_true]
        binary_pred = [1 if p == label else 0 for p in y_pred]
        per_domain[domain] = compute_precision_recall_f1(binary_true, binary_pred)

    macro_f1 = np.mean([d["f1"] for d in per_domain.values()])

    return {
        "num_cases": len(y_true),
        "accuracy": round(acc, 4),
        "macro_f1": round(float(macro_f1), 4),
        "per_domain": {
            k: {m: round(v, 4) for m, v in d.items()}
            for k, d in per_domain.items()
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--centroid", action="store_true", help="Use zero-shot centroid baseline")
    parser.add_argument("--full", action="store_true", help="Evaluate on full dataset (not just val split)")
    args, _ = parser.parse_known_args()

    cfg = Settings.load()

    # Load dataset
    dataset = TroubleshootingDataset.from_jsonl(cfg.data.raw_path)
    if len(dataset) == 0:
        print("No cases found. Run `python scripts/gen_synth_data.py` first.")
        return

    model = BaselineModel(cfg.model.embedding_model)

    # Filter to cases with known domains
    all_cases = [c for c in dataset if c.expected_resolution in DOMAIN_LABELS]

    # Determine which cases to evaluate
    classifier_path = Path(cfg.data.cache_dir) / "classifier.pkl"
    W, b, val_indices = None, None, None

    if not args.centroid and classifier_path.exists():
        with open(classifier_path, "rb") as f:
            clf = pickle.load(f)
        W = clf["weights"]
        b = clf["bias"]
        val_indices = clf.get("val_indices")

    if args.centroid:
        mode = "centroid"
        eval_cases = all_cases
    elif W is not None and val_indices is not None and not args.full:
        mode = "classifier (val split)"
        eval_cases = [all_cases[i] for i in val_indices if i < len(all_cases)]
    elif W is not None:
        mode = "classifier (full dataset)"
        eval_cases = all_cases
    else:
        print("No trained classifier found. Run `python -m zt411_agent.train` first,")
        print("or use --centroid for the zero-shot baseline.")
        return

    print(f"Eval mode: {mode}")
    print(f"Evaluating on {len(eval_cases)} cases...")

    # Predict
    y_true = []
    y_pred = []

    if args.centroid:
        centroids = _build_domain_centroids(model)
        for case in eval_cases:
            y_true.append(DOMAIN_LABELS[case.expected_resolution])
            y_pred.append(predict_centroid(model, case.build_input_text(), centroids))
    else:
        for case in eval_cases:
            y_true.append(DOMAIN_LABELS[case.expected_resolution])
            y_pred.append(predict_classifier(case.build_input_text(), model, W, b))

    results = evaluate(y_true, y_pred)
    results["mode"] = mode

    # Confusion matrix
    label_order = sorted(DOMAIN_LABELS.values())
    domain_names = [LABEL_DOMAINS[l] for l in label_order]
    cm = compute_confusion_matrix(y_true, y_pred, labels=label_order)
    results["confusion_matrix"] = {
        "labels": domain_names,
        "matrix": cm,
    }

    # Write report
    out_dir = Path("reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "eval.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\nEvaluation complete — {results['num_cases']} cases")
    print(f"  Accuracy:  {results['accuracy']}")
    print(f"  Macro F1:  {results['macro_f1']}")
    for domain, metrics in results["per_domain"].items():
        print(f"  {domain:12s}  P={metrics['precision']:.3f}  R={metrics['recall']:.3f}  F1={metrics['f1']:.3f}")
    print()
    print(format_confusion_matrix(cm, domain_names))
    print(f"\nReport saved to {out_path}")

    # ---- Plots -------------------------------------------------------------
    # Tag filenames with mode so classifier-vs-centroid runs don't overwrite
    # each other.
    mode_slug = mode.split()[0]  # "classifier" or "centroid"

    try:
        cm_path = plot_normalized_confusion_matrix(
            cm,
            domain_names,
            out_dir / f"confusion_matrix_{mode_slug}.png",
            title=f"Normalized Confusion Matrix — {mode}",
        )
        print(f"Confusion matrix plot saved to {cm_path}")
    except RuntimeError as exc:
        print(f"Skipped confusion matrix plot: {exc}")

    try:
        f1_path = plot_per_domain_f1(
            results["per_domain"],
            out_dir / f"per_domain_f1_{mode_slug}.png",
            title=f"Per-Domain F1 — {mode}",
        )
        print(f"F1 plot saved to {f1_path}")
    except RuntimeError as exc:
        print(f"Skipped F1 plot: {exc}")


if __name__ == "__main__":
    main()