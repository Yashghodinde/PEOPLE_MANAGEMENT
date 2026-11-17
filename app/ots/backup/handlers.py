"""Tell the storage queue what to do when a backup upload is verified (M04 section 2)."""

from __future__ import annotations

from ots.storage.handlers import register_completion_handler
from ots.storage.models import StorageOperation as Op

from . import shipper


def register() -> None:
    register_completion_handler(Op.Kind.SHIP_WAL, shipper.on_shipped)
    register_completion_handler(Op.Kind.UPLOAD_BACKUP_OBJECT, shipper.on_backup_uploaded)
