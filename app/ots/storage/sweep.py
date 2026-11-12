"""Orphan sweep (M05 section 5): files in Business-Files that no upload record explains.

Never deletes. An unknown file is either
* the result of an upload whose database commit failed (a pending ARCHIVE_FILE / REPAIR_FILE operation has the
  same path and the same content): adopt it, so the version reaches ARCHIVED and nothing is uploaded twice
  (F-FL-4); or
* a stranger: moved to /_quarantine/ with a WARNING, so a person can look at it.
Two safety rules keep the sweep from acting on something that is just being written: a file an active
operation owns (it may be uploading this very minute) is left alone, and so is anything modified in the last
15 minutes.
"""

from __future__ import annotations

import datetime
import logging
from collections import Counter
from collections.abc import Callable

from django.db import transaction

from ots.audit import api as audit
from ots.core import alerts
from ots.core.clock import now as server_now

from . import classify as c
from .errors import StorageProviderError
from .factory import get_provider
from .fake import QUARANTINE
from .gate import Permission, QueueGate
from .gate import gate as default_gate
from .models import StorageOperation as Op
from .provider import Library, StorageProvider
from .queue import adopt_existing

log = logging.getLogger(__name__)
GRACE = datetime.timedelta(minutes=15)
ADOPTABLE = (Op.Kind.ARCHIVE_FILE, Op.Kind.REPAIR_FILE)


def run(
    provider: StorageProvider | None = None,
    gate: QueueGate | None = None,
    clock: Callable[[], datetime.datetime] = server_now,
) -> Counter[str]:
    """The `storage.orphan_sweep` job body. Returns counts of what it did."""
    gate = gate or default_gate
    if gate.permission().permission != Permission.RUN:
        return Counter(skipped=1)
    provider = provider or get_provider()
    stats: Counter[str] = Counter()
    try:
        listing, _ = provider.changes(Library.BUSINESS, None)  # a full listing: what exists right now
        known = set(
            Op.objects.filter(target_library=Library.BUSINESS.value, state=Op.State.DONE)
            .exclude(item_id="")
            .values_list("item_id", flat=True)
        )
        t = clock()
        for meta in listing:
            if meta.ref.item_id in known or meta.path.startswith(f"{QUARANTINE}/"):
                continue
            if meta.last_modified > t - GRACE:
                stats["too_recent"] += 1
                continue
            owner = Op.objects.filter(
                target_library=Library.BUSINESS.value, remote_path=meta.path, state__in=Op.ACTIVE
            ).first()
            if owner is not None:
                stats[_owned(provider, owner, meta, clock)] += 1
                continue
            _quarantine(provider, meta)
            stats["quarantined"] += 1
    except StorageProviderError as exc:
        gate.failure(exc)
        raise
    gate.success()
    return stats


def _owned(provider: StorageProvider, owner: Op, meta, clock: Callable[[], datetime.datetime]) -> str:  # type: ignore[no-untyped-def]
    """The path belongs to an operation that is still open."""
    if owner.state in (Op.State.PENDING, Op.State.RETRY_WAIT) and owner.kind in ADOPTABLE:
        return {"adopted": "adopted", "busy": "in_flight", "mismatch": "left_for_conflict"}[
            adopt_existing(provider, owner, meta, clock)
        ]
    return "in_flight"  # RUNNING: a worker is on it right now


def _quarantine(provider: StorageProvider, meta) -> None:  # type: ignore[no-untyped-def]
    with transaction.atomic():
        moved = provider.move_to_quarantine(meta.ref)
        alerts.raise_alert(
            c.ALERT_ORPHAN, "WARNING", "An unknown file in Business-Files was moved to quarantine",
            f"{meta.path} (item {meta.ref.item_id}, {meta.size} bytes) -> /{QUARANTINE}/",
        )  # fmt: skip
        audit.record(
            "storage.orphan_quarantined", None, ("storage_item", meta.ref.item_id),
            {"path": meta.path, "size": meta.size, "moved_item": moved.item_id},
        )  # fmt: skip
