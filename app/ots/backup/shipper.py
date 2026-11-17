"""Shipping WAL to SharePoint (M04 section 2): spool files become SHIP_WAL operations; tidy up after them."""

from __future__ import annotations

import datetime
import logging
from typing import Any

from django.db import transaction

from ots.core.clock import now
from ots.storage import queue
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library

from . import spool, wal
from .models import BackupObject

log = logging.getLogger(__name__)


def remote_path(name: str) -> str:
    """DR:PostgreSQL/WAL/<timeline>/<file>. A .history or .backup file belongs to the timeline in its name."""
    return f"PostgreSQL/WAL/{name[:8]}/{name}.zst.age"


def enqueue_wal() -> int:
    """One pass over the spool: queue what is new, tidy what is safely off-site. Returns how many were queued.

    Files are queued in segment-name order, so the queue's created order is the shipping order.
    """
    made = 0
    for path in spool.entries():
        name = spool.segment_name(path)
        existing = (
            Op.objects.filter(kind=Op.Kind.SHIP_WAL, local_path=str(path)).order_by("-created_at").first()
        )
        if existing is not None:
            if existing.state == Op.State.DONE:
                spool.mark_shipped(
                    path
                )  # the transaction committed but the process stopped before the tidy-up
            continue
        with transaction.atomic():
            queue.enqueue(
                Op.Kind.SHIP_WAL,
                Library.DR,
                path,
                remote_path(name),
                spool.sha256_of(path),
                path.stat().st_size,
                ref_type="wal",
                ref_id=name,
            )
        made += 1
    return made


def on_shipped(op: Op, ref: Any) -> None:
    """Completion handler for SHIP_WAL (runs inside the finishing transaction).

    Records the object as confirmed. The spool file is removed only after the transaction commits: if the
    commit failed, the operation would be retried and still needs its file.
    """
    from pathlib import Path

    path = Path(op.local_path)
    seg = wal.parse(op.ref_id)
    try:
        archived_at = datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.UTC)
    except (
        FileNotFoundError
    ):  # already tidied by an earlier attempt: the operation's own time is close enough
        archived_at = op.created_at
    BackupObject.objects.update_or_create(
        remote_path=op.remote_path,
        defaults={
            "type": BackupObject.Type.WAL,
            "name": op.ref_id,
            "sha256": op.sha256,
            "size_bytes": op.size_bytes,
            "wal_segment": op.ref_id,
            "wal_timeline": seg.timeline if seg else None,
            "wal_end_lsn": seg.end_lsn if seg else None,
            "wal_end_time": archived_at,
            "confirmed_at": now(),
            "pruned_at": None,
        },
    )
    transaction.on_commit(lambda: spool.mark_shipped(path))


def on_backup_uploaded(op: Op, ref: Any) -> None:
    """Completion handler for UPLOAD_BACKUP_OBJECT: the object the run queued is now confirmed."""
    BackupObject.objects.filter(remote_path=op.remote_path).update(confirmed_at=now(), sha256=op.sha256)
    from . import base

    for obj in BackupObject.objects.filter(
        remote_path=op.remote_path, type=BackupObject.Type.BASE
    ).select_related("run"):
        base.on_uploaded(obj)  # a base backup is SUCCESS only once SharePoint holds it
