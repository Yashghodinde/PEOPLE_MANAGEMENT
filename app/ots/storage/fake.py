"""LocalDirProvider: an in-process imitation of SharePoint for tests and development.

It keeps file bytes in a directory and metadata in memory, honours conflict=fail (409), reports sizes and
QuickXorHash, keeps a delta log with cursors, enforces a quota, and can be told to fail:
`inject([503, None, 429])` makes the next three calls answer 503, succeed, then answer 429.
Failures raise `GraphError`, exactly what the real provider raises, so queue code is tested against the
same error shapes.
"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ots.core.clock import now

from .errors import GraphError, VerificationFailed, WrongLibrary
from .provider import CHUNK, Library, OperationState, RemoteMeta, RemoteRef
from .quickxor import QuickXorHash

QUARANTINE = "_quarantine"


@dataclass
class _Item:
    library: Library
    item_id: str
    path: str
    size: int
    sha256: str
    quick_xor: str
    etag: int
    modified: object  # datetime
    file: Path


class LocalDirProvider:
    def __init__(self, root: Path, *, quota_total: int = 1 << 40) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.quota_total = quota_total
        self._items: dict[str, _Item] = {}
        self._by_path: dict[tuple[Library, str], str] = {}
        self._by_op: dict[str, str] = {}
        self._log: list[
            tuple[Library, str]
        ] = []  # (library, item_id) per change; the cursor is an index into it
        self._failures: list[int | None] = []
        self._expire_cursors = False
        self.calls: list[str] = []

    # ---- test controls -----------------------------------------------------------------------
    def inject(self, statuses: list[int | None], *, retry_after: float | None = None) -> None:
        """Queue failures for the next calls: a status code raises GraphError, None lets the call through."""
        self._failures.extend(statuses)
        self._retry_after = retry_after

    _retry_after: float | None = None

    def expire_cursors(self) -> None:
        self._expire_cursors = True

    def set_quota(self, total: int) -> None:
        self.quota_total = total

    def plant(self, library: Library, path: str, data: bytes) -> RemoteRef:
        """Put an item on the 'remote' without put(): an orphan, or an upload whose answer was lost."""
        return self._store(library, path, data, op_id=None)

    def remote_delete(self, ref: RemoteRef) -> None:
        """Imitate someone deleting the item in the SharePoint web page."""
        item = self._items.pop(ref.item_id)
        del self._by_path[(item.library, item.path)]
        item.file.unlink(missing_ok=True)
        self._log.append((item.library, item.item_id))

    def remote_rename(self, ref: RemoteRef, new_path: str) -> None:
        item = self._items[ref.item_id]
        del self._by_path[(item.library, item.path)]
        item.path = new_path
        self._by_path[(item.library, new_path)] = item.item_id
        self._log.append((item.library, item.item_id))

    def remote_modify(self, ref: RemoteRef, data: bytes) -> None:
        item = self._items[ref.item_id]
        item.file.write_bytes(data)
        item.size, item.sha256 = len(data), hashlib.sha256(data).hexdigest()
        h = QuickXorHash()
        h.update(data)
        item.quick_xor = h.b64digest()
        item.etag += 1
        self._log.append((item.library, item.item_id))

    def paths(self, library: Library) -> set[str]:
        return {i.path for i in self._items.values() if i.library == library}

    # ---- internals ----------------------------------------------------------------------------
    def _enter(self, name: str) -> None:
        self.calls.append(name)
        if self._failures:
            status = self._failures.pop(0)
            if status is not None:
                raise GraphError(
                    status, code="injected", body="injected failure", retry_after=self._retry_after
                )

    def _drive(self, library: Library) -> str:
        return f"fake-drive-{library.value.lower()}"

    def _ref(self, item: _Item) -> RemoteRef:
        return RemoteRef(item.library, self._drive(item.library), item.item_id, str(item.etag))

    def _meta(self, item: _Item) -> RemoteMeta:
        return RemoteMeta(
            ref=self._ref(item),
            name=item.path.rsplit("/", 1)[-1],
            path=item.path,
            size=item.size,
            quick_xor_hash=item.quick_xor,
            deleted=False,
            last_modified=item.modified,  # type: ignore[arg-type]
        )

    def _used(self) -> int:
        return sum(i.size for i in self._items.values())

    def _store(self, library: Library, path: str, data: bytes, op_id: str | None) -> RemoteRef:
        path = path.strip("/")
        if (library, path) in self._by_path:
            raise GraphError(409, code="nameAlreadyExists", body=f"{path} exists")
        if self._used() + len(data) > self.quota_total:
            raise GraphError(507, code="insufficientStorage", body="quota exceeded")
        item_id = uuid.uuid4().hex
        file = self.root / f"{item_id}.bin"
        file.write_bytes(data)
        h = QuickXorHash()
        h.update(data)
        item = _Item(
            library, item_id, path, len(data), hashlib.sha256(data).hexdigest(), h.b64digest(), 1, now(), file
        )
        self._items[item_id] = item
        self._by_path[(library, path)] = item_id
        if op_id is not None:
            self._by_op[op_id] = item_id
        self._log.append((library, item_id))
        return self._ref(item)

    # ---- StorageProvider ----------------------------------------------------------------------
    def put(
        self, library: Library, remote_path: str, local_path: Path, size: int, sha256: str, op: OperationState
    ) -> RemoteRef:
        self._enter("put")
        path = remote_path.strip("/")
        done = self._by_op.get(str(op.id))
        if done is not None and done in self._items:  # idempotent: this operation already uploaded it
            return self._ref(self._items[done])
        data = local_path.read_bytes()  # the fake may hold a file in memory; the real provider streams
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise VerificationFailed(f"local file does not match its recorded size/hash: {local_path}")
        return self._store(library, path, data, str(op.id))

    def open_read(self, ref: RemoteRef) -> Iterator[bytes]:
        self._enter("open_read")
        item = self._items.get(ref.item_id)
        if item is None:
            raise GraphError(404, code="itemNotFound")
        with item.file.open("rb") as fh:
            while chunk := fh.read(CHUNK):
                yield chunk

    def head(self, ref: RemoteRef) -> RemoteMeta:
        self._enter("head")
        item = self._items.get(ref.item_id)
        if item is None:
            raise GraphError(404, code="itemNotFound")
        return self._meta(item)

    def find_by_path(self, library: Library, remote_path: str) -> RemoteMeta | None:
        self._enter("find_by_path")
        item_id = self._by_path.get((library, remote_path.strip("/")))
        return self._meta(self._items[item_id]) if item_id else None

    def changes(self, library: Library, cursor: str | None) -> tuple[list[RemoteMeta], str]:
        self._enter("changes")
        if self._expire_cursors and cursor is not None:
            self._expire_cursors = False
            raise GraphError(410, code="resyncRequired")
        start = int(cursor) if cursor else 0
        seen: dict[str, None] = {}
        for lib, item_id in self._log[start:]:
            if lib == library:
                seen[item_id] = None
        out: list[RemoteMeta] = []
        for item_id in seen:
            item = self._items.get(item_id)
            if item is not None:
                out.append(self._meta(item))
            else:
                ref = RemoteRef(library, self._drive(library), item_id)
                out.append(RemoteMeta(ref, "", "", 0, None, True, now()))
        return out, str(len(self._log))

    def usage(self) -> tuple[int, int]:
        self._enter("usage")
        return self._used(), self.quota_total

    def delete_dr(self, ref: RemoteRef) -> None:
        if ref.library != Library.DR:
            raise WrongLibrary("business files are never deleted by code")
        self._enter("delete_dr")
        self.remote_delete(ref)

    def move_to_quarantine(self, ref: RemoteRef) -> RemoteRef:
        self._enter("move_to_quarantine")
        item = self._items[ref.item_id]
        del self._by_path[(item.library, item.path)]
        item.path = f"{QUARANTINE}/{item.item_id}-{item.path.rsplit('/', 1)[-1]}"
        self._by_path[(item.library, item.path)] = item.item_id
        self._log.append((item.library, item.item_id))
        return self._ref(item)

    def replace_dr(self, remote_path: str, local_path: Path, size: int, sha256: str) -> RemoteRef:
        self._enter("replace_dr")
        path = remote_path.strip("/")
        existing = self._by_path.get((Library.DR, path))
        if existing is not None:
            self.remote_delete(self._ref(self._items[existing]))
        data = local_path.read_bytes()
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise VerificationFailed(f"local file does not match its recorded size/hash: {local_path}")
        return self._store(Library.DR, path, data, op_id=None)

    def wipe(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)
        self._items.clear()
        self._by_path.clear()
        self._by_op.clear()
        self._log.clear()
