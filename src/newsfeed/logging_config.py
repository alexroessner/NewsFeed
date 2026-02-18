"""Structured JSON logging for NewsFeed.

Provides a JSON formatter that outputs one JSON object per log line,
compatible with Cloudflare Workers, AWS CloudWatch, Datadog, etc.

Usage:
    from newsfeed.logging_config import configure_logging
    configure_logging(level="INFO", json_format=True)
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter.

    Output format:
    {"ts": "2026-02-17T18:04:12.934Z", "level": "INFO", "logger": "newsfeed.engine",
     "msg": "Research produced 18 candidates", "request_id": "abc123", ...}
    """

    # Fields that should never appear in log output (security)
    _REDACT_FIELDS = frozenset({
        "password", "secret", "token", "api_key", "bearer",
        "authorization", "cookie", "credential",
    })

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
                  + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        # Add source location for warnings and errors
        if record.levelno >= logging.WARNING:
            entry["file"] = f"{record.filename}:{record.lineno}"

        # Add exception info if present
        if record.exc_info and record.exc_info[1]:
            entry["error"] = str(record.exc_info[1])
            entry["error_type"] = type(record.exc_info[1]).__name__

        # Add any extra fields from the record
        for key in ("request_id", "user_id", "agent_id", "duration_ms", "stage"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val

        return json.dumps(entry, default=str)


def configure_logging(
    level: str = "INFO",
    json_format: bool | None = None,
) -> None:
    """Configure root logger with optional JSON formatting.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        json_format: If True, use JSON formatter. If None, auto-detect
                     (JSON in CI/production, plain text in development).
    """
    if json_format is None:
        # Auto-detect: JSON in CI or when NEWSFEED_LOG_JSON is set
        json_format = bool(
            os.environ.get("CI")
            or os.environ.get("GITHUB_ACTIONS")
            or os.environ.get("NEWSFEED_LOG_JSON")
        )

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    if json_format:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    root.addHandler(handler)

    # Silence noisy third-party loggers
    for noisy in ("urllib3", "httpcore", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
