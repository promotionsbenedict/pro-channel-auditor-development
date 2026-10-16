from __future__ import annotations

import logging
import re
import sys
from typing import Any

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"), "[REDACTED_BOT_TOKEN]"),
    (re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]+\b"), "[REDACTED_PAYSTACK_KEY]"),
    (re.compile(r"(?i)\b(api_hash|password|session_string|session|secret|token|otp|phone_code|login_code)\b(\s*[=:]\s*)(\S+)"), r"\1\2[REDACTED]"),
    # Telethon StringSession values and other long opaque blobs.
    (re.compile(r"\b[A-Za-z0-9+/_=-]{120,}\b"), "[REDACTED_BLOB]"),
)


def redact(value: str) -> str:
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


_SENSITIVE_KEY = re.compile(
    r"(?i)(api_hash|password|passwd|session|secret|token|otp|phone_code|login_code|srp|authorization|signature|init_data)"
)


def redact_any(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if isinstance(key, str) and _SENSITIVE_KEY.search(key) else redact_any(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_any(item) for item in value]
    return value


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
