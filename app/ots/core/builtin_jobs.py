"""Core jobs registered by M00. Importing this module registers them."""

from .dispatcher import Dispatcher
from .idempotency import purge_older_than_days
from .jobs import job, watchdog

_dispatcher = Dispatcher()


@job("live.dispatch_outbox", schedule="continuous")
def dispatch_outbox() -> None:
    _dispatcher.run_once()


@job("sync.purge_idempotency", schedule="30 4 * * *")
def purge_idempotency() -> None:
    purge_older_than_days()


@job("jobs.watchdog", schedule="* * * * *")
def jobs_watchdog() -> None:
    watchdog()
