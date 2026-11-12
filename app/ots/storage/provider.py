"""The storage contract. Only `ots.storage` talks to Microsoft; everything else uses this interface.

There is no generic delete: business files are never deleted by code. `delete_dr` exists for pruning
recovery copies in the DR library and refuses anything else.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

CHUNK = 1024 * 1024  # streaming unit for downloads and hashing: 1 MiB


class Library(StrEnum):
    BUSINESS = "BUSINESS"  # SharePoint library Business-Files
    DR = "DR"  # SharePoint library System-DR


@dataclass(frozen=True)
class RemoteRef:
    library: Library
    drive_id: str
    item_id: str
    etag: str | None = None
    quick_xor_hash: str | None = None  # the verified hash at upload time; reconciliation compares against it


@dataclass(frozen=True)
class RemoteMeta:
    ref: RemoteRef
    name: str
    path: str  # path inside the library, no leading slash
    size: int
    quick_xor_hash: str | None
    deleted: bool
    last_modified: datetime.datetime


class OperationState(Protocol):
    """What `put` needs from a queue operation: its id (idempotency) and a place to keep upload progress.

    `StorageOperation` (M05.4) implements this on top of the database row; tests use an in-memory one.
    """

    id: Any
    upload_expires_at: datetime.datetime | None
    confirmed_bytes: int

    @property
    def upload_url(self) -> str:
        """The decrypted session URL; "" when no session exists."""
        ...

    def save_session(self, upload_url: str, expires_at: datetime.datetime | None) -> None: ...

    def save_progress(self, confirmed_bytes: int) -> None: ...

    def clear_session(self) -> None: ...


class StorageProvider(Protocol):
    def put(
        self, library: Library, remote_path: str, local_path: Path, size: int, sha256: str, op: OperationState
    ) -> RemoteRef:
        """Upload a local file. Resumable, idempotent on op.id; an existing name is a conflict (409)."""
        ...

    def open_read(self, ref: RemoteRef) -> Iterator[bytes]:
        """Stream the content in 1 MiB chunks."""
        ...

    def head(self, ref: RemoteRef) -> RemoteMeta: ...

    def find_by_path(self, library: Library, remote_path: str) -> RemoteMeta | None: ...

    def changes(self, library: Library, cursor: str | None) -> tuple[list[RemoteMeta], str]:
        """Items changed since `cursor` (None = everything), and the new cursor (Graph delta)."""
        ...

    def usage(self) -> tuple[int, int]:
        """(used bytes, total bytes) of the site drive quota."""
        ...

    def delete_dr(self, ref: RemoteRef) -> None:
        """Delete a recovery copy. Raises WrongLibrary unless ref.library is DR."""
        ...

    def move_to_quarantine(self, ref: RemoteRef) -> RemoteRef:
        """Move an unknown item to /_quarantine/ (a move, never a delete)."""
        ...

    def replace_dr(self, remote_path: str, local_path: Path, size: int, sha256: str) -> RemoteRef:
        """Upload to the DR library replacing any existing file of that name. The only allowed replace."""
        ...
