"""Structured logging that Cloud Logging can read, with a hard stop on PII.

Cloud Run forwards stdout to Cloud Logging. A JSON line with a ``severity`` field
becomes a structured entry with the right level; anything else lands as plain text
at INFO, which makes an error invisible in the console. So the renderer emits the
fields Cloud Logging looks for, and only those.

The interesting part is :func:`_reject_pii`. Resume and job description text must
never reach a log sink, and "remember not to log that" is not a control. This
processor drops any event carrying a banned key and replaces it with a complaint,
so the mistake shows up in review rather than in a log bucket.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.types import EventDict, Processor

from jobtrack_core.config.settings import Settings

# Field names that may never be logged. These are the shapes user content actually
# arrives in; anything holding free text from a resume or a posting belongs here.
_BANNED_KEYS: frozenset[str] = frozenset(
    {
        "address",
        "bullet",
        "bullets",
        "content",
        "cover_letter",
        "description",
        "email",
        "full_name",
        "jd_text",
        "name",
        "phone",
        "profile_item",
        "prompt",
        "raw_text",
        "response",
        "resume_text",
        "summary",
        "text",
    }
)

# Cloud Logging severities. structlog gives us lowercase Python level names.
_SEVERITY: dict[str, str] = {
    "debug": "DEBUG",
    "info": "INFO",
    "warning": "WARNING",
    "error": "ERROR",
    "critical": "CRITICAL",
    "exception": "ERROR",
}


def _reject_pii(_logger: object, _method: str, event_dict: EventDict) -> EventDict:
    offending = sorted(_BANNED_KEYS.intersection(event_dict))
    if not offending:
        return event_dict
    return {
        "event": "log_call_dropped_for_pii",
        "offending_keys": offending,
        "origin_event": str(event_dict.get("event", "")),
        "level": "error",
    }


def _to_cloud_logging(_logger: object, method: str, event_dict: EventDict) -> EventDict:
    level = str(event_dict.pop("level", method)).lower()
    event_dict["severity"] = _SEVERITY.get(level, "INFO")
    if "event" in event_dict:
        event_dict["message"] = event_dict.pop("event")
    return event_dict


def configure_logging(settings: Settings) -> None:
    """Install the logging configuration for this process. Safe to call twice."""
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _reject_pii,
    ]

    renderer: Processor
    if settings.is_local:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    else:
        shared.append(_to_cloud_logging)
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str, **initial: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound logger. ``name`` is the module, by convention ``__name__``."""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name, **initial)
    return logger
