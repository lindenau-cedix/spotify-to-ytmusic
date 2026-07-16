"""Structured logging: JSON to ./logs/migrator.log + human-readable to stdout.

Single helper `setup_logging()` should be called once at process start.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import get_settings


class JsonFormatter(logging.Formatter):
    """Render each LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Pull extra fields (anything set via `logger.info(..., extra={...})`)
        for k, v in record.__dict__.items():
            if k in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "taskName",
            }:
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except TypeError:
                payload[k] = repr(v)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(level: str | None = None) -> logging.Logger:
    """Configure the root logger and return a project logger."""
    settings = get_settings()
    level_name = (level or settings.log_level).upper()
    level_value = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    root.setLevel(level_value)

    # Reset handlers so re-init (tests, CLI re-run) doesn't duplicate output.
    for h in list(root.handlers):
        root.removeHandler(h)

    json_path: Path = settings.log_json_path
    json_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        json_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%H:%M:%S")
    )
    root.addHandler(stdout)

    # Quiet noisy third-party loggers.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("migrator")


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a project-namespaced logger. Call setup_logging() once first."""
    return logging.getLogger(f"migrator.{name}" if name else "migrator")