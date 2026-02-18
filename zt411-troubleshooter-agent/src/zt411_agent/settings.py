# MIT License

from pathlib import Path
from typing import Any
import yaml
from pydantic import BaseModel


class DataConfig(BaseModel):
    raw_path: str
    cache_dir: str
    split_ratio: float


class TrainConfig(BaseModel):
    batch_size: int
    epochs: int
    seed: int
    deterministic: bool
    mlflow: bool


class ModelConfig(BaseModel):
    name: str
    embedding_model: str
    max_steps: int


class Settings(BaseModel):
    data: DataConfig
    train: TrainConfig
    model: ModelConfig

    @staticmethod
    def load() -> "Settings":
        base = Path("configs")
        data = yaml.safe_load((base / "data.yaml").read_text())
        train = yaml.safe_load((base / "train.yaml").read_text())
        model = yaml.safe_load((base / "model.yaml").read_text())
        return Settings(
            data=DataConfig(**data["data"]),
            train=TrainConfig(**train["train"]),
            model=ModelConfig(**model["model"]),
        )
