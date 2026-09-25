"""Structured JSON logging with correlation ids and secret redaction."""

from __future__ import annotations

import json
import logging
from typing import Any

from app.observability.context import (
    generation_id_var,
    get_request_id,
    request_id_var,
    user_id_var,
)
from app.observability.redaction import redact


class JsonFormatter(logging.Formatter):
    """Renders log records as single-line JSON with correlation ids; redacts secrets.

    ``service`` / ``version`` identify the INSTANCE. Several template-born services ship logs into
    one aggregator, and without these fields their records are indistinguishable — SERVICE_NAME is
    "the one place where the service names itself", in logs and metrics, not only in OpenAPI.
    """

    def __init__(self, *, service: str, version: str) -> None:
        super().__init__()
        self._service = service
        self._version = version

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service,
            "version": self._version,
            "requestId": request_id_var.get(),
            "generationId": generation_id_var.get(),
            "userId": user_id_var.get(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps({k: v for k, v in payload.items() if v is not None})


def configure_logging(level: str, *, service: str, version: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter(service=service, version=version))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Log a structured event; fields are redacted by the formatter."""
    logger.log(level, message, extra={"extra_fields": {**fields, "requestId": get_request_id()}})
