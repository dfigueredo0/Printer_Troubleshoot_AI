"""
transforms.py — Lightweight transforms applied to SampleCase objects before eval / training.

All transforms are pure functions: SampleCase → SampleCase (or dict → dict).
They do not mutate the input.
"""

from __future__ import annotations

from .dataset import SampleCase


def normalise_symptoms(case: SampleCase) -> SampleCase:
    """Lowercase and strip whitespace from all symptom strings."""
    return SampleCase(
        **{
            **case.__dict__,
            "symptoms": [s.strip().lower() for s in case.symptoms],
        }
    )


def normalise_platform(case: SampleCase) -> SampleCase:
    """Normalise os_platform to one of: windows | linux | macos | unknown."""
    known = {"windows", "linux", "macos"}
    platform = case.os_platform.strip().lower()
    if platform not in known:
        platform = "unknown"
    return SampleCase(**{**case.__dict__, "os_platform": platform})


def to_agent_input(case: SampleCase) -> dict:
    """
    Convert a SampleCase to the dict format expected by AgentState initialisation.

    Returns a dict suitable for: AgentState(**to_agent_input(case))
    """
    from ..state import OSPlatform

    platform_map = {
        "windows": OSPlatform.WINDOWS,
        "linux": OSPlatform.LINUX,
        "macos": OSPlatform.MACOS,
    }
    return {
        "os_platform": platform_map.get(case.os_platform, OSPlatform.UNKNOWN),
        "symptoms": case.symptoms,
        "user_description": case.description,
        "device": {"ip": case.device_ip},
    }


def compose(*transforms):
    """Compose multiple transforms into a single function."""
    def _apply(case: SampleCase) -> SampleCase:
        for fn in transforms:
            case = fn(case)
        return case
    return _apply


# Canonical pipeline for eval runs
default_transform = compose(normalise_symptoms, normalise_platform)
