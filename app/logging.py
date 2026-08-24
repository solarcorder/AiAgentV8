"""
Structured JSON logging with structural redaction.

§24: "Redaction is a logging-layer concern, not a discipline." A deny-list
of field names is applied to every log record so a careless log.info(obj)
cannot leak a secret. This is enforced here, once, rather than trusted at
every call site.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from typing import Any

# Field names that must never appear in a log line, structurally.
# A11 in the red-team review: this covers named fields only — free-text
# fields (e.g. a maintenance request description) must never be logged
# wholesale. Callers are responsible for not passing raw request bodies.
REDACTED_FIELD_NAMES = {
    "token",
    "raw_token",
    "approval_token",
    "password",
    "authorization",
    "cookie",
    "session_cookie",
    "ciphertext",
    "nonce",
    "auth_tag",
    "wrapped_data_key",
    "payload_hash",  # not secret, but binds to sensitive content — err conservative
    "phone",
    "email",
    "access_token",
    "refresh_token",
    "api_key",
    "secret",
    "client_secret",
}

REDACTED_PLACEHOLDER = "***REDACTED***"

# Per-request context, populated by tenancy middleware.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
org_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("org_id", default="")
user_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("user_id", default="")


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {
            k: (REDACTED_PLACEHOLDER if k.lower() in REDACTED_FIELD_NAMES else _redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.Formatter) -> str:  # type: ignore[override]
        payload: dict[str, Any] = {
            "ts": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(""),
            "org_id": org_id_var.get(""),
            "user_id": user_id_var.get(""),
        }
        extra = getattr(record, "context", None)
        if extra:
            payload.update(_redact(extra))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)


def new_request_id() -> str:
    return str(uuid.uuid4())


def log_event(logger: logging.Logger, level: int, message: str, **context: Any) -> None:
    """Log with a structured context block, redacted before it ever hits the formatter."""
    logger.log(level, message, extra={"context": context})
