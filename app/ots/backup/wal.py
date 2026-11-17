"""Facts about WAL segment names (PostgreSQL 16, 16 MiB segments).

A name is 24 hex digits: timeline (8), log (8), segment within the log (8). With 16 MiB segments a log holds
256 of them, and a segment's end position follows from its name alone.
"""

from __future__ import annotations

import re
from typing import NamedTuple

SEGMENT_BYTES = 16 * 1024 * 1024
PER_LOG = 0x100000000 // SEGMENT_BYTES  # 256
_SEGMENT = re.compile(r"^([0-9A-F]{8})([0-9A-F]{8})([0-9A-F]{8})$")


class Segment(NamedTuple):
    timeline: int
    log: int
    seg: int

    @property
    def number(self) -> int:
        """Position in the unbroken chain of segments of one timeline."""
        return self.log * PER_LOG + self.seg

    @property
    def end_lsn(self) -> int:
        return (self.number + 1) * SEGMENT_BYTES

    @property
    def name(self) -> str:
        return f"{self.timeline:08X}{self.log:08X}{self.seg:08X}"


def parse(name: str) -> Segment | None:
    """A segment name, or None for anything else (.history, .backup, .partial files are not segments)."""
    m = _SEGMENT.match(name)
    if m is None:
        return None
    return Segment(int(m.group(1), 16), int(m.group(2), 16), int(m.group(3), 16))


def gaps(numbers: list[int]) -> list[tuple[int, int]]:
    """Ranges (first missing, last missing) between consecutive segment numbers of one timeline."""
    out = []
    ordered = sorted(set(numbers))
    for a, b in zip(ordered, ordered[1:], strict=False):
        if b != a + 1:
            out.append((a + 1, b - 1))
    return out
