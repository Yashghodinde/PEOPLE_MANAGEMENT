"""Completion handlers: what to do, in the same transaction, when an upload is verified.

`files` registers `ARCHIVE_FILE` and `REPAIR_FILE` (M08: blob and version become ARCHIVED, `file.archived`
is published); `backup` registers `SHIP_WAL` and `UPLOAD_BACKUP_OBJECT` (M04: records `backup_objects`).
A handler runs inside the finishing transaction: if it raises, the operation is NOT marked done and is
retried, so "off-site" can never be claimed for something the database does not know about.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .provider import RemoteMeta, RemoteRef

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
    _RECONCILE.clear()


@dataclass(frozen=True)
class ReconcileHooks:
    """What an owning module wants to know when reconciliation finds trouble with one of its uploads.

    `files` (M08) uses these to mark a blob MISSING or DRIFTED and fix its stored path; `backup` (M04)
    does the same for backup objects. All three are optional and run inside the reconciliation transaction.
    """

    on_missing: Callable[[StorageOperation, RemoteMeta | None], None] | None = None
    on_drifted: Callable[[StorageOperation, RemoteMeta], None] | None = None
    on_moved: Callable[[StorageOperation, RemoteMeta], None] | None = None


_RECONCILE: dict[str, ReconcileHooks] = {}


def register_reconcile_hooks(kind: str, hooks: ReconcileHooks) -> None:
    _RECONCILE[kind] = hooks


def reconcile_hooks_for(kind: str) -> ReconcileHooks:
    return _RECONCILE.get(kind, ReconcileHooks())
