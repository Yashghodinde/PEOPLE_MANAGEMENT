"""The local cache of file bytes (M08 section 4).

M08.2 only needs the hook the disk check calls. M08.4 builds the real eviction (ARCHIVED blobs only,
least recently used first, down to 80% of the cap).
"""

from __future__ import annotations


def evict_on_demand() -> int:
    """Free cache space now. Returns the bytes freed. Nothing is evictable until M08.4 archives blobs."""
    return 0
