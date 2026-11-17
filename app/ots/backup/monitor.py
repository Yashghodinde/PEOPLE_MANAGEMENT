"""Is the recovery point fresh? (M04 section 3). Runs every minute.

rpo age = now - the archive time of the newest WAL segment that is confirmed in SharePoint.
Under 10 minutes the alert closes; 10 to 15 is a WARNING; over 15 is CRITICAL. The spool and PostgreSQL's own
archiver are watched too, because they are what goes wrong first.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from django.conf import settings
from django.db import connection
from django.db.models import Max, Min

from ots.core import alerts
from ots.core.clock import now
from ots.storage.models import StorageOperation as Op

from . import spool
from .models import BackupObject

WARN_AFTER = datetime.timedelta(minutes=10)
CRITICAL_AFTER = datetime.timedelta(minutes=15)


@dataclass(frozen=True)
class Status:
    rpo_age: datetime.timedelta | None  # None: nothing has been archived yet, so nothing is at risk
    last_shipped: datetime.datetime | None
    spool_bytes: int


def _waiting_since() -> datetime.datetime | None:
    """The oldest thing that is archived locally but not yet confirmed off-site."""
    stamps = []
    pending = (
        Op.objects.filter(kind=Op.Kind.SHIP_WAL).exclude(state=Op.State.DONE).aggregate(m=Min("created_at"))
    )
    if pending["m"] is not None:
        stamps.append(pending["m"])
    for path in spool.entries()[:1]:
        stamps.append(datetime.datetime.fromtimestamp(path.stat().st_mtime, tz=datetime.UTC))
    return min(stamps) if stamps else None


def status(at: datetime.datetime | None = None) -> Status:
    at = at or now()
    last = BackupObject.objects.filter(
        type=BackupObject.Type.WAL, confirmed_at__isnull=False, pruned_at__isnull=True
    ).aggregate(m=Max("wal_end_time"))["m"]
    if last is not None:
        age: datetime.timedelta | None = at - last
    else:  # nothing confirmed yet: the clock starts when the first segment started waiting to be shipped
        waiting = _waiting_since()
        age = at - waiting if waiting is not None else None
    return Status(rpo_age=age, last_shipped=last, spool_bytes=spool.size_bytes())


def check(at: datetime.datetime | None = None) -> Status:
    at = at or now()
    s = status(at)
    _rpo(s)
    _spool(s)
    _archiver()
    return s


def _rpo(s: Status) -> None:
    if s.rpo_age is None or s.rpo_age < WARN_AFTER:
        alerts.resolve_alert("backup.rpo")
        return
    minutes = int(s.rpo_age.total_seconds() // 60)
    if s.rpo_age >= CRITICAL_AFTER:
        alerts.raise_alert(
            "backup.rpo", "CRITICAL", f"Recovery point is {minutes} minutes old",
            "More than 15 minutes of changes are not yet copied off-site.",
        )  # fmt: skip
    else:
        alerts.raise_alert(
            "backup.rpo", "WARNING", f"Recovery point is {minutes} minutes old",
            "Changes are taking longer than usual to reach SharePoint.",
        )  # fmt: skip


def _spool(s: Status) -> None:
    warn = int(settings.OTS_WAL_SPOOL_WARN_BYTES)
    crit = int(settings.OTS_WAL_SPOOL_CRITICAL_BYTES)
    gb = s.spool_bytes / 1024**3
    if s.spool_bytes >= crit:
        alerts.raise_alert(
            "backup.spool_size", "CRITICAL", f"Backup spool is {gb:.1f} GB", "The disk could fill."
        )
    elif s.spool_bytes >= warn:
        alerts.raise_alert(
            "backup.spool_size", "WARNING", f"Backup spool is {gb:.1f} GB",
            "Backups are not leaving this server as fast as they are made.",
        )  # fmt: skip
    else:
        alerts.resolve_alert("backup.spool_size")


def archiver_failing() -> bool:
    """PostgreSQL's own view: its last attempt to archive a segment failed, and no success since."""
    with connection.cursor() as cur:
        cur.execute("SELECT last_failed_time, last_archived_time FROM pg_stat_archiver")
        row = cur.fetchone()
    if not row or row[0] is None:
        return False
    return row[1] is None or row[0] > row[1]


def _archiver() -> None:
    if archiver_failing():
        alerts.raise_alert(
            "backup.archiver_failing", "CRITICAL", "PostgreSQL cannot archive its log",
            "The archive script is failing; changes are piling up inside the database.",
        )  # fmt: skip
    else:
        alerts.resolve_alert("backup.archiver_failing")
