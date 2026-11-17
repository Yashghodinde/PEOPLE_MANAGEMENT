"""Worker jobs for backup. Importing this module registers them."""

from __future__ import annotations

from ots.core.clock import now
from ots.core.jobs import CONTINUOUS, job

from . import base, monitor, shipper, spool


@job("backup.enqueue_wal", schedule=CONTINUOUS)
def enqueue_wal() -> None:
    shipper.enqueue_wal()


@job("backup.rpo_monitor", schedule="* * * * *")
def rpo_monitor() -> None:
    monitor.check()
    spool.prune_markers(now())


@job("backup.base", schedule="0 1 * * 0", heavy=True)
def base_backup() -> None:
    """Sunday 01:00: a full copy of the database to start restores from."""
    base.run()


@job("backup.base_retry", schedule="0 2 * * 0", heavy=True)
def base_backup_retry() -> None:
    """Sunday 02:00: once more, only if the 01:00 one did not work."""
    base.retry_if_needed()
