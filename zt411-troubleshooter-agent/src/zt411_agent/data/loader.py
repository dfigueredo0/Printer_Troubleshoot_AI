"""
loader.py — DataLoader for TroubleshootingDataset.

Provides batched iteration and optional shuffling over SampleCase collections.
"""

from __future__ import annotations

import random
from typing import Iterator

from .dataset import SampleCase, TroubleshootingDataset


class DataLoader:
    """
    Batched iterator over a TroubleshootingDataset.

    Parameters
    ----------
    dataset    : The source dataset.
    batch_size : Number of cases per batch (default 1).
    shuffle    : Whether to shuffle the order on each epoch (default False).
    seed       : RNG seed for reproducible shuffles (default None = random).
    """

    def __init__(
        self,
        dataset: TroubleshootingDataset,
        batch_size: int = 1,
        shuffle: bool = False,
        seed: int | None = None,
    ) -> None:
        self._dataset = dataset
        self.batch_size = max(1, batch_size)
        self.shuffle = shuffle
        self._rng = random.Random(seed)

    def __len__(self) -> int:
        """Number of batches per epoch."""
        return (len(self._dataset) + self.batch_size - 1) // self.batch_size

    def __iter__(self) -> Iterator[list[SampleCase]]:
        indices = list(range(len(self._dataset)))
        if self.shuffle:
            self._rng.shuffle(indices)

        batch: list[SampleCase] = []
        for idx in indices:
            batch.append(self._dataset[idx])
            if len(batch) == self.batch_size:
                yield batch
                batch = []
        if batch:
            yield batch
