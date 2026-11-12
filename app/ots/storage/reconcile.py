"""Nightly reconciliation (M05 section 5): does SharePoint still hold what we archived?

The Graph delta feed lists what changed since the last run. For each changed item that matches one of our
finished uploads (matched by item id, never by name):
* deleted         -> remote_state MISSING, CRITICAL `storage.missing`, and if the local cache still has the
                     file, a REPAIR_FILE upload (same name, or `.restored-<date>` if the name is taken again);
* renamed/moved   -> INFO `storage.drifted`, the stored path is updated (identity is the item id, so nothing
                     is wrong);
* content differs -> remote_state DRIFTED and CRITICAL `storage.content_changed`; a person decides.
When there is no usable cursor (the first run, or Graph answered 410) the whole listing is compared with what
we know, so deletions the feed can no longer report are still found.
Everything here is safe to repeat: a change is acted on only on the OK -> MISSING / DRIFTED transition.
"""

from __future__ import annotations

import datetime
import logging
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from django.db import transaction

from ots.core import alerts
from ots.core.clock import now as server_now
from ots.core.clock import today_local

from . import classify as c
from .errors import GraphError, StorageProviderError
from .factory import get_provider
from .gate import Permission, QueueGate
from .gate import gate as default_gate
from .handlers import reconcile_hooks_for
from .models import StorageCursor
from .models import StorageOperation as Op
from .provider import Library, RemoteMeta, StorageProvider
from .queue import enqueue

log = logging.getLogger(__name__)
LIBRARIES = (Library.BUSINESS, Library.DR)
REPAIRABLE = (
    Op.Kind.ARCHIVE_FILE,
    Op.Kind.REPAIR_FILE,
)  # files have a local cache copy; WAL and backups do not


def run(
    provider: StorageProvider | None = None,
    gate: QueueGate | None = None,
    clock: Callable[[], datetime.datetime] = server_now,
) -> Counter[str]:
    """The `storage.delta_reconcile` job body. Returns counts of what it found."""
    gate = gate or default_gate
    if gate.permission().permission != Permission.RUN:
        return Counter(skipped=1)  # blocked, throttled or offline: tonight's run waits for tomorrow
    provider = provider or get_provider()
    total: Counter[str] = Counter()
    for library in LIBRARIES:
        try:
            total += _reconcile_library(provider, library)
        except StorageProviderError as exc:
            gate.failure(exc)
            raise
    gate.success()
    return total


def _reconcile_library(provider: StorageProvider, library: Library) -> Counter[str]:
    row = StorageCursor.objects.filter(library=library.value).first()
    cursor = row.delta_link if row else None
    try:
        items, new_cursor = provider.changes(library, cursor)
    except GraphError as exc:
        if exc.status != 410:  # the cursor expired: start again from a full listing
            raise
        log.warning(
            "delta cursor expired; comparing the full listing", extra={"event": "storage.delta_resync"}
        )
        cursor = None
        items, new_cursor = provider.changes(library, None)
    stats = _apply(provider, library, items, full=cursor is None)
    StorageCursor.objects.update_or_create(library=library.value, defaults={"delta_link": new_cursor})
    return stats


def _apply(
    provider: StorageProvider, library: Library, items: list[RemoteMeta], *, full: bool
) -> Counter[str]:
    stats: Counter[str] = Counter()
    ids = [m.ref.item_id for m in items]
    known = {
        op.item_id: op
        for op in Op.objects.filter(
            target_library=library.value, state=Op.State.DONE, item_id__in=ids
        ).exclude(remote_state=Op.RemoteState.SUPERSEDED)
    }
    for meta in items:
        op = known.get(meta.ref.item_id)
        if op is None:
            continue  # not ours (an orphan: the sweep deals with it) or an upload still in flight
        with transaction.atomic():
            if meta.deleted:
                stats["missing"] += _mark_missing(provider, op, meta)
                continue
            if meta.path and meta.path != op.remote_path:
                stats["moved"] += _mark_moved(op, meta)
            hash_differs = bool(
                meta.quick_xor_hash and op.remote_hash and meta.quick_xor_hash != op.remote_hash
            )
            if meta.size != op.size_bytes or hash_differs:
                stats["content_changed"] += _mark_content_changed(op, meta)
    if full:
        listed = {m.ref.item_id for m in items if not m.deleted}
        for pk, item_id in (
            Op.objects.filter(
                target_library=library.value, state=Op.State.DONE, remote_state=Op.RemoteState.OK
            )
            .exclude(item_id="")
            .values_list("pk", "item_id")
            .iterator()
        ):
            if item_id not in listed:
                with transaction.atomic():
                    stats["missing"] += _mark_missing(provider, Op.objects.get(pk=pk), None)
    return stats


def _mark_missing(provider: StorageProvider, op: Op, meta: RemoteMeta | None) -> int:
    if not Op.objects.filter(pk=op.pk, remote_state=Op.RemoteState.OK).update(
        remote_state=Op.RemoteState.MISSING
    ):
        return 0  # already known: do nothing twice
    alerts.raise_alert(
        c.ALERT_MISSING, "CRITICAL", "A file we archived is missing from SharePoint",
        f"{op.remote_path} ({op.kind}, item {op.item_id}, operation {op.pk})",
    )  # fmt: skip
    hook = reconcile_hooks_for(op.kind).on_missing
    if hook is not None:
        hook(op, meta)
    if op.kind in REPAIRABLE and Path(op.local_path).is_file():
        _enqueue_repair(provider, op)
    return 1


def _enqueue_repair(provider: StorageProvider, op: Op) -> None:
    library = Library(op.target_library)
    if (
        op.ref_id
        and Op.objects.filter(
            kind=Op.Kind.REPAIR_FILE, ref_type=op.ref_type, ref_id=op.ref_id, state__in=Op.ACTIVE
        ).exists()
    ):
        return  # a repair for this file is already on its way
    path = op.remote_path
    if provider.find_by_path(library, path) is not None:  # the name was taken again in the meantime
        path = f"{path}.restored-{today_local():%Y%m%d}"
    enqueue(
        Op.Kind.REPAIR_FILE, library, op.local_path, path, op.sha256, op.size_bytes,
        ref_type=op.ref_type, ref_id=op.ref_id,
    )  # fmt: skip


def _mark_moved(op: Op, meta: RemoteMeta) -> int:
    old = op.remote_path
    Op.objects.filter(pk=op.pk).update(remote_path=meta.path)
    op.remote_path = meta.path
    alerts.raise_alert(
        c.ALERT_DRIFTED, "INFO", "A file was renamed or moved in SharePoint", f"{old} -> {meta.path}"
    )
    hook = reconcile_hooks_for(op.kind).on_moved
    if hook is not None:
        hook(op, meta)
    return 1


def _mark_content_changed(op: Op, meta: RemoteMeta) -> int:
    if not Op.objects.filter(pk=op.pk, remote_state=Op.RemoteState.OK).update(
        remote_state=Op.RemoteState.DRIFTED
    ):
        return 0
    alerts.raise_alert(
        c.ALERT_CONTENT_CHANGED, "CRITICAL", "A file archived in SharePoint was changed there",
        f"{meta.path or op.remote_path} (item {op.item_id}): the stored hash no longer matches",
    )  # fmt: skip
    hook = reconcile_hooks_for(op.kind).on_drifted
    if hook is not None:
        hook(op, meta)
    return 1
