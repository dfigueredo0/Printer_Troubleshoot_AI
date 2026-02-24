"""
Owns: routing, planning, and utility scoring.
Chooses which specialist to call next based on information gain vs risk vs cost.
Maintains global state and resolves conflicts between specialists’ recommendations.
Stops early when success criteria are met; escalates to human when blocked.
"""
from .base import Specialist

class Orchestrator:
    def __init__(self, specialists: list[Specialist]):
        self.specialists = specialists

    def next_step(self, state):
        scored = [(s, s.can_handle(state)) for s in self.specialists]
        best = max(scored, key=lambda x: x[1])[0]
        return best.act(state)