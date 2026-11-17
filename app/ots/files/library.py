"""Library and attach-existing (M08 section 7). Writes only; reads live in selectors."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction
from django.http import HttpRequest

from ots.audit import api as audit
from ots.core import events
from ots.core.errors import Conflict, NotFound, ValidationFailed
from ots.core.permissions import require
from ots.tasks.models import Task, TaskEvent

from . import selectors
from .models import File, FileRelation, FileVisibility, LibraryFolder, TaskFile

NAME_MAX = 120


def _name(raw: str) -> str:
    name = " ".join((raw or "").split())
    if not name or len(name) > NAME_MAX or "/" in name or "\\" in name:
        raise ValidationFailed(
            "Give the folder a name of up to 120 characters, without slashes.", fields={"name": ["Invalid."]}
        )
    return name


def create_folder(
    user: Any, name: str, parent_id: Any = None, request: HttpRequest | None = None
) -> LibraryFolder:
    require(user, "library.manage", None)
    name = _name(name)
    parent = None
    if parent_id:
        parent = LibraryFolder.objects.filter(pk=parent_id).first()
        if parent is None:
            raise NotFound("No such folder.")
    try:
        with transaction.atomic():
            folder = LibraryFolder.objects.create(parent=parent, name=name)
            audit.record("library.folder_created", user, folder, {"name": name}, request)
            events.publish("library.changed", ("library_folder", str(folder.pk)), {"name": name})
    except IntegrityError:
        raise Conflict("A folder with that name already exists here.") from None
    return folder


def rename_folder(user: Any, folder_id: Any, name: str, request: HttpRequest | None = None) -> LibraryFolder:
    require(user, "library.manage", None)
    name = _name(name)
    folder = LibraryFolder.objects.filter(pk=folder_id).first()
    if folder is None:
        raise NotFound("No such folder.")
    try:
        with transaction.atomic():
            old, folder.name = folder.name, name
            folder.save(update_fields=["name"])
            audit.record("library.folder_renamed", user, folder, {"from": old, "to": name}, request)
            events.publish("library.changed", ("library_folder", str(folder.pk)), {"name": name})
    except IntegrityError:
        raise Conflict("A folder with that name already exists here.") from None
    return folder


def move_file(user: Any, file_id: Any, folder_id: Any, request: HttpRequest | None = None) -> File:
    """Put a library file in a folder (or back at the top with folder_id=None)."""
    require(user, "library.manage", None)
    folder = None
    if folder_id:
        folder = LibraryFolder.objects.filter(pk=folder_id).first()
        if folder is None:
            raise NotFound("No such folder.")
    with transaction.atomic():
        f = File.objects.select_for_update().filter(pk=file_id, visibility=FileVisibility.LIBRARY).first()
        if f is None:
            raise NotFound("No such library file.")
        f.library_folder = folder
        f.save(update_fields=["library_folder"])
        audit.record("library.file_moved", user, f, {"folder": str(folder.pk) if folder else None}, request)
        events.publish("library.changed", ("file", str(f.pk)), {})
    return f


def attach_existing(
    user: Any, task_id: Any, version_id: Any, request: HttpRequest | None = None
) -> tuple[TaskFile, bool]:
    """Link a file someone already uploaded to a task as a REFERENCE. No bytes, no blob, no new version.

    Returns (link, created); attaching the same version twice is not an error, it is the same link.
    """
    task = Task.objects.visible_to(user).filter(pk=task_id).first()
    if task is None:
        raise NotFound("No such task.")
    require(user, "file.attach_reference", task)
    if task.is_cancelled:
        raise Conflict("That task is cancelled.")
    version = (
        selectors.versions_visible_to(user)
        .filter(is_voice=False)
        .select_related("file")
        .filter(pk=version_id)
        .first()
    )
    if version is None:
        raise NotFound("File not found.")
    with transaction.atomic():
        link, created = TaskFile.objects.get_or_create(
            task=task,
            version=version,
            relation=FileRelation.REFERENCE,
            defaults={"file": version.file, "linked_by": user},
        )
        if created:
            body = {
                "file_code": version.file_code,
                "version": version.version_number,
                "relation": "REFERENCE",
            }
            TaskEvent.objects.create(
                task=task, actor=user, event_type="file.attached", payload=body, created_at=link.linked_at
            )
            audit.record("file.attached", user, version, {**body, "task_id": str(task.pk)}, request)
            events.publish("file.attached", ("task", str(task.pk)), {**body, "task_id": str(task.pk)})
    return link, created
