"""Worker jobs for files. Importing this module registers them."""

from __future__ import annotations

from ots.core.jobs import job

from . import cache, fetch, services


@job("files.staging_sweep", schedule="45 4 * * *")
def staging_sweep() -> None:
    services.staging_sweep()


@job("files.cache_evict", schedule="45 2 * * *", heavy=True)
def cache_evict() -> None:
    cache.run()


@job("files.fetch_pending", schedule="* * * * *", heavy=True)
def fetch_pending() -> None:
    fetch.run()
