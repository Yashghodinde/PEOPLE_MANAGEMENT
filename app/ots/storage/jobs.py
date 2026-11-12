"""Worker jobs for storage. Importing this module registers them."""

from __future__ import annotations

from ots.core.jobs import CONTINUOUS, job

from . import queue, reconcile


@job("storage.process_queue", schedule=CONTINUOUS)
def process_queue() -> None:
    queue.process_queue()


@job("storage.delta_reconcile", schedule="0 2 * * *")
def delta_reconcile() -> None:
    reconcile.run()


def on_worker_start() -> int:
    """Called once when the worker starts: operations a dead worker left RUNNING go back to RETRY_WAIT."""
    return queue.reset_stale_running()
