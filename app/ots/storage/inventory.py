"""Monthly full inventory (M05 section 5): everything in SharePoint against everything we believe is there.

Heavy, so it runs alone (1st of the month, 03:00). It reuses the nightly comparison on the complete
listing: missing files, renamed files, and files whose size or hash no longer matches. On top of that it
spot-checks the real bytes of about 2% of our files: it streams the download in 1 MiB chunks, computes
SHA-256 and throws the data away. That catches damage a metadata comparison cannot see, and it keeps
memory flat.
"""

from __future__ import annotations

import hashlib
import logging
import random
from collections import Counter
from collections.abc import Callable

from django.db import transaction

from ots.core import alerts

from . import classify as c
from . import reconcile
from .errors import GraphError, StorageProviderError
from .factory import get_provider
from .gate import Permission, QueueGate
from .gate import gate as default_gate
from .handlers import reconcile_hooks_for
from .models import StorageOperation as Op
from .provider import Library, StorageProvider

log = logging.getLogger(__name__)
SAMPLE_RATE = 0.02


def run(
    provider: StorageProvider | None = None,
    gate: QueueGate | None = None,
    sample_rate: float = SAMPLE_RATE,
    rng: Callable[[], float] = random.random,
) -> Counter[str]:
    """The `storage.full_inventory` job body. Returns counts of what it checked and found."""
    gate = gate or default_gate
    if gate.permission().permission != Permission.RUN:
        return Counter(skipped=1)
    provider = provider or get_provider()
    total: Counter[str] = Counter()
    for library in reconcile.LIBRARIES:
        try:
            listing, _ = provider.changes(library, None)  # the complete listing
            total["listed"] += len(listing)
            total += reconcile._apply(provider, library, listing, full=True)
            known = set(
                Op.objects.filter(target_library=library.value, state=Op.State.DONE)
                .exclude(remote_state=Op.RemoteState.SUPERSEDED)
                .values_list("item_id", flat=True)
            )
            total["unknown"] += sum(1 for m in listing if m.ref.item_id not in known and not m.deleted)
            total += _spot_check(provider, library, [m.ref.item_id for m in listing], sample_rate, rng)
        except StorageProviderError as exc:
            gate.failure(exc)
            raise
    gate.success()
    return total


def _spot_check(
    provider: StorageProvider, library: Library, item_ids: list[str], rate: float, rng: Callable[[], float]
) -> Counter[str]:
    """Download a random sample and compare SHA-256 with what we recorded at upload."""
    stats: Counter[str] = Counter()
    ops = Op.objects.filter(
        target_library=library.value,
        state=Op.State.DONE,
        remote_state=Op.RemoteState.OK,
        item_id__in=item_ids,
    )
    for op in ops.iterator(chunk_size=200):
        if rng() >= rate:
            continue
        stats["spot_checked"] += 1
        digest, size = hashlib.sha256(), 0
        try:
            for chunk in provider.open_read(_ref(op)):
                digest.update(chunk)
                size += len(chunk)
        except GraphError as exc:
            if (
                exc.status == 404
            ):  # vanished between the listing and the download: the nightly run will see it
                continue
            raise
        if digest.hexdigest() != op.sha256 or size != op.size_bytes:
            stats["spot_failed"] += 1
            _mark_damaged(op, size)
    return stats


def _ref(op: Op):  # type: ignore[no-untyped-def]
    from .provider import RemoteRef

    return RemoteRef(Library(op.target_library), op.drive_id, op.item_id)


def _mark_damaged(op: Op, size: int) -> None:
    with transaction.atomic():
        if not Op.objects.filter(pk=op.pk, remote_state=Op.RemoteState.OK).update(
            remote_state=Op.RemoteState.DRIFTED
        ):
            return
        alerts.raise_alert(
            c.ALERT_CONTENT_CHANGED, "CRITICAL", "A file in SharePoint no longer matches what we uploaded",
            f"{op.remote_path} (item {op.item_id}): the downloaded bytes differ from the recorded SHA-256",
        )  # fmt: skip
        hook = reconcile_hooks_for(op.kind).on_drifted
        if hook is not None:
            hook(op, None)  # type: ignore[arg-type]
