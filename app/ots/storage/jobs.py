"""Worker jobs for storage. Importing this module registers them."""

from __future__ import annotations

from ots.core.jobs import CONTINUOUS, job

from . import capacity, inventory, manifest, queue, reconcile, sweep


@job("storage.process_queue", schedule=CONTINUOUS)
def process_queue() -> None:
    queue.process_queue()


@job("storage.delta_reconcile", schedule="0 2 * * *")
def delta_reconcile() -> None:
    reconcile.run()


@job("storage.orphan_sweep", schedule="30 2 * * *")
def orphan_sweep() -> None:
    sweep.run()


@job("storage.capacity", schedule="0 * * * *")
def capacity_check() -> None:
    capacity.check_capacity()


@job("storage.cert_check", schedule="15 3 * * *")
def cert_check() -> None:
    capacity.check_certificate()


@job("storage.full_inventory", schedule="0 3 1 * *", heavy=True)
def full_inventory() -> None:
    inventory.run()


@job("storage.manifest_upload", schedule="30 3 * * *")
def manifest_upload() -> None:
    manifest.upload_changed()


def on_worker_start() -> int:
    """Called once when the worker starts: operations a dead worker left RUNNING go back to RETRY_WAIT."""
    return queue.reset_stale_running()
