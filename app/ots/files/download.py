"""Download and preview (M08 section 5).

Memory: under ASGI, Django turns a *synchronous* streaming body into one big list before sending it, so a
plain FileResponse would hold a whole 200 MB file in RAM. These responses stream from an async generator
that reads one chunk at a time in the request's own worker thread, so memory stays flat.
"""

from __future__ import annotations

import datetime
import hashlib
import itertools
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

from asgiref.sync import sync_to_async
from django.db import transaction
from django.http import HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import render

from ots.audit import api as audit
from ots.core import permissions
from ots.core.clock import now
from ots.core.errors import NotFound, OtsError, StorageUnavailable
from ots.storage.errors import GraphError, StorageProviderError
from ots.storage.factory import get_provider
from ots.storage.gate import SCOPE_ALL, QueueGate
from ots.storage.provider import Library, RemoteRef

from . import cache, handover
from .models import Blob, FileAccessLog, FileFetchRequest, FileState, FileVersion, TaskFile

CHUNK = 1024 * 1024
TOUCH_EVERY = 3600  # last_accessed_at is written at most once an hour per blob
PREVIEW_CSP = "default-src 'none'; img-src 'self'; object-src 'self'; sandbox"
_END = object()


class UnsupportedPreview(OtsError):
    status = 415
    code = "unsupported_preview"


# --- streaming ------------------------------------------------------------------------------------


async def aiter_blocking(it: Iterator[bytes]) -> AsyncIterator[bytes]:
    """Hand a blocking iterator to ASGI one chunk at a time, each read in the request's worker thread."""
    step = sync_to_async(lambda: next(it, _END), thread_sensitive=True)
    close = sync_to_async(it.close, thread_sensitive=True)  # type: ignore[attr-defined]
    try:
        while (chunk := await step()) is not _END:
            yield chunk  # type: ignore[misc]
    finally:
        await close()


def _read_file(path: Path) -> Iterator[bytes]:
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            yield chunk


def _tee(blob: Blob, first: bytes, rest: Iterator[bytes]) -> Iterator[bytes]:
    """Pass SharePoint's bytes to the client while keeping a copy, cached only if it verifies."""
    part = cache.claim_part(blob.sha256)  # None when another request is already filling the cache
    h = hashlib.sha256()
    fh = part.open("wb") if part is not None else None
    complete = False
    try:
        for chunk in itertools.chain([first], rest):
            if fh is not None:
                h.update(chunk)
                fh.write(chunk)
            yield chunk
        complete = True
    finally:
        if fh is not None:
            fh.close()
        if part is not None:
            if complete:
                cache.finish_fill(blob, part, h.hexdigest())
            else:
                part.unlink(missing_ok=True)  # the client left early: a partial copy is never cached
        close = getattr(rest, "close", None)
        if close is not None:
            close()


# --- where the bytes come from --------------------------------------------------------------------


def _local_copy(blob: Blob) -> Path | None:
    if not blob.cached or not blob.local_path:
        return None
    path = Path(blob.local_path)
    try:
        if path.stat().st_size == blob.size_bytes:
            return path
    except OSError:
        pass
    Blob.objects.filter(pk=blob.pk).update(cached=False, local_path=None)  # the cache lost it: say so
    return None


def _open_remote(blob: Blob) -> tuple[bytes, Iterator[bytes]] | None:
    """Start reading from SharePoint. None means it cannot be reached right now (the offline page)."""
    block = QueueGate().block()
    if block is not None and block.scope == SCOPE_ALL:
        return None
    ref = RemoteRef(Library.BUSINESS, blob.drive_id, blob.item_id)
    it = get_provider().open_read(ref)
    try:
        first = next(it)  # the request goes out here, so a failure is seen before any header is sent
    except StopIteration:
        return b"", iter(())
    except GraphError as exc:
        if exc.status == 404:
            raise StorageUnavailable(
                "This file is missing from SharePoint. The system is restoring it from its copy."
            ) from exc
        return None
    except StorageProviderError:
        return None
    return first, it


# --- the views' logic -----------------------------------------------------------------------------


def _touch(blob: Blob) -> None:
    at = now()
    Blob.objects.filter(pk=blob.pk, last_accessed_at__lt=at - datetime.timedelta(seconds=TOUCH_EVERY)).update(
        last_accessed_at=at
    )


def _log(request: HttpRequest, version: FileVersion, action: str, source: str) -> None:
    with transaction.atomic():
        FileAccessLog.objects.create(version=version, user=request.user, action=action, source=source)
        audit.record(
            f"file.{action.lower()}",
            request.user,
            version,
            {"file_code": version.file_code, "source": source},
            request,
        )
        handover.record_open(request.user, version, request)  # a recipient's first open, once


def _offline(request: HttpRequest, version: FileVersion) -> HttpResponse:
    """F-FL-6: SharePoint is out of reach and the file is not in the cache. Remember who wants it; the worker
    fetches it as soon as it can and sends them a notification."""
    with transaction.atomic():
        fetch, made = FileFetchRequest.objects.get_or_create(version=version, user=request.user)
        if not made and fetch.state != FileFetchRequest.State.PENDING:
            fetch.state, fetch.created_at, fetch.done_at = FileFetchRequest.State.PENDING, now(), None
            fetch.save(update_fields=["state", "created_at", "done_at"])
            made = True
        if made:
            audit.record(
                "file.fetch_requested", request.user, version, {"file_code": version.file_code}, request
            )
    link = TaskFile.objects.filter(version=version).select_related("task").order_by("-linked_at").first()
    response = render(
        request,
        "files/offline.html",
        {"page_title": "File not available yet", "version": version, "task": link.task if link else None},
        status=503,
    )
    response["Retry-After"] = "300"
    return response


def serve(request: HttpRequest, version_id: Any, action: str) -> HttpResponse:
    user = request.user
    from .selectors import versions_visible_to

    version = versions_visible_to(user).select_related("blob", "file").filter(pk=version_id).first()
    if version is None:
        raise NotFound("File not found.")
    permissions.require(user, "file.download", version)
    preview = action == FileAccessLog.Action.PREVIEW
    if preview and version.preview_type is None:
        raise UnsupportedPreview("This kind of file cannot be previewed. Download it instead.")
    blob = version.blob

    path = _local_copy(blob)
    if path is not None:
        body: Iterator[bytes] = _read_file(path)
        source = FileAccessLog.Source.CACHE
        _touch(blob)
    elif blob.state not in (FileState.ARCHIVED, FileState.DRIFTED):
        raise StorageUnavailable("The stored copy of this file is not available. Tell the Super Admin.")
    else:
        opened = _open_remote(blob)
        if opened is None:
            return _offline(request, version)
        body, source = _tee(blob, *opened), FileAccessLog.Source.ONEDRIVE
    _log(request, version, action, source)

    response = StreamingHttpResponse(aiter_blocking(body))
    response["Content-Length"] = str(blob.size_bytes)
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    if preview:
        response["Content-Type"] = version.preview_type
        response["Content-Disposition"] = f'inline; filename="{version.file_code}"'
        response["Content-Security-Policy"] = PREVIEW_CSP
    else:
        response["Content-Type"] = "application/octet-stream"
        response["Content-Disposition"] = f'attachment; filename="{version.file_code}"'
    return response
