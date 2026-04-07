"""
logging_utils.py — Structured JSON logger for the ZT411 troubleshooter agent.

Usage
-----
    from zt411_agent.logging_utils import configure_logging, get_logger

    configure_logging()                    # call once at startup
    logger = get_logger(__name__)          # use per-module
    logger.info("msg", extra={"session_id": "abc"})

Output format
-------------
Each log line is a single JSON object:
    {"ts": "2026-01-01T00:00:00Z", "level": "INFO", "logger": "...", "msg": "...", ...extra}

Config is read from configs/logging.yaml when present; sane defaults apply otherwise.
"""

from __future__ import annotations

import json
import logging
import logging.config
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    # Fields to always include
    _CORE_FIELDS = ("ts", "level", "logger", "msg")

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        obj: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Structured extra fields (anything set via extra={} or LoggerAdapter)
        skip = {
            "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
            "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "created", "msecs", "relativeCreated", "thread", "threadName",
            "processName", "process", "message", "taskName",
        }
        for key, val in record.__dict__.items():
            if key not in skip and not key.startswith("_"):
                obj[key] = val

        # Exception info
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)

        return json.dumps(obj, default=str)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def configure_logging(
    level: str = "INFO",
    json_format: bool = True,
    config_path: str | Path | None = None,
) -> None:
    """
    Configure root logging for the agent process.

    Parameters
    ----------
    level       : Log level string ("DEBUG", "INFO", "WARNING", "ERROR").
    json_format : If True, emit JSON lines. If False, use a human-readable format.
    config_path : Optional path to a logging.yaml config.  When provided the file
                  is used directly via logging.config.dictConfig; other args are ignored.
    """
    if config_path is not None:
        _load_yaml_config(Path(config_path))
        return

    handler = logging.StreamHandler(sys.stdout)

    if json_format:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Suppress noisy third-party loggers
    for noisy in ("httpx", "httpcore", "sentence_transformers", "faiss"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str, **bound_fields: Any) -> logging.LoggerAdapter:
    """
    Return a LoggerAdapter for *name* with optional bound context fields.

    Example
    -------
        log = get_logger(__name__, session_id="abc123")
        log.info("Starting agent loop")
        # → {"ts": "...", "level": "INFO", "logger": "...", "msg": "Starting agent loop",
        #    "session_id": "abc123"}
    """
    logger = logging.getLogger(name)
    return logging.LoggerAdapter(logger, extra=bound_fields)


def session_logger(name: str, session_id: str) -> logging.LoggerAdapter:
    """Convenience wrapper that always binds session_id."""
    return get_logger(name, session_id=session_id)


# ---------------------------------------------------------------------------
# Internal: YAML config loader
# ---------------------------------------------------------------------------


def _load_yaml_config(path: Path) -> None:
    try:
        import yaml  # type: ignore[import]
        cfg = yaml.safe_load(path.read_text())
        logging.config.dictConfig(cfg)
    except Exception as exc:  # noqa: BLE001
        # Fall back to basic config if YAML loading fails
        logging.basicConfig(level=logging.INFO)
        logging.getLogger(__name__).warning(
            "Failed to load logging config from %s: %s — using defaults.", path, exc
        )
