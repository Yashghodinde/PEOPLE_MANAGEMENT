"""Worker jobs for attendance. Importing this module registers them."""

from __future__ import annotations

import datetime

from ots.core.clock import today_local
from ots.core.jobs import job

from . import rollup, sessions


@job("attendance.idle_sweep", schedule="* * * * *")
def idle_sweep() -> None:
    sessions.sweep()


@job("attendance.rollup", schedule="45 22 * * *")
def daily_rollup() -> None:
    today = today_local()
    rollup.rollup(today - datetime.timedelta(days=1))  # catches a day the job missed
    rollup.rollup(today)


def on_worker_start() -> int:
    return sessions.reset_live_on_boot()
