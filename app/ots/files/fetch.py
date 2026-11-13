"""The fetch job for F-FL-6: bring archived files back into the cache for people who asked while
SharePoint was out of reach, then tell them."""

from __future__ import annotations

import datetime
from pathlib import Path

from django.db import transaction

from ots.core.clock import now
from ots.notifications import services as notifications
from ots.notifications.models import Kind
from ots.storage.factory import get_provider
from ots.storage.gate import SCOPE_ALL, QueueGate

from . import cache
from .models import Blob, FileFetchRequest, FileState, TaskFile

GIVE_UP_AFTER = datetime.timedelta(days=3)
PER_RUN = 3  # a fetch can be large; the job runs every minute, so a few at a time is plenty


def backoff(attempts: int) -> datetime.timedelta:
    """2, 4, 8 ... minutes, at most an hour: a copy that does not verify is not re-downloaded every minute."""
    return datetime.timedelta(minutes=min(2**attempts, 60))


def run() -> dict[str, int]:
    at = now()
    expired = FileFetchRequest.objects.filter(
        state=FileFetchRequest.State.PENDING, created_at__lt=at - GIVE_UP_AFTER
    ).update(state=FileFetchRequest.State.EXPIRED)
    stats = {"expired": expired, "fetched": 0, "waiting": 0}
    due = FileFetchRequest.objects.filter(state=FileFetchRequest.State.PENDING, next_attempt_at__lte=at)
    shas: list[str] = []
    for sha in due.order_by("created_at").values_list("version__blob_id", flat=True)[:50]:
        if sha not in shas:
            shas.append(sha)
        if len(shas) == PER_RUN:
            break
    if not shas:
        return stats
    block = QueueGate().block()
    if block is not None and block.scope == SCOPE_ALL:
        stats["waiting"] = len(shas)
        return stats
    provider = get_provider()
    for sha in shas:
        blob = Blob.objects.get(pk=sha)
        if not _in_cache(blob):
            if blob.state not in (FileState.ARCHIVED, FileState.DRIFTED):
                stats["waiting"] += 1
                continue
            outcome = cache.fill(blob, provider)
            if outcome == cache.CORRUPT:
                _back_off(sha)
            if outcome != cache.OK:
                stats["waiting"] += 1  # unreachable or busy: try again next minute; corrupt: after a back-off
                continue
        _complete(sha)
        stats["fetched"] += 1
    return stats


def _back_off(sha: str) -> None:
    for r in FileFetchRequest.objects.filter(version__blob_id=sha, state=FileFetchRequest.State.PENDING):
        r.attempts += 1
        r.next_attempt_at = now() + backoff(r.attempts)
        r.save(update_fields=["attempts", "next_attempt_at"])


def _in_cache(blob: Blob) -> bool:
    try:
        return bool(
            blob.cached and blob.local_path and Path(blob.local_path).stat().st_size == blob.size_bytes
        )
    except OSError:
        return False


def _complete(sha: str) -> None:
    with transaction.atomic():
        rows = list(
            FileFetchRequest.objects.select_for_update()
            .filter(version__blob_id=sha, state=FileFetchRequest.State.PENDING)
            .select_related("version", "user")
        )
        for r in rows:
            r.state, r.done_at = FileFetchRequest.State.DONE, now()
            r.save(update_fields=["state", "done_at"])
            link = TaskFile.objects.filter(version=r.version).select_related("task").first()
            if link is not None:  # the bell is about a task; a library file has none, the page itself says so
                notifications.notify(
                    [r.user_id], Kind.FILE_READY, link.task, f"File ready: {r.version.original_filename}",
                    body="It is available to download again.",
                )  # fmt: skip
