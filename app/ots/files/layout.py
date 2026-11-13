"""Where file bytes live, and whether there is room for more (F-FL-3).

staging/{upload_id}.part         an upload in progress
blobs/{sha[0:2]}/{sha[2:4]}/{sha}  the content-addressed cache
"""

from __future__ import annotations

import shutil
from pathlib import Path

from django.conf import settings

from ots.core.errors import QuotaBlocked


def staging_path(upload_id: object) -> Path:
    return Path(settings.OTS_STAGING_DIR) / f"{upload_id}.part"


def blob_path(sha256: str) -> Path:
    return Path(settings.OTS_BLOB_DIR) / sha256[0:2] / sha256[2:4] / sha256


def _usage() -> tuple[int, int]:
    """(free bytes, total bytes) of the disk that holds the staging area."""
    root = Path(settings.OTS_STAGING_DIR)
    root.mkdir(parents=True, exist_ok=True)
    u = shutil.disk_usage(root)
    return u.free, u.total


def admit(size: int, *, evict: object = None) -> None:
    """Refuse an upload that would leave less than the minimum free space (default 10% of the disk).

    It reserves `size * 1.1`. Before refusing it asks the cache to evict ARCHIVED copies and looks again.
    `evict` is that hook (told how many bytes are missing); it defaults to `cache.evict_on_demand`.
    """
    need = int(size * 1.1)
    floor_pct = float(settings.OTS_DISK_FREE_MIN_PERCENT)

    def shortfall() -> int:
        free, total = _usage()
        return max(0, int(total * floor_pct / 100) + need - free)

    def enough() -> bool:
        return shortfall() == 0

    if enough():
        return
    if evict is None:
        from . import cache

        evict = cache.evict_on_demand
    evict(shortfall())  # type: ignore[operator]
    if not enough():
        raise QuotaBlocked("Server storage full. Ask the Super Admin to free some space.")
