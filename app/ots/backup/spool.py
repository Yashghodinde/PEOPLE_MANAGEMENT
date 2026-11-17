"""The encrypted WAL spool: what the archive script leaves for the worker to ship (M04 sections 1 to 3)."""

from __future__ import annotations

import datetime
import hashlib
import os
from pathlib import Path

from django.conf import settings

CHUNK = 1024 * 1024
SHIPPED = ".shipped"
MARKER_KEEP = datetime.timedelta(days=30)


def spool_dir() -> Path:
    return Path(settings.OTS_WAL_SPOOL_DIR)


def local_dir() -> Path:
    return Path(settings.OTS_WAL_LOCAL_DIR)


def entries() -> list[Path]:
    """Finished encrypted segments, in segment-name order (the order they must be shipped in)."""
    d = spool_dir()
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.name.endswith(".zst.age") and ".tmp" not in p.name)


def segment_name(path: Path) -> str:
    return path.name.removesuffix(".zst.age")


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def size_bytes() -> int:
    """Everything in the spool, not counting the zero-byte markers."""
    d = spool_dir()
    if not d.is_dir():
        return 0
    return sum(p.stat().st_size for p in d.iterdir() if p.is_file())


def mark_shipped(path: Path) -> None:
    """Leave a zero-byte marker so the archive script never queues this segment again, then drop the data."""
    marker = spool_dir() / SHIPPED / segment_name(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    fd = os.open(marker.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    path.unlink(missing_ok=True)


def prune_markers(at: datetime.datetime) -> int:
    d = spool_dir() / SHIPPED
    if not d.is_dir():
        return 0
    cutoff = (at - MARKER_KEEP).timestamp()
    old = [p for p in d.iterdir() if p.is_file() and p.stat().st_mtime < cutoff]
    for p in old:
        p.unlink(missing_ok=True)
    return len(old)
