"""Worker jobs for files. Importing this module registers them."""

from __future__ import annotations

from ots.core.jobs import job

from . import services


@job("files.staging_sweep", schedule="45 4 * * *")
def staging_sweep() -> None:
    services.staging_sweep()
