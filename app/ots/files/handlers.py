"""What the storage queue tells `files` (M08 section 3, and the reconcile hooks of M05).

`on_archived` runs inside the queue's finishing transaction: the blob and every version of it become ARCHIVED
together with the operation becoming DONE, so "Off-site" can never be shown for something SharePoint has not
confirmed. `on_repaired` is the same for a re-upload after someone deleted the file in SharePoint.
"""

from __future__ import annotations

from ots.audit import api as audit
from ots.core import events
from ots.core.clock import now
from ots.storage import manifest
from ots.storage.handlers import ReconcileHooks, register_completion_handler, register_reconcile_hooks
from ots.storage.models import StorageOperation as Op
from ots.storage.provider import RemoteMeta, RemoteRef

from .models import Blob, FileState, FileVersion

_PROBLEM = (FileState.MISSING, FileState.DRIFTED)


def _versions_of(sha: str) -> list[FileVersion]:
    return list(FileVersion.objects.select_for_update().filter(blob_id=sha).order_by("uploaded_at"))


def _archived(op: Op, ref: RemoteRef, *, repaired: bool) -> None:
    sha = op.sha256
    blob = Blob.objects.select_for_update().filter(pk=sha).first()
    if blob is None:  # the operation outlived its blob: nothing here to mark
        return
    at = now()
    blob.drive_id, blob.item_id, blob.etag = ref.drive_id, ref.item_id, ref.etag or ""
    blob.ms_quick_xor_hash = ref.quick_xor_hash or blob.ms_quick_xor_hash
    blob.archived_at, blob.state = at, FileState.ARCHIVED
    blob.save()
    for v in _versions_of(sha):
        before = v.state
        if before != FileState.ARCHIVED:
            v.state = FileState.ARCHIVED
            v.save(update_fields=["state"])
        if before == FileState.ARCHIVED and not repaired:
            continue  # a second finish of the same operation changes nothing
        manifest.append(v.file_code, sha, blob.size_bytes, ref.drive_id, ref.item_id, at)
        events.publish(
            "file.repaired" if repaired else "file.archived",
            ("file_version", str(v.pk)),
            {
                "file_id": str(v.file_id),
                "version_id": str(v.pk),
                "file_code": v.file_code,
                "uploaded_by": str(v.uploaded_by_id),
            },
        )
        if repaired:
            audit.record("file.repaired", None, v, {"file_code": v.file_code, "item_id": ref.item_id})


def on_archived(op: Op, ref: RemoteRef) -> None:
    _archived(op, ref, repaired=False)


def on_repaired(op: Op, ref: RemoteRef) -> None:
    _archived(op, ref, repaired=True)


def _problem(op: Op, state: str, why: str) -> None:
    blob = Blob.objects.select_for_update().filter(pk=op.sha256).first()
    if blob is None or blob.state == state:
        return
    blob.state = state
    blob.save(update_fields=["state"])
    for v in _versions_of(op.sha256):
        if v.state != state:
            v.state = state
            v.save(update_fields=["state"])
            audit.record(f"file.{why}", None, v, {"file_code": v.file_code})
            events.publish(f"file.{why}", ("file_version", str(v.pk)), {"version_id": str(v.pk)})


def on_missing(op: Op, meta: RemoteMeta | None) -> None:
    _problem(op, FileState.MISSING, "missing")


def on_drifted(op: Op, meta: RemoteMeta) -> None:
    _problem(op, FileState.DRIFTED, "drifted")


def register() -> None:
    """Idempotent: tests that clear the registry call this again."""
    register_completion_handler(Op.Kind.ARCHIVE_FILE, on_archived)
    register_completion_handler(Op.Kind.REPAIR_FILE, on_repaired)
    hooks = ReconcileHooks(on_missing=on_missing, on_drifted=on_drifted)
    register_reconcile_hooks(Op.Kind.ARCHIVE_FILE, hooks)
    register_reconcile_hooks(Op.Kind.REPAIR_FILE, hooks)
