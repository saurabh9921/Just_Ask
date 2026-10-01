"""Structured JSON logging with mandatory secret redaction.

Every log record is emitted as a single JSON object carrying the request
correlation ID and (once authenticated) tenant/user identifiers from context
variables. A redaction filter runs on *every* record so that passwords, tokens,
DSN credentials and API keys cannot reach log storage even if a developer logs
them by mistake.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Final

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
tenant_id_var: ContextVar[str | None] = ContextVar("tenant_id", default=None)
user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)

REDACTED: Final = "[REDACTED]"

# Keys whose values are always redacted, matched case-insensitively as substrings.
_SENSITIVE_KEY_PATTERN: Final = re.compile(
    r"(pass(word|wd)?|secret|token|authorization|api[_-]?key|cookie|credential|"
    r"private[_-]?key|encryption[_-]?key|dsn|session[_-]?id)",
    re.IGNORECASE,
)

# Value patterns scrubbed from free text (messages, exception text).
_VALUE_PATTERNS: Final[tuple[tuple[re.Pattern[str], str], ...]] = (
    # scheme://user:password@host  ->  scheme://user:[REDACTED]@host
    (
        re.compile(r"(?P<pre>[a-zA-Z][a-zA-Z0-9+.\-]*://[^:/\s@]*:)[^@\s/]+(?=@)"),
        r"\g<pre>" + REDACTED,
    ),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"), r"\1" + REDACTED),
    # JWT-shaped strings
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*"), REDACTED),
    # key=value / key: value pairs for sensitive keys
    (
        re.compile(
            r"(?i)\b((?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key)"
            r"\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
        ),
        r"\1" + REDACTED,
    ),
)

_MAX_DEPTH: Final = 6


def redact_text(text: str) -> str:
    """Scrub credential-shaped substrings from free text."""
    for pattern, replacement in _VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact sensitive keys and credential-shaped strings."""
    if _depth > _MAX_DEPTH:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        return {
            str(k): (
                REDACTED if _SENSITIVE_KEY_PATTERN.search(str(k)) else redact(v, _depth=_depth + 1)
            )
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(v, _depth=_depth + 1) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


# Attributes present on every LogRecord; anything else was passed via ``extra=``.
_STANDARD_ATTRS: Final = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "taskName"}
)


class JsonFormatter(logging.Formatter):
    """Render records as one-line JSON objects with redaction applied."""

    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "message": redact_text(record.getMessage()),
        }
        for key, var in (
            ("request_id", request_id_var),
            ("tenant_id", tenant_id_var),
            ("user_id", user_id_var),
        ):
            ctx_value = var.get()
            if ctx_value is not None:
                payload[key] = ctx_value

        extras = {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS}
        if extras:
            payload["extra"] = redact(extras)

        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
            payload["exc"] = redact_text(self.formatException(record.exc_info))

        return json.dumps(payload, default=str, ensure_ascii=False)


class PlainFormatter(logging.Formatter):
    """Human-readable formatter for local development; still redacts."""

    def format(self, record: logging.LogRecord) -> str:
        rid = request_id_var.get() or "-"
        base = f"{record.levelname:<8} {record.name} [{rid}] {redact_text(record.getMessage())}"
        if record.exc_info and record.exc_info[0] is not None:
            base += "\n" + redact_text(self.formatException(record.exc_info))
        return base


def configure_logging(*, level: str, json_output: bool, service: str) -> None:
    """Install the root handler. Idempotent: safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service) if json_output else PlainFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn's access log duplicates our request log and would log raw query strings.
    logging.getLogger("uvicorn.access").disabled = True
    for noisy in ("uvicorn", "uvicorn.error"):
        logging.getLogger(noisy).handlers.clear()
        logging.getLogger(noisy).propagate = True
    # SQLAlchemy echo would log bound parameters; keep it at WARNING.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
