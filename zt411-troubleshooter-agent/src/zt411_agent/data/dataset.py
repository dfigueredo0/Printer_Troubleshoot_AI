"""
dataset.py — Dataset abstraction for ZT411 troubleshooting sample cases.

The on-disk format is JSONL (one JSON object per line), where each object
represents one labelled troubleshooting scenario used for eval and training.

Sample case schema
------------------
{
    "case_id": "case-001",
    "description": "Printer offline after network switch replacement",
    "symptoms": ["offline", "cannot print", "network unreachable"],
    "os_platform": "windows",
    "device_ip": "192.168.1.100",
    "expected_resolution": "network",            # specialist domain
    "expected_steps": 3,                          # expected loop iterations
    "expected_actions": ["ping", "tcp_connect"],  # tools expected to run
    "resolution_notes": "Port 9100 blocked on new switch VLAN config",
    "risk_class": "safe"
}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SampleCase:
    """One labelled troubleshooting scenario."""

    case_id: str
    description: str
    symptoms: list[str] = field(default_factory=list)
    os_platform: str = "unknown"
    device_ip: str = "192.168.1.100"
    expected_resolution: str = ""
    expected_steps: int = 0
    expected_actions: list[str] = field(default_factory=list)
    resolution_notes: str = ""
    risk_class: str = "safe"

    @classmethod
    def from_dict(cls, data: dict) -> "SampleCase":
        return cls(
            case_id=data.get("case_id", ""),
            description=data.get("description", ""),
            symptoms=data.get("symptoms", []),
            os_platform=data.get("os_platform", "unknown"),
            device_ip=data.get("device_ip", "192.168.1.100"),
            expected_resolution=data.get("expected_resolution", ""),
            expected_steps=data.get("expected_steps", 0),
            expected_actions=data.get("expected_actions", []),
            resolution_notes=data.get("resolution_notes", ""),
            risk_class=data.get("risk_class", "safe"),
        )

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "description": self.description,
            "symptoms": self.symptoms,
            "os_platform": self.os_platform,
            "device_ip": self.device_ip,
            "expected_resolution": self.expected_resolution,
            "expected_steps": self.expected_steps,
            "expected_actions": self.expected_actions,
            "resolution_notes": self.resolution_notes,
            "risk_class": self.risk_class,
        }


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


class TroubleshootingDataset:
    """
    Loads a collection of SampleCase objects from a JSONL file.

    Parameters
    ----------
    path : Path to the .jsonl file.

    Usage
    -----
        ds = TroubleshootingDataset.from_jsonl("data/sample/sample_cases.jsonl")
        for case in ds:
            print(case.case_id, case.expected_resolution)
    """

    def __init__(self, cases: list[SampleCase]) -> None:
        self._cases = cases

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_jsonl(cls, path: str | Path) -> "TroubleshootingDataset":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Dataset not found: {path}")

        cases: list[SampleCase] = []
        with path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    data = json.loads(line)
                    cases.append(SampleCase.from_dict(data))
                except (json.JSONDecodeError, KeyError) as exc:
                    raise ValueError(f"Invalid JSONL at line {lineno}: {exc}") from exc

        return cls(cases)

    # ------------------------------------------------------------------
    # Collection interface
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._cases)

    def __getitem__(self, idx: int) -> SampleCase:
        return self._cases[idx]

    def __iter__(self) -> Iterator[SampleCase]:
        return iter(self._cases)

    def filter_by_platform(self, os_platform: str) -> "TroubleshootingDataset":
        return TroubleshootingDataset(
            [c for c in self._cases if c.os_platform == os_platform]
        )

    def filter_by_resolution(self, domain: str) -> "TroubleshootingDataset":
        return TroubleshootingDataset(
            [c for c in self._cases if c.expected_resolution == domain]
        )

    def case_ids(self) -> list[str]:
        return [c.case_id for c in self._cases]

    def to_list(self) -> list[dict]:
        return [c.to_dict() for c in self._cases]
