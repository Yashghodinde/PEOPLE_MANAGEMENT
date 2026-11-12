"""Worker jobs for attendance. Importing this module registers them."""

from __future__ import annotations

from ots.core.jobs import job

from . import sessions


@job("attendance.idle_sweep", schedule="* * * * *")
def idle_sweep() -> None:
    sessions.sweep()


def on_worker_start() -> int:
    return sessions.reset_live_on_boot()
