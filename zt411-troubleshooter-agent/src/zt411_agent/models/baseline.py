# MIT License

from sentence_transformers import SentenceTransformer
from .interface import BaseModelInterface


class BaselineModel(BaseModelInterface):
    def __init__(self, model_name: str):
        self.model = SentenceTransformer(model_name)

    def predict(self, text: str) -> dict:
        emb = self.model.encode(text)
        return {"embedding_norm": float((emb**2).sum())}
