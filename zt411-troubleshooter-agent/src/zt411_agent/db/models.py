"""
db/models.py — SQLAlchemy ORM models for the ZT411 session store.

The full AgentState is serialized to `state_json` (JSONB) for efficient
round-trip deserialization.  The top-level scalar columns are denormalised
copies used only for fast admin-list queries and filtering — they are always
kept in sync with `state_json` by the repository.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, Text
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class SessionRecord(Base):
    __tablename__ = "sessions"

    session_id = Column(String(36), primary_key=True)
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=True,
        onupdate=lambda: datetime.now(timezone.utc),
    )
    os_platform = Column(String(16), nullable=False, default="unknown")
    symptoms = Column(JSON, nullable=False, default=list)
    user_description = Column(Text, nullable=False, default="")
    loop_status = Column(String(16), nullable=False, default="running")
    loop_counter = Column(Integer, nullable=False, default=0)
    escalation_reason = Column(Text, nullable=False, default="")
    is_resolved = Column(Boolean, nullable=False, default=False)
    # Full serialised AgentState — source of truth for round-trips
    state_json = Column(JSON, nullable=False)
