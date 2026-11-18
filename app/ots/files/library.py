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
from .models import (
    File,
    FileRelation,
    FileVisibility,
    LibraryFolder,
    LibraryReview,
    LibraryState,
    TaskFile,
)

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


def ensure_path(user: Any, parts: list[str], parent_id: Any = None) -> LibraryFolder | None:
    """Create (or reuse) each folder of `parts` under `parent_id` (a folder upload). Returns the last."""
    require(user, "library.manage", None)
    cur_id, last = parent_id, None
    for raw in parts:
        name = _name(raw)
        existing = LibraryFolder.objects.filter(parent_id=cur_id, name=name).first()
        last = existing or create_folder(user, name, cur_id)
        cur_id = last.pk
    return last


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


# ---------------------------------------------------------------- staff uploads and approval (IMP-15.5)


def may_upload(user: Any, folder: LibraryFolder | None) -> bool:
    """The Super Admin anywhere; staff (not HR) only inside a folder that allows it."""
    from ots.accounts.models import Role
    from ots.core.permissions import can

    if can(user, "library.manage", None):
        return True
    return bool(
        user is not None
        and getattr(user, "is_active", False)
        and user.role in (Role.SUB_ADMIN, Role.EMPLOYEE)
        and folder is not None
        and folder.employee_upload
    )


def approvers_of(uploader: Any) -> list[Any]:
    """Who is told: the uploader's reporting manager, or the next one up if that person is gone; the Super
    Admins if nobody above is active (D-097)."""
    from ots.accounts.models import Role, User

    cur, seen = uploader.reports_to, 0
    while cur is not None and seen < 10:
        if cur.is_active:
            return [cur]
        cur, seen = cur.reports_to, seen + 1
    return list(User.objects.filter(is_active=True, role=Role.SUPER_ADMIN)[:5])


def can_review(user: Any, file: File) -> bool:
    """The Super Admin, or anyone above the uploader in the reporting chain."""
    from ots.accounts import selectors as accounts
    from ots.accounts.models import Role

    if user is None or not getattr(user, "is_active", False):
        return False
    if user.role == Role.SUPER_ADMIN:
        return True
    return user.role == Role.SUB_ADMIN and file.owner_id in accounts.subtree_ids(user.pk)


def notify_pending(file: File, folder: LibraryFolder | None, request: HttpRequest | None = None) -> None:
    from ots.notifications.models import Kind
    from ots.notifications.services import notify

    who = file.owner.full_name
    link = f"/files/library?folder={folder.pk}" if folder else "/files/library"
    notify(
        [u.pk for u in approvers_of(file.owner)], Kind.LIBRARY_REVIEW, None,
        f"{who} sent {file.display_name} to the Library",
        actor=file.owner, body="Waiting for your approval.", link=link, source=f"library:{file.pk}",
    )  # fmt: skip


def _decide(user: Any, file_id: Any, decision: str, reason: str, request: HttpRequest | None) -> File:
    from ots.notifications.models import Kind
    from ots.notifications.services import notify

    with transaction.atomic():
        f = (
            File.objects.select_for_update(of=("self",))
            .select_related("owner", "library_folder", "current_version")
            .filter(pk=file_id, visibility=FileVisibility.LIBRARY, library_state=LibraryState.PENDING)
            .first()
        )
        if f is None or not can_review(user, f):
            raise NotFound("Nothing to review there.")
        f.library_state = decision
        f.save(update_fields=["library_state"])
        LibraryReview.objects.create(
            file=f, version=f.current_version, reviewer=user, decision=decision, reason=reason
        )
        audit.record(
            f"library.{decision.lower()}", user, f, {"name": f.display_name, "reason": reason}, request
        )
        events.publish("library.changed", ("file", str(f.pk)), {})
        folder = f.library_folder
        link = f"/files/library?folder={folder.pk}" if folder else "/files/library"
        if decision == LibraryState.APPROVED:
            notify([f.owner_id], Kind.LIBRARY_DECISION, None, f"{f.display_name} was approved", actor=user,
                   body="It is now in the Library.", link=link, source=f"library-ok:{f.pk}")  # fmt: skip
        else:
            notify([f.owner_id], Kind.LIBRARY_DECISION, None, f"{f.display_name} was not approved",
                   actor=user,
                   body=reason, link="/app/notifications", source=f"library-no:{f.pk}")  # fmt: skip
    return f


def approve(user: Any, file_id: Any, request: HttpRequest | None = None) -> File:
    return _decide(user, file_id, LibraryState.APPROVED, "", request)


def reject(user: Any, file_id: Any, reason: str, request: HttpRequest | None = None) -> File:
    """The file leaves the Library view for everyone but the audit trail; it is not deleted (D-098)."""
    reason = " ".join((reason or "").split())[:300]
    if len(reason) < 5:
        raise ValidationFailed(
            "Say why, so the sender knows what to change.", fields={"reason": ["Required."]}
        )
    return _decide(user, file_id, LibraryState.REJECTED, reason, request)


def set_employee_upload(
    user: Any, folder_id: Any, allowed: bool, request: HttpRequest | None = None
) -> LibraryFolder:
    require(user, "library.manage", None)
    with transaction.atomic():
        folder = LibraryFolder.objects.select_for_update().filter(pk=folder_id).first()
        if folder is None:
            raise NotFound("No such folder.")
        if folder.employee_upload != allowed:
            folder.employee_upload = allowed
            folder.save(update_fields=["employee_upload"])
            audit.record("library.folder_uploads", user, folder, {"allowed": allowed}, request)
    return folder
