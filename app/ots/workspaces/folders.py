"""Folders inside a File ID (IMP-14.6). Anyone who may contribute can create, rename and move folders and
place documents; every one is audited. A folder of another File ID is a 404, never a leak."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction
from django.http import HttpRequest

from ots.audit import api as audit
from ots.core.clock import now
from ots.core.errors import Conflict, NotFound, ValidationFailed
from ots.core.permissions import require
from ots.files import folders as base
from ots.files.models import FileStatus, Folder, FolderContext

from .models import FileIdDocument, FileIdWorkspace, WorkspaceStatus


def _writable(user: Any, ws: FileIdWorkspace) -> None:
    require(user, "fileid.contribute", ws)
    if ws.status == WorkspaceStatus.ARCHIVED:
        raise ValidationFailed("This File ID is archived. Restore it to change anything.")


def folder_of(ws: FileIdWorkspace, folder_id: Any) -> Folder:
    f = Folder.objects.filter(
        pk=folder_id, context=FolderContext.WORKSPACE, workspace_id=ws.pk, status=FileStatus.ACTIVE
    ).first()
    if f is None:
        raise NotFound("Folder not found.")
    return f


def create(
    user: Any, ws: FileIdWorkspace, name: str, parent_id: Any = None, request: HttpRequest | None = None
) -> Folder:
    _writable(user, ws)
    name = base.clean_name(name)
    parent = folder_of(ws, parent_id) if parent_id else None
    if parent is not None and base.depth(parent) >= base.MAX_DEPTH:
        raise ValidationFailed(
            f"Folders can go {base.MAX_DEPTH} levels deep, no further.", fields={"name": ["Too deep."]}
        )
    try:
        with transaction.atomic():
            f = Folder.objects.create(
                context=FolderContext.WORKSPACE, workspace_id=ws.pk, parent=parent, name=name,
                name_key=name.lower(), created_by=user,
            )  # fmt: skip
            audit.record("fileid.folder_created", user, ws, {"name": name, "folder": str(f.pk)}, request)
    except IntegrityError:
        raise Conflict("A folder with that name already exists here.") from None
    return f


def ensure_path(user: Any, ws: FileIdWorkspace, parts: list[str], parent_id: Any = None) -> Folder | None:
    """Make (or reuse) each folder of `parts` below `parent_id`, for a folder upload. Returns the last."""
    cur, last = parent_id, None
    for raw in parts:
        name = base.clean_name(raw)
        existing = Folder.objects.filter(
            context=FolderContext.WORKSPACE, workspace_id=ws.pk, parent_id=cur, name_key=name.lower(),
            status=FileStatus.ACTIVE,
        ).first()  # fmt: skip
        last = existing or create(user, ws, name, cur)
        cur = last.pk
    return last


@transaction.atomic
def rename(
    user: Any, ws: FileIdWorkspace, folder_id: Any, name: str, request: HttpRequest | None = None
) -> Folder:
    _writable(user, ws)
    f = folder_of(ws, folder_id)
    name = base.clean_name(name)
    old = f.name
    f.name, f.name_key, f.updated_at = name, name.lower(), now()
    try:
        with transaction.atomic():
            f.save(update_fields=["name", "name_key", "updated_at"])
    except IntegrityError:
        raise Conflict("A folder with that name already exists here.") from None
    audit.record("fileid.folder_renamed", user, ws, {"from": old, "to": name}, request)
    return f


@transaction.atomic
def move_folder(
    user: Any, ws: FileIdWorkspace, folder_id: Any, to_id: Any, request: HttpRequest | None = None
) -> Folder:
    _writable(user, ws)
    f = folder_of(ws, folder_id)
    parent = folder_of(ws, to_id) if to_id else None
    if parent is not None:
        if f.pk in {a.pk for a in base.ancestors(parent)}:
            raise ValidationFailed("A folder cannot go inside itself.", fields={"to": ["Invalid."]})
        if base.depth(parent) + base.height(f) > base.MAX_DEPTH:
            raise ValidationFailed(
                f"Folders can go {base.MAX_DEPTH} levels deep, no further.", fields={"to": ["Too deep."]}
            )
    old = str(f.parent_id or "")
    f.parent, f.updated_at = parent, now()
    try:
        with transaction.atomic():
            f.save(update_fields=["parent", "updated_at"])
    except IntegrityError:
        raise Conflict("A folder with that name already exists there.") from None
    audit.record("fileid.folder_moved", user, ws, {"from": old, "to": str(to_id or "")}, request)
    return f


@transaction.atomic
def place_document(
    user: Any, ws: FileIdWorkspace, file_id: Any, folder_id: Any, request: HttpRequest | None = None
) -> FileIdDocument:
    """Put a document of this File ID in one of its folders (empty = the top)."""
    _writable(user, ws)
    doc = (
        FileIdDocument.objects.select_for_update()
        .filter(workspace=ws, file_id=file_id, removed_at__isnull=True)
        .first()
    )
    if doc is None:
        raise NotFound("That document is not in this File ID.")
    target = folder_of(ws, folder_id) if folder_id else None
    if doc.folder_id == (target.pk if target else None):
        return doc
    old = str(doc.folder_id or "")
    doc.folder = target
    doc.save(update_fields=["folder"])
    audit.record("fileid.document_moved", user, ws, {"file": str(file_id), "from": old}, request)
    return doc
