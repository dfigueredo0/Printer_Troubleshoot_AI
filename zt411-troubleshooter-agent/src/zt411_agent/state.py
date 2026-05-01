# MIT License
"""
AgentState — single source of truth for the agent loop.

Design rules
------------
* Immutable identity fields (device_ip, os_platform, session_id) are set once at
  session start and never mutated by specialists.
* Mutable fields are updated via helper methods that also append an audit entry,
  so the event log is always consistent.
* All fields default to "unknown" / empty so specialists can safely read without
  None-guards everywhere.
* SnapshotDiff captures before/after for any observable state change, giving
  the validation specialist the evidence it needs to confirm success.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------


class OSPlatform(str, Enum):
    WINDOWS = "windows"
    LINUX = "linux"
    MACOS = "macos"
    UNKNOWN = "unknown"


class RiskLevel(str, Enum):
    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    DESTRUCTIVE = "destructive"
    CONFIG_CHANGE = "config_change"
    FIRMWARE = "firmware"
    REBOOT = "reboot"
    SERVICE_RESTART = "service_restart"


class ActionStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    EXECUTED = "executed"
    VERIFYING = "verifying"   # post-execution settle window before verify
    RESOLVED = "resolved"     # verified post-state healthy
    SKIPPED = "skipped"
    FAILED = "failed"


class LoopStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    ESCALATED = "escalated"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"


class LoopIntent(str, Enum):
    """High-level intent of a diagnostic loop. Used to gate optional
    tool calls (e.g. consumables read) that aren't needed for every
    symptom path. The default GENERAL runs everything, preserving
    pre-Phase-4 behavior for callers that don't set an intent.
    """
    GENERAL = "general"
    CALIBRATE = "calibrate"
    DIAGNOSE_CONSUMABLES = "diagnose_consumables"
    DIAGNOSE_PRINT_QUALITY = "diagnose_print_quality"
    DIAGNOSE_NETWORK = "diagnose_network"


class DeviceInfo(BaseModel):
    """Static + slowly-changing facts about the ZT411."""

    ip: str = "unknown"
    mac: str = "unknown"
    hostname: str = "unknown"
    firmware_version: str = "unknown"
    model: str = "ZT411"

    # Last-known SNMP/IPP attributes
    printer_status: str = "unknown"   # e.g. "idle", "printing", "error"
    alerts: list[str] = Field(default_factory=list)
    consumables: dict[str, Any] = Field(default_factory=dict)
    error_codes: list[str] = Field(default_factory=list)

    # Physical observations
    head_open: bool | None = None
    media_out: bool | None = None
    ribbon_out: bool | None = None
    paused: bool | None = None


class EvidenceItem(BaseModel):
    """A single piece of collected evidence, grounded in a tool output or doc snippet."""

    evidence_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    specialist: str
    source: str          # e.g. "snmp", "cups_error_log", "lpstat", "ping", "rag_snippet"
    snippet_id: str = "" # RAG doc reference when applicable
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ActionLogEntry(BaseModel):
    """Records one action taken (or proposed) by any specialist."""

    entry_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    specialist: str
    action: str
    risk: RiskLevel = RiskLevel.SAFE
    status: ActionStatus = ActionStatus.PENDING
    # Full chronological status history for one logical action. Phase 4.3
    # introduced VERIFYING + RESOLVED, and the demo UI needs to show the
    # row transitioning through every state — so each transition appends
    # here in addition to flipping `status`. Initialised in __init__ via
    # a Field default_factory so each entry gets its own list.
    status_history: list[ActionStatus] = Field(default_factory=list)
    confirmation_token: str = ""
    result: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def model_post_init(self, __context: Any) -> None:  # noqa: D401
        # Seed the history with the initial status so callers can read
        # `status_history[0]` without first having to call update_action_status.
        if not self.status_history:
            self.status_history = [self.status]


class SnapshotDiff(BaseModel):
    """Before/after diff of any observable piece of state."""

    field: str
    before: Any
    after: Any
    confirmed_by: str = ""   # specialist that validated the change
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class NetworkInfo(BaseModel):
    """Network-layer facts discovered by the network specialist."""

    reachable: bool | None = None
    latency_ms: float | None = None
    dns_resolved: bool | None = None
    dns_ip: str = ""
    port_open: dict[int, bool] = Field(default_factory=dict)  # {9100: True, ...}
    vlan_id: str = ""
    mac_oui: str = ""
    snmp_sys_descr: str = ""


class CUPSInfo(BaseModel):
    """CUPS / Linux print subsystem state."""

    queue_name: str = ""
    queue_state: str = ""         # "idle", "stopped", "processing"
    pending_jobs: int = 0
    driver_name: str = ""
    device_uri: str = ""
    ppd_valid: bool | None = None
    filter_errors: list[str] = Field(default_factory=list)
    last_error_log: str = ""


class WindowsInfo(BaseModel):
    """Windows print subsystem state."""

    spooler_running: bool | None = None
    queue_name: str = ""
    queue_state: str = ""
    pending_jobs: int = 0
    driver_name: str = ""
    driver_version: str = ""
    driver_isolation: str = ""    # "Isolated" | "Shared" | "None"
    port_type: str = ""           # "TCP/IP" | "WSD" | "USB" | "LPR"
    port_name: str = ""
    event_log_errors: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Root state
# ---------------------------------------------------------------------------


class AgentState(BaseModel):
    """
    Canonical shared state for one troubleshooting session.

    Mutate ONLY via the helper methods below — they maintain the audit trail.
    """

    # ------------------------------------------------------------------
    # Session identity (set once, never changed)
    # ------------------------------------------------------------------
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    os_platform: OSPlatform = OSPlatform.UNKNOWN

    # ------------------------------------------------------------------
    # Symptom description (provided by the caller / user)
    # ------------------------------------------------------------------
    symptoms: list[str] = Field(default_factory=list)
    user_description: str = ""

    # ------------------------------------------------------------------
    # Discovered facts (written by specialists)
    # ------------------------------------------------------------------
    device: DeviceInfo = Field(default_factory=DeviceInfo)
    network: NetworkInfo = Field(default_factory=NetworkInfo)
    cups: CUPSInfo = Field(default_factory=CUPSInfo)
    windows: WindowsInfo = Field(default_factory=WindowsInfo)

    # ------------------------------------------------------------------
    # Evidence + audit trail
    # ------------------------------------------------------------------
    evidence: list[EvidenceItem] = Field(default_factory=list)
    action_log: list[ActionLogEntry] = Field(default_factory=list)
    snapshot_diffs: list[SnapshotDiff] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # Active confirmation tokens  {token: ActionLogEntry.entry_id}
    # ------------------------------------------------------------------
    confirmation_tokens: dict[str, str] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # Loop control
    # ------------------------------------------------------------------
    loop_counter: int = 0
    loop_status: LoopStatus = LoopStatus.RUNNING
    loop_intent: LoopIntent = LoopIntent.GENERAL
    last_specialist: str = ""
    escalation_reason: str = ""

    # Tracks which specialist categories have already been attempted,
    # so the orchestrator can deprioritise re-visiting them.
    visited_specialists: list[str] = Field(default_factory=list)

    # ------------------------------------------------------------------
    # Success criteria flags (set by validation specialist)
    # ------------------------------------------------------------------
    queue_drained: bool = False
    test_print_ok: bool = False
    device_ready: bool = False

    # ------------------------------------------------------------------
    # Helpers — the only correct way to mutate state
    # ------------------------------------------------------------------

    def add_evidence(
        self,
        specialist: str,
        source: str,
        content: str,
        snippet_id: str = "",
    ) -> EvidenceItem:
        item = EvidenceItem(
            specialist=specialist,
            source=source,
            content=content,
            snippet_id=snippet_id,
        )
        self.evidence.append(item)
        return item

    def log_action(
        self,
        specialist: str,
        action: str,
        risk: RiskLevel = RiskLevel.SAFE,
        status: ActionStatus = ActionStatus.PENDING,
        result: str = "",
    ) -> ActionLogEntry:
        entry = ActionLogEntry(
            specialist=specialist,
            action=action,
            risk=risk,
            status=status,
            result=result,
        )
        self.action_log.append(entry)
        return entry

    def record_diff(
        self,
        field: str,
        before: Any,
        after: Any,
        confirmed_by: str = "",
    ) -> SnapshotDiff:
        diff = SnapshotDiff(
            field=field,
            before=before,
            after=after,
            confirmed_by=confirmed_by,
        )
        self.snapshot_diffs.append(diff)
        return diff

    def update_action_status(
        self,
        entry_id: str,
        new_status: ActionStatus,
        result: str | None = None,
    ) -> ActionLogEntry | None:
        """Mutate an existing action_log entry in place.

        Phase 4.3: action lifecycle is one logical entry that transitions
        through PENDING → CONFIRMED → EXECUTED → VERIFYING → RESOLVED
        (or any branch into FAILED). The frontend renders one row per
        entry_id and replaces it in place via HTMX OOB swap on each
        status change, so we mutate rather than append. The full
        transition history lives on `status_history`.

        Returns the mutated entry, or None if no entry has that id.
        """
        for entry in self.action_log:
            if entry.entry_id != entry_id:
                continue
            entry.status = new_status
            entry.status_history.append(new_status)
            if result is not None:
                entry.result = result
            entry.timestamp = datetime.now(timezone.utc)
            return entry
        return None

    def issue_confirmation_token(self, entry_id: str) -> str:
        token = str(uuid.uuid4())
        self.confirmation_tokens[token] = entry_id
        return token

    def consume_confirmation_token(self, token: str) -> str | None:
        """Returns the associated entry_id and removes the token, or None if invalid."""
        return self.confirmation_tokens.pop(token, None)

    def mark_visited(self, specialist_name: str) -> None:
        if specialist_name not in self.visited_specialists:
            self.visited_specialists.append(specialist_name)

    def increment_loop(self) -> None:
        self.loop_counter += 1

    def is_resolved(self) -> bool:
        return self.queue_drained and self.test_print_ok and self.device_ready

    # ------------------------------------------------------------------
    # Derived helpers used by can_handle() heuristics
    # ------------------------------------------------------------------

    @property
    def has_network_symptoms(self) -> bool:
        keywords = {"network", "ip", "ping", "connect", "timeout", "unreachable", "port"}
        combined = " ".join(self.symptoms + [self.user_description]).lower()
        return any(k in combined for k in keywords)

    @property
    def has_driver_symptoms(self) -> bool:
        keywords = {"driver", "spooler", "queue", "stuck", "offline", "cups", "ppd", "filter"}
        combined = " ".join(self.symptoms + [self.user_description]).lower()
        return any(k in combined for k in keywords)

    @property
    def has_device_symptoms(self) -> bool:
        keywords = {"ribbon", "media", "head", "jam", "calibrat", "error", "pause", "beep", "blink"}
        combined = " ".join(self.symptoms + [self.user_description]).lower()
        return any(k in combined for k in keywords)

    @property
    def network_unknown(self) -> bool:
        return self.network.reachable is None

    @property
    def device_unknown(self) -> bool:
        return self.device.printer_status == "unknown"

    @property
    def os_is_windows(self) -> bool:
        return self.os_platform == OSPlatform.WINDOWS

    @property
    def os_is_linux(self) -> bool:
        return self.os_platform == OSPlatform.LINUX