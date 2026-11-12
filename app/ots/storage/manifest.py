"""The monthly manifest: a plain list of every archived file, so the archive explains itself without the DB.

`append()` adds one JSON line per archived file to a local `manifest-YYYY-MM.jsonl` (the month is the
archive date in Asia/Kolkata). A daily job uploads each changed manifest to the DR library as
`Manifests/YYYY-MM.jsonl`, replacing the previous copy of that month. That is the ONLY file the system
ever replaces (an allowlist enforces it: DR library, that exact name pattern); every other file is
written once and never overwritten or deleted.

The upload is a snapshot copy, so a line appended while the upload is queued cannot invalidate its
recorded hash; the next daily run uploads the newer snapshot.
"""

from __future__ import annotations

import datetime
import fcntl
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from django.conf import settings

from ots.core.clock import IST
from ots.core.clock import now as server_now

from .models import StorageOperation as Op
from .provider import Library
from .queue import enqueue

log = logging.getLogger(__name__)
KEYS = ("file_code", "sha256", "size", "drive_id", "item_id", "archived_at")


def manifest_dir() -> Path:
    path = Path(settings.OTS_MANIFEST_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def month_of(archived_at: datetime.datetime) -> str:
    return archived_at.astimezone(IST).strftime("%Y-%m")


def manifest_path(month: str) -> Path:
    return manifest_dir() / f"manifest-{month}.jsonl"


def append(
    file_code: str, sha256: str, size: int, drive_id: str, item_id: str, archived_at: datetime.datetime
) -> bool:
    """Add one line. False if this exact file (code + item) is already listed: a retry is harmless."""
    entry: dict[str, Any] = {
        "file_code": file_code,
        "sha256": sha256,
        "size": size,
        "drive_id": drive_id,
        "item_id": item_id,
        "archived_at": archived_at.astimezone(IST).isoformat(timespec="seconds"),
    }
    line = json.dumps(entry, separators=(",", ":"), ensure_ascii=False)
    path = manifest_path(month_of(archived_at))
    with path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # two workers must not interleave lines
        try:
            fh.seek(0)
            marker = f'"file_code":{json.dumps(file_code)},'
            if any(marker in existing and f'"item_id":{json.dumps(item_id)}' in existing for existing in fh):
                return False
            fh.seek(0, 2)
            fh.write(line + "\n")
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
    return True


def _digest(path: Path) -> tuple[str, int]:
    sha, size = hashlib.sha256(), 0
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            sha.update(chunk)
            size += len(chunk)
    return sha.hexdigest(), size


def upload_changed(clock=server_now) -> int:  # type: ignore[no-untyped-def]
    """The daily `storage.manifest_upload` job body: queue an upload for every manifest that has changed."""
    queued = 0
    snapshots = manifest_dir() / "upload"
    snapshots.mkdir(exist_ok=True)
    for path in sorted(manifest_dir().glob("manifest-????-??.jsonl")):
        month = path.stem.removeprefix("manifest-")
        sha, size = _digest(path)
        if size == 0:
            continue
        done = (
            Op.objects.filter(
                kind=Op.Kind.UPLOAD_BACKUP_OBJECT, ref_type="manifest", ref_id=month, state=Op.State.DONE
            )
            .order_by("-created_at")
            .first()
        )
        if done is not None and done.sha256 == sha:
            continue  # SharePoint already has this exact version
        if Op.objects.filter(
            kind=Op.Kind.UPLOAD_BACKUP_OBJECT, ref_type="manifest", ref_id=month, state__in=Op.ACTIVE
        ).exists():
            continue  # an earlier snapshot is still on its way; the next daily run picks up newer lines
        snapshot = snapshots / f"manifest-{month}.{sha[:12]}.jsonl"
        snapshot.write_bytes(path.read_bytes())
        op = enqueue(
            Op.Kind.UPLOAD_BACKUP_OBJECT, Library.DR, snapshot, f"Manifests/{month}.jsonl", sha, size,
            ref_type="manifest", ref_id=month,
        )  # fmt: skip
        Op.objects.filter(pk=op.pk).update(replace_existing=True)
        queued += 1
    _prune_snapshots(snapshots)
    return queued


def _prune_snapshots(snapshots: Path) -> None:
    """Delete snapshot copies that no unfinished upload needs any more."""
    keep = {Path(p) for p in Op.objects.exclude(state=Op.State.DONE).values_list("local_path", flat=True)}
    for f in snapshots.glob("manifest-*.jsonl"):
        if f not in keep:
            f.unlink(missing_ok=True)
