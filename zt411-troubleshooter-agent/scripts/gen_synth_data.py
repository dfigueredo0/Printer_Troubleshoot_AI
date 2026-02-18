import json
from pathlib import Path

cases = [
    {"text": "Printer not printing over network", "label": 1},
    {"text": "Driver crash after update", "label": 0},
]

Path("data/sample").mkdir(parents=True, exist_ok=True)
with open("data/sample/sample_cases.jsonl", "w") as f:
    for c in cases:
        f.write(json.dumps(c) + "\n")
