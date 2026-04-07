"""
split.py — Train / validation / test split utilities.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .dataset import TroubleshootingDataset


@dataclass
class DataSplit:
    train: TroubleshootingDataset
    val: TroubleshootingDataset
    test: TroubleshootingDataset


def split_dataset(
    dataset: TroubleshootingDataset,
    train_ratio: float = 0.7,
    val_ratio: float = 0.15,
    seed: int = 42,
) -> DataSplit:
    """
    Randomly split *dataset* into train / val / test subsets.

    Parameters
    ----------
    dataset     : The full dataset to split.
    train_ratio : Fraction for training (default 0.70).
    val_ratio   : Fraction for validation (default 0.15).
                  The remainder goes to test.
    seed        : RNG seed for reproducibility.

    Returns
    -------
    DataSplit with .train, .val, .test attributes.
    """
    if not (0 < train_ratio < 1) or not (0 < val_ratio < 1):
        raise ValueError("train_ratio and val_ratio must be in (0, 1)")
    if train_ratio + val_ratio >= 1.0:
        raise ValueError("train_ratio + val_ratio must be < 1.0")

    cases = list(dataset)
    rng = random.Random(seed)
    rng.shuffle(cases)

    n = len(cases)
    n_train = int(n * train_ratio)
    n_val = int(n * val_ratio)

    from .dataset import TroubleshootingDataset as _DS

    return DataSplit(
        train=_DS(cases[:n_train]),
        val=_DS(cases[n_train : n_train + n_val]),
        test=_DS(cases[n_train + n_val :]),
    )
