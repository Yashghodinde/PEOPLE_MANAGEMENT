import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest


@dataclass
class MemoryOp:
    """An in-memory stand-in for a StorageOperation row."""

    id: Any = "op-1"
    upload_url: str = ""
    upload_expires_at: datetime.datetime | None = None
    confirmed_bytes: int = 0
    log: list[Any] = field(default_factory=list)

    def save_session(self, upload_url: str, expires_at: datetime.datetime | None) -> None:
        self.upload_url, self.upload_expires_at = upload_url, expires_at
        self.log.append(("session", upload_url))

    def save_progress(self, confirmed_bytes: int) -> None:
        self.confirmed_bytes = confirmed_bytes
        self.log.append(("progress", confirmed_bytes))

    def clear_session(self) -> None:
        self.upload_url, self.upload_expires_at, self.confirmed_bytes = "", None, 0
        self.log.append(("cleared", None))


@pytest.fixture
def local_file(tmp_path: Path):
    def make(data: bytes = b"hello world", name: str = "f.bin") -> tuple[Path, int, str]:
        import hashlib

        p = tmp_path / name
        p.write_bytes(data)
        return p, len(data), hashlib.sha256(data).hexdigest()

    return make
