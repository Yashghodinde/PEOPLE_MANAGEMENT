"""Completion handlers: what to do, in the same transaction, when an upload is verified.

`files` registers `ARCHIVE_FILE` and `REPAIR_FILE` (M08: blob and version become ARCHIVED, `file.archived`
is published); `backup` registers `SHIP_WAL` and `UPLOAD_BACKUP_OBJECT` (M04: records `backup_objects`).
A handler runs inside the finishing transaction: if it raises, the operation is NOT marked done and is
retried, so "off-site" can never be claimed for something the database does not know about.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from .provider import RemoteRef

if TYPE_CHECKING:
    from .models import StorageOperation

Handler = Callable[["StorageOperation", RemoteRef], None]
_HANDLERS: dict[str, Handler] = {}


def register_completion_handler(kind: str, fn: Handler) -> None:
    if kind in _HANDLERS and _HANDLERS[kind] is not fn:
        raise RuntimeError(f"completion handler already registered for {kind}")
    _HANDLERS[kind] = fn


def handler_for(kind: str) -> Handler | None:
    return _HANDLERS.get(kind)


def clear_handlers() -> None:
    """For tests."""
    _HANDLERS.clear()
