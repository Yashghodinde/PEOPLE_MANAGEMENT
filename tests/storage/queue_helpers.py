"""Helpers for queue tests: operations with real local files, and a gate that behaves under frozen time."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ots.storage import queue
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import Library


def make_op(
    tmp_path: Path, data: bytes = b"payload", *, kind: str = Op.Kind.ARCHIVE_FILE, name: str = "F-1",
    library: Library = Library.BUSINESS, remote: str | None = None, ref_id: str = "",
) -> Op:  # fmt: skip
    local = tmp_path / f"{name}.bin"
    local.write_bytes(data)
    return queue.enqueue(
        kind, library, local, remote or f"Files/2026/11/{name}", hashlib.sha256(data).hexdigest(), len(data),
        ref_type="test" if ref_id else "", ref_id=ref_id,
    )  # fmt: skip
