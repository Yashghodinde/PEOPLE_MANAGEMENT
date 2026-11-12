"""Capacity and certificate monitoring (M05 section 5, step M05.7).

Capacity (hourly): read the site quota, keep a history row, and raise `storage.capacity`:
    >= 70% WARNING, >= 80% WARNING, >= 90% CRITICAL, >= 95% CRITICAL.
At >= 95% new files wait (an archive-only block in the gate) while WAL and backups keep going into the
space that is left, because recovery data comes first. Below 95% the archive block is lifted again; below
70% the alert closes.

Certificate (daily): the Graph app certificate expires on a known date. WARNING `storage.cert_expiring` at
30 days left, CRITICAL at 7 days or when it has expired (sign-in would then fail and the queue would stop).
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from dataclasses import dataclass

from ots.core import alerts
from ots.core.clock import now as server_now

from . import classify as c
from .errors import StorageProviderError
from .factory import get_provider
from .gate import Permission, QueueGate
from .gate import gate as default_gate
from .models import StorageCapacity
from .provider import StorageProvider

log = logging.getLogger(__name__)
LEVELS = ((95.0, "CRITICAL"), (90.0, "CRITICAL"), (80.0, "WARNING"), (70.0, "WARNING"))
ARCHIVE_BLOCK_AT = 95.0
CERT_WARN_DAYS = 30
CERT_CRITICAL_DAYS = 7


@dataclass(frozen=True)
class CapacityReading:
    used: int
    total: int
    percent: float
    severity: str | None  # None below 70%
    archive_blocked: bool


def check_capacity(
    provider: StorageProvider | None = None,
    gate: QueueGate | None = None,
    clock: Callable[[], datetime.datetime] = server_now,
) -> CapacityReading | None:
    """The `storage.capacity` job body. None if skipped (queue blocked) or the quota is unknown."""
    gate = gate or default_gate
    if gate.permission().permission != Permission.RUN:
        return None  # blocked or throttled: no extra calls (an archive-only block still answers RUN)
    provider = provider or get_provider()
    try:
        used, total = provider.usage()
    except StorageProviderError as exc:
        gate.failure(exc)
        raise
    gate.success()
    if total <= 0:
        log.warning("storage quota is unknown", extra={"event": "storage.capacity_unknown"})
        return None
    StorageCapacity.objects.create(at=clock(), used_bytes=used, total_bytes=total)
    percent = used * 100.0 / total
    severity = next((sev for limit, sev in LEVELS if percent >= limit), None)
    if severity is None:
        alerts.resolve_alert(c.ALERT_CAPACITY)
    else:
        alerts.raise_alert(
            c.ALERT_CAPACITY, severity, f"SharePoint storage is {percent:.0f}% full",
            f"{used / 1e9:.1f} GB of {total / 1e9:.1f} GB used",
        )  # fmt: skip
    blocked = percent >= ARCHIVE_BLOCK_AT
    if blocked:
        gate.block_archive_only(f"{percent:.1f}% full: new files wait, recovery data continues")
    else:
        gate.clear_archive_block()
    return CapacityReading(used, total, percent, severity, blocked)


def check_certificate(
    provider: StorageProvider | None = None, clock: Callable[[], datetime.datetime] = server_now
) -> int | None:
    """The `storage.cert_check` job body. Returns days left, or None if the provider has no certificate."""
    provider = provider or get_provider()
    expiry = getattr(provider, "certificate_not_after", None)
    if expiry is None:  # the fake provider
        return None
    not_after: datetime.datetime = expiry()
    days = (not_after - clock()).days
    if days >= CERT_WARN_DAYS:
        alerts.resolve_alert(c.ALERT_CERT)
    else:
        severity = "CRITICAL" if days < CERT_CRITICAL_DAYS else "WARNING"
        when = "has EXPIRED" if not_after <= clock() else f"expires in {days} days"
        alerts.raise_alert(
            c.ALERT_CERT, severity, f"The Microsoft Graph certificate {when}",
            f"valid until {not_after:%Y-%m-%d}; renew it as described in runbook OPS-5",
        )  # fmt: skip
    return days
