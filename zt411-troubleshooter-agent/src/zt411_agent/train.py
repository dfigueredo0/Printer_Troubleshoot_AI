# MIT License

import random
import numpy as np
from .settings import Settings
from .models.baseline import BaselineModel
import mlflow


def set_seed(seed: int):
    random.seed(seed)
    np.random.seed(seed)


def main():
    cfg = Settings.load()
    if cfg.train.deterministic:
        set_seed(cfg.train.seed)

    if cfg.train.mlflow:
        mlflow.start_run()

    model = BaselineModel(cfg.model.embedding_model)
    print("Training stub complete.")

    if cfg.train.mlflow:
        mlflow.end_run()


if __name__ == "__main__":
    main()
