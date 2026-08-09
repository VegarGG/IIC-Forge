"""Structured, credential-redacting logs for production processes."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any


_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "iic_correlation_id", default=None
)
_SENSITIVE_NAME = re.compile(
    r"(?i)(api[_-]?key|token|password|secret|authorization|cookie|session)"
)
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret|authorization|cookie|session)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_URL_USERINFO = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^/@\s]+@", re.I)


def set_correlation_id(value: str | None) -> contextvars.Token:
    """Set the correlation id for logs in the current execution context."""
    return _correlation_id.set(value[:160] if value else None)


def reset_correlation_id(token: contextvars.Token) -> None:
    _correlation_id.reset(token)


def redact(value: Any) -> str:
    """Return bounded text with credentials replaced, never echoed."""
    text = str(value).replace("\x00", "")
    for name, secret in os.environ.items():
        if _SENSITIVE_NAME.search(name) and len(secret) >= 4:
            text = text.replace(secret, "[REDACTED]")
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _ASSIGNMENT.sub(lambda m: f"{m.group(1)}=[REDACTED]", text)
    text = _URL_USERINFO.sub(lambda m: f"{m.group('scheme')}[REDACTED]@", text)
    return text[:8000]


def redact_fields(values: dict[str, Any]) -> dict[str, str]:
    """Redact a bounded metadata mapping by both key name and value."""
    return {
        str(key)[:80]: (
            "[REDACTED]" if _SENSITIVE_NAME.search(str(key)) else redact(value)[:500]
        )
        for key, value in values.items()
        if value is not None
    }


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(value)


class JsonFormatter(logging.Formatter):
    """One-line JSON formatter suited to Docker's json-file driver."""

    _standard = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

    def __init__(self, *, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        correlation = getattr(record, "correlation_id", None) or _correlation_id.get()
        if correlation:
            payload["correlation_id"] = redact(correlation)
        for key, value in record.__dict__.items():
            if key in self._standard or key.startswith("_") or key in payload:
                continue
            if key in {"args", "exc_info", "exc_text", "stack_info"}:
                continue
            payload[key] = _safe_value(value)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def configure_logging(service_name: str, *, level: int = logging.INFO) -> None:
    """Replace root handlers with the production JSON/redaction contract."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service_name=service_name))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
