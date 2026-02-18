from abc import ABC, abstractmethod
from typing import Any


class BaseModelInterface(ABC):
    @abstractmethod
    def predict(self, text: str) -> dict[str, Any]:
        pass
