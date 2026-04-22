# MIT License

"""
train.py — Train a specialist-routing classifier for the ZT411 agent.

Encodes each troubleshooting case with the sentence-transformer model,
then trains a logistic regression head with L2 regularization to predict
which specialist domain should handle the case.

The classifier and train/val split indices are saved to data/cache/classifier.pkl
so eval.py can evaluate on the exact held-out set.

Per-epoch history (train_loss, val_loss, train_acc, val_acc) is written to
reports/train_history.json and rendered as reports/train_history.png.

Usage:
    python -m zt411_agent.train
"""

import json
import pickle
import random
from pathlib import Path

import numpy as np
import mlflow

from .data.dataset import TroubleshootingDataset
from .metrics import plot_training_history
from .models.baseline import BaselineModel
from .settings import Settings

# Must match gen_synth_data.py and eval.py
DOMAIN_LABELS = {
    "network": 0,
    "device": 1,
    "windows": 2,
    "cups": 3,
    "validation": 4,
}


def set_seed(seed: int):
    random.seed(seed)
    np.random.seed(seed)


def softmax(logits: np.ndarray) -> np.ndarray:
    exp = np.exp(logits - logits.max(axis=1, keepdims=True))
    return exp / exp.sum(axis=1, keepdims=True)


def _cross_entropy(X: np.ndarray, y: np.ndarray, W: np.ndarray, b: np.ndarray) -> float:
    """Mean cross-entropy loss for a dataset against the current (W, b)."""
    if len(X) == 0:
        return 0.0
    logits = X @ W + b
    probs = softmax(logits)
    ce = -np.log(probs[np.arange(len(y)), y] + 1e-9)
    return float(ce.mean())


