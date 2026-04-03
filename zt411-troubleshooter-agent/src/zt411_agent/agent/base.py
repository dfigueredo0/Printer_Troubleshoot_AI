from abc import ABC, abstractmethod
from typing import Any, Dict

class Specialist(ABC):
    name: str

    @abstractmethod
    def can_handle(self, state: Any) -> float:
        """
        Return utility score (0.0–1.0) representing
        expected information gain / usefulness.
        """
        pass

    @abstractmethod
    def act(self, state: Any) -> Dict[str, Any]:
        """
        Execute next step and return:
        {
            "evidence": ...,
            "actions_taken": ...,
            "next_state": ...
        }
        """
        pass