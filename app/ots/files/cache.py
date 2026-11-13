"""The local cache of file bytes (M08 section 4) and filling it from SharePoint.

Eviction deletes the local copy of ARCHIVED blobs only, least recently used first. A blob that is staged,
archiving, waiting to retry, blocked, failed, missing or drifted is the only copy (or the repair source),
so it is never touched, and neither is anything a pending storage operation still points at.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Sum

from ots.core.clock import now
from ots.storage import queue
from ots.storage.errors import StorageProviderError
from ots.storage.provider import Library, RemoteRef

from . import layout
from .models import Blob, FileState

log = logging.getLogger(__name__)
HIGH, LOW = 0.90, 0.80
STALE_PART_SECONDS = 3600  # a fetch part file this old belongs to a process that died


def cap_bytes() -> int:
    return int(float(settings.OTS_CACHE_CAP_GB) * 1024**3)


def cached_bytes() -> int:
    return int(Blob.objects.filter(cached=True).aggregate(n=Sum("size_bytes"))["n"] or 0)


def _evict_one(sha: str, pinned: set[str]) -> int:
    """Drop one blob's local copy if it is still safe to. Returns the bytes freed."""
    with transaction.atomic():
        blob = Blob.objects.select_for_update().filter(pk=sha, cached=True, state=FileState.ARCHIVED).first()
        if blob is None or blob.local_path in pinned:
            return 0
        path = Path(blob.local_path) if blob.local_path else None
        blob.cached, blob.local_path = False, None
        blob.save(update_fields=["cached", "local_path"])
        if path is not None:
            # The file goes inside the transaction. If that were ever rolled back, the row would say "cached"
            # for a file that is gone; a download treats that as a miss and corrects the row (_local_copy).
            path.unlink(missing_ok=True)
        return int(blob.size_bytes)


def evict(target_bytes: int) -> int:
    """Evict least-recently-used ARCHIVED blobs down to `target_bytes`. Returns the bytes freed."""
    over = cached_bytes() - target_bytes
    if over <= 0:
        return 0
    pinned = queue.pinned_local_paths()
    freed = 0
    candidates = (
        Blob.objects.filter(cached=True, state=FileState.ARCHIVED)
        .order_by("last_accessed_at", "sha256")
        .values_list("sha256", flat=True)
        .iterator(chunk_size=200)
    )
    for sha in candidates:
        if freed >= over:
            break
        freed += _evict_one(sha, pinned)
    return freed


def run() -> int:
    """The daily job and the check after each upload: act only when the cache is above 90% of its cap."""
    cap = cap_bytes()
    if cached_bytes() <= cap * HIGH:
        return 0
    return evict(int(cap * LOW))


def evict_on_demand(need: int = 0) -> int:
    """The disk is nearly full (F-FL-3): free at least `need` bytes if that many are evictable, and bring the
    cache down to 80% of its cap as well."""
    return evict(max(0, min(int(cap_bytes() * LOW), cached_bytes() - need)))


# --- filling the cache from SharePoint ------------------------------------------------------------


def fetch_part_path(sha: str) -> Path:
    return Path(settings.OTS_STAGING_DIR) / f"fetch-{sha}.part"


def claim_part(sha: str) -> Path | None:
    """Create the fetch part file for this content, or None if another request is already writing it."""
    path = fetch_part_path(sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return path
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime < STALE_PART_SECONDS:
                    return None
                path.unlink(missing_ok=True)  # abandoned by a process that died
            except FileNotFoundError:
                continue
    return None


def finish_fill(blob: Blob, part: Path, digest: str) -> bool:
    """Verify the fetched bytes and move them into the cache. A wrong hash leaves nothing behind."""
    try:
        if digest != blob.sha256 or part.stat().st_size != blob.size_bytes:
            log.error(
                "fetched file does not match its hash",
                extra={"event": "files.fetch_mismatch", "sha": blob.sha256},
            )
            return False
        dest = layout.blob_path(blob.sha256)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, dest)
        Blob.objects.filter(pk=blob.pk).update(cached=True, local_path=str(dest), last_accessed_at=now())
        return True
    finally:
        part.unlink(missing_ok=True)


def fill(blob: Blob, provider: object) -> bool:
    """Fetch an archived blob into the cache (the worker's way; downloads tee the same bytes while serving).

    False if SharePoint cannot be reached or the bytes do not verify; the caller tries again later.
    """
    part = claim_part(blob.sha256)
    if part is None:
        return False  # a download is filling it right now
    h = hashlib.sha256()
    try:
        ref = RemoteRef(Library.BUSINESS, blob.drive_id, blob.item_id)
        it: Iterator[bytes] = provider.open_read(ref)  # type: ignore[attr-defined]
        with part.open("wb") as fh:
            for chunk in it:
                h.update(chunk)
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
    except StorageProviderError as exc:
        part.unlink(missing_ok=True)
        log.warning("could not fetch %s: %s", blob.sha256[:12], exc, extra={"event": "files.fetch_failed"})
        return False
    return finish_fill(blob, part, h.hexdigest())
