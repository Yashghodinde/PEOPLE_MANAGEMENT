"""JSON-lines logging with request-scoped context (request_id, user_id, event)."""

import logging
from contextvars import ContextVar
from typing import Any

from pythonjsonlogger.json import JsonFormatter

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
user_id_var: ContextVar[str] = ContextVar("user_id", default="-")


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.user_id = user_id_var.get()
        if not hasattr(record, "event"):
            record.event = "-"
        return True


class OtsJsonFormatter(JsonFormatter):
    def add_fields(
        self, log_data: dict[str, Any], record: logging.LogRecord, message_dict: dict[str, Any]
    ) -> None:
        super().add_fields(log_data, record, message_dict)
        log_data["ts"] = self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z")
        log_data["level"] = record.levelname
        log_data["logger"] = record.name
        log_data["msg"] = record.getMessage()
