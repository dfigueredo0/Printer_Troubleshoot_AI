"""
db/store.py — Strategy-pattern session store.

Two concrete implementations:
  - MemorySessionStore  (default; no DB required; safe for tests)
  - DatabaseSessionStore (SQLAlchemy + PostgreSQL; activated via USE_DB=true)

Pick one at startup with get_session_store().
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class SessionStore(ABC):
    @abstractmethod
    def get(self, session_id: str):
        """Return AgentState or None."""

    @abstractmethod
    def save(self, state) -> None:
        """Upsert an AgentState."""

    @abstractmethod
    def delete(self, session_id: str) -> bool:
        """Remove a session.  Returns True if it existed."""

    @abstractmethod
    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[dict]:
        """Return lightweight session summaries for admin listing."""


# ---------------------------------------------------------------------------
# In-memory implementation (tests / offline mode)
# ---------------------------------------------------------------------------


class MemorySessionStore(SessionStore):
    def __init__(self):
        self._data: dict[str, Any] = {}

    def get(self, session_id: str):
        return self._data.get(session_id)

    def save(self, state) -> None:
        self._data[state.session_id] = state

    def delete(self, session_id: str) -> bool:
        if session_id in self._data:
            del self._data[session_id]
            return True
        return False

    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[dict]:
        items = sorted(
            self._data.values(),
            key=lambda s: s.created_at,
            reverse=True,
        )
        return [
            {
                "session_id": s.session_id,
                "created_at": s.created_at.isoformat(),
                "os_platform": s.os_platform.value,
                "symptoms": s.symptoms,
                "loop_status": s.loop_status.value,
                "loop_counter": s.loop_counter,
                "is_resolved": s.is_resolved(),
            }
            for s in items[offset : offset + limit]
        ]


# ---------------------------------------------------------------------------
# Database-backed implementation (PostgreSQL)
# ---------------------------------------------------------------------------


class DatabaseSessionStore(SessionStore):
    def __init__(self, database_url: str):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from .models import Base

        self._engine = create_engine(database_url, pool_pre_ping=True)
        Base.metadata.create_all(bind=self._engine)
        self._SessionLocal = sessionmaker(bind=self._engine, autocommit=False, autoflush=False)
        logger.info("DatabaseSessionStore initialised", extra={"url": database_url.split("@")[-1]})

    def _db(self):
        return self._SessionLocal()

    def get(self, session_id: str):
        from .models import SessionRecord
        from ..state import AgentState

        with self._db() as db:
            record = db.get(SessionRecord, session_id)
            if record is None:
                return None
            try:
                return AgentState.model_validate(record.state_json)
            except Exception as exc:
                logger.error("Failed to deserialise session %s: %s", session_id, exc)
                return None

    def save(self, state) -> None:
        from .models import SessionRecord

        state_dict = state.model_dump(mode="json")
        with self._db() as db:
            record = db.get(SessionRecord, state.session_id)
            now = datetime.now(timezone.utc)
            if record is None:
                record = SessionRecord(
                    session_id=state.session_id,
                    created_at=state.created_at,
                    updated_at=now,
                    os_platform=state.os_platform.value,
                    symptoms=state.symptoms,
                    user_description=state.user_description,
                    loop_status=state.loop_status.value,
                    loop_counter=state.loop_counter,
                    escalation_reason=state.escalation_reason,
                    is_resolved=state.is_resolved(),
                    state_json=state_dict,
                )
                db.add(record)
            else:
                record.updated_at = now
                record.os_platform = state.os_platform.value
                record.symptoms = state.symptoms
                record.user_description = state.user_description
                record.loop_status = state.loop_status.value
                record.loop_counter = state.loop_counter
                record.escalation_reason = state.escalation_reason
                record.is_resolved = state.is_resolved()
                record.state_json = state_dict
            db.commit()

    def delete(self, session_id: str) -> bool:
        from .models import SessionRecord

        with self._db() as db:
            record = db.get(SessionRecord, session_id)
            if record is None:
                return False
            db.delete(record)
            db.commit()
            return True

    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[dict]:
        from .models import SessionRecord
        from sqlalchemy import desc

        with self._db() as db:
            records = (
                db.query(SessionRecord)
                .order_by(desc(SessionRecord.created_at))
                .offset(offset)
                .limit(limit)
                .all()
            )
            return [
                {
                    "session_id": r.session_id,
                    "created_at": r.created_at.isoformat(),
                    "os_platform": r.os_platform,
                    "symptoms": r.symptoms,
                    "loop_status": r.loop_status,
                    "loop_counter": r.loop_counter,
                    "is_resolved": r.is_resolved,
                }
                for r in records
            ]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_session_store() -> SessionStore:
    """
    Return the appropriate session store based on environment:
      USE_DB=true  →  DatabaseSessionStore(DATABASE_URL)
      otherwise    →  MemorySessionStore (safe default for tests)
    """
    use_db = os.environ.get("USE_DB", "false").lower() == "true"
    database_url = os.environ.get("DATABASE_URL", "")

    if use_db and database_url:
        try:
            return DatabaseSessionStore(database_url)
        except Exception as exc:
            logger.warning(
                "Could not connect to PostgreSQL (%s); falling back to in-memory store.", exc
            )
    return MemorySessionStore()
