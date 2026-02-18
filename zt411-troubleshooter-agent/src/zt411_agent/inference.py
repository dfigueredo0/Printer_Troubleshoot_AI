from .models.baseline import BaselineModel
from .settings import Settings


def load_model():
    cfg = Settings.load()
    return BaselineModel(cfg.model.embedding_model)
