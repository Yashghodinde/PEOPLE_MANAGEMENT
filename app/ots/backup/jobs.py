"""Worker jobs for backup. Importing this module registers them."""

from __future__ import annotations

from ots.core.clock import now
from ots.core.jobs import CONTINUOUS, job

from . import monitor, shipper, spool


@job("backup.enqueue_wal", schedule=CONTINUOUS)
def enqueue_wal() -> None:
    shipper.enqueue_wal()


@job("backup.rpo_monitor", schedule="* * * * *")
def rpo_monitor() -> None:
    monitor.check()
    spool.prune_markers(now())