def main():
    cfg = Settings.load()
    if cfg.train.deterministic:
        set_seed(cfg.train.seed)

    if cfg.train.mlflow:
        mlflow.start_run()

    # ---- Load data --------------------------------------------------------
    dataset = TroubleshootingDataset.from_jsonl(cfg.data.raw_path)
    if len(dataset) == 0:
        print("No cases found. Run `python scripts/gen_synth_data.py` first.")
        return

    model = BaselineModel(cfg.model.embedding_model)
    num_classes = len(DOMAIN_LABELS)

    # Build feature matrix and labels using the enriched text builder
    texts = []
    labels = []
    case_ids = []
    for case in dataset:
        if case.expected_resolution not in DOMAIN_LABELS:
            continue
        texts.append(case.build_input_text())
        labels.append(DOMAIN_LABELS[case.expected_resolution])
        case_ids.append(case.case_id)

    print(f"Encoding {len(texts)} cases...")
    X = np.array(model.model.encode(texts, show_progress_bar=True))  # (N, D)
    y = np.array(labels)  # (N,)
    N, D = X.shape

    # L2-normalize embeddings for cosine-like geometry
    norms = np.linalg.norm(X, axis=1, keepdims=True) + 1e-9
    X = X / norms

    # Train/val split — save indices for eval.py
    split = int(N * cfg.data.split_ratio)
    indices = list(range(N))
    random.shuffle(indices)
    train_idx, val_idx = indices[:split], indices[split:]

    X_train, y_train = X[train_idx], y[train_idx]
    X_val, y_val = X[val_idx], y[val_idx]

    print(f"Dataset: {N} cases, {split} train / {N - split} val, {D}-dim embeddings")
    # Show class distribution
    from collections import Counter
    label_to_domain = {v: k for k, v in DOMAIN_LABELS.items()}
    train_dist = Counter(y_train.tolist())
    val_dist = Counter(y_val.tolist())
    print("  Train distribution:", {label_to_domain[k]: v for k, v in sorted(train_dist.items())})
    print("  Val distribution:  ", {label_to_domain[k]: v for k, v in sorted(val_dist.items())})

    # ---- Hyperparameters ---------------------------------------------------
    lr_init = 0.5
    lr_min = 0.01
    weight_decay = 1e-3          # L2 regularization strength
    batch_size = min(cfg.train.batch_size, len(X_train))
    epochs = cfg.train.epochs
    patience = 8                 # early stopping patience
    lr_decay_factor = 0.95       # per-epoch LR decay

    # ---- Train logistic regression with SGD + L2 ---------------------------
    W = np.random.randn(D, num_classes) * 0.01
    b = np.zeros(num_classes)

    best_val_acc = 0.0
    best_W, best_b = W.copy(), b.copy()
    epochs_without_improvement = 0
    lr = lr_init

    # Per-epoch history for plotting / post-hoc analysis
    history: dict[str, list[float]] = {
        "epoch": [],
        "train_loss": [],
        "val_loss": [],
        "train_acc": [],
        "val_acc": [],
        "lr": [],
    }

    for epoch in range(1, epochs + 1):
        perm = list(range(len(X_train)))
        random.shuffle(perm)

        epoch_loss = 0.0
        num_batches = 0

        for start in range(0, len(X_train), batch_size):
            batch_idx = perm[start : start + batch_size]
            Xb = X_train[batch_idx]
            yb = y_train[batch_idx]

            # Forward: softmax cross-entropy
            logits = Xb @ W + b
            probs = softmax(logits)

            B = len(yb)
            log_probs = -np.log(probs[np.arange(B), yb] + 1e-9)
            ce_loss = log_probs.mean()
            l2_loss = 0.5 * weight_decay * (W ** 2).sum()
            loss = ce_loss + l2_loss
            epoch_loss += loss
            num_batches += 1

            # Backward
            grad_logits = probs.copy()
            grad_logits[np.arange(B), yb] -= 1.0
            grad_logits /= B

            grad_W = Xb.T @ grad_logits + weight_decay * W
            grad_b = grad_logits.sum(axis=0)

            W -= lr * grad_W
            b -= lr * grad_b

        avg_loss = epoch_loss / max(num_batches, 1)

        # Validation loss + accuracy
        val_loss = _cross_entropy(X_val, y_val, W, b) if len(X_val) > 0 else 0.0
        val_logits = X_val @ W + b
        val_preds = val_logits.argmax(axis=1)
        val_acc = (val_preds == y_val).mean() if len(y_val) > 0 else 0.0

        train_logits = X_train @ W + b
        train_preds = train_logits.argmax(axis=1)
        train_acc = (train_preds == y_train).mean()

        history["epoch"].append(epoch)
        history["train_loss"].append(float(avg_loss))
        history["val_loss"].append(float(val_loss))
        history["train_acc"].append(float(train_acc))
        history["val_acc"].append(float(val_acc))
        history["lr"].append(float(lr))

        print(
            f"  Epoch {epoch:3d}/{epochs}  "
            f"loss={avg_loss:.4f}  val_loss={val_loss:.4f}  "
            f"train_acc={train_acc:.4f}  val_acc={val_acc:.4f}  lr={lr:.4f}"
        )

        if cfg.train.mlflow:
            mlflow.log_metrics({
                "loss": float(avg_loss),
                "val_loss": float(val_loss),
                "train_acc": float(train_acc),
                "val_acc": float(val_acc),
                "lr": lr,
            }, step=epoch)

        # Early stopping check
        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_W, best_b = W.copy(), b.copy()
            epochs_without_improvement = 0
        else:
            epochs_without_improvement += 1
            if epochs_without_improvement >= patience:
                print(f"\n  Early stopping at epoch {epoch} (no improvement for {patience} epochs)")
                break

        # Learning rate decay
        lr = max(lr * lr_decay_factor, lr_min)

    # Restore best weights
    W, b = best_W, best_b

    # Final val accuracy with best weights
    val_logits = X_val @ W + b
    val_preds = val_logits.argmax(axis=1)
    final_val_acc = (val_preds == y_val).mean() if len(y_val) > 0 else 0.0
    print(f"\n  Best val accuracy: {final_val_acc:.4f}")

    # Per-class val accuracy
    for label, domain in sorted(label_to_domain.items()):
        mask = y_val == label
        if mask.sum() > 0:
            domain_acc = (val_preds[mask] == y_val[mask]).mean()
            print(f"    {domain:12s} val_acc={domain_acc:.4f} ({mask.sum()} samples)")

    # ---- Save classifier + split info --------------------------------------
    cache_dir = Path(cfg.data.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    classifier_path = cache_dir / "classifier.pkl"

    classifier = {
        "weights": W,
        "bias": b,
        "domain_labels": DOMAIN_LABELS,
        "embedding_model": cfg.model.embedding_model,
        "train_indices": train_idx,
        "val_indices": val_idx,
        "case_ids": case_ids,
        "best_val_acc": float(final_val_acc),
    }
    with open(classifier_path, "wb") as f:
        pickle.dump(classifier, f)

    print(f"\nTraining complete. Classifier saved to {classifier_path}")

    # ---- Save training history + render plot -------------------------------
    reports_dir = Path("reports")
    reports_dir.mkdir(parents=True, exist_ok=True)

    history_path = reports_dir / "train_history.json"
    with open(history_path, "w") as f:
        json.dump(history, f, indent=2)
    print(f"Training history saved to {history_path}")

    try:
        plot_path = plot_training_history(
            history,
            reports_dir / "train_history.png",
            title="Training History — train/val loss and accuracy",
        )
        print(f"Training plot saved to {plot_path}")
        if cfg.train.mlflow:
            mlflow.log_artifact(str(plot_path))
            mlflow.log_artifact(str(history_path))
    except RuntimeError as exc:
        # matplotlib missing — don't fail training over a plot.
        print(f"Skipped training plot: {exc}")

    if cfg.train.mlflow:
        mlflow.end_run()


if __name__ == "__main__":
    main()