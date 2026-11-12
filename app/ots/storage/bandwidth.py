"""Upload bandwidth limiter: half of the office uplink during working hours, unlimited otherwise.

A leaky bucket: each send reserves `bytes / rate` seconds of the line; the next send waits until the
reservation ends. Between 09:00 and 19:00 IST the rate is `OTS_UPLINK_MBPS * 0.5`; outside those hours
there is no wait.
"""

from __future__ import annotations

import datetime
import time
from collections.abc import Callable

from ots.core.clock import IST
from ots.core.clock import now as server_now

DAY_START = 9  # hour, IST, inclusive
DAY_END = 19  # hour, IST, exclusive
DAY_FRACTION = 0.5


class BandwidthLimiter:
    def __init__(
        self,
        uplink_mbps: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], datetime.datetime] = server_now,
    ) -> None:
        self.uplink_mbps = uplink_mbps
        self._clock, self._sleep, self._wall = clock, sleep, wall
        self._free_at = 0.0

    def rate_bytes_per_second(self) -> float | None:
        """The limit right now, or None when unlimited."""
        hour = self._wall().astimezone(IST).hour
        if self.uplink_mbps <= 0 or not (DAY_START <= hour < DAY_END):
            return None
        return self.uplink_mbps * DAY_FRACTION * 1_000_000 / 8

    def acquire(self, nbytes: int) -> float:
        """Wait until `nbytes` may be sent. Returns how long it waited (seconds)."""
        rate = self.rate_bytes_per_second()
        if rate is None:
            return 0.0
        now = self._clock()
        wait = max(0.0, self._free_at - now)
        if wait:
            self._sleep(wait)
        self._free_at = max(now, self._free_at) + nbytes / rate
        return wait
