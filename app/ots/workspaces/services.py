"""File ID workspaces: every write, with its audit row and event (IMP-04). Nothing is ever deleted: a
removal or a revocation sets a timestamp, so history stays."""

from __future__ import annotations

from typing import Any

from django.db import IntegrityError, transaction

from ots.accounts.models import Role as UserRole
from ots.accounts.models import User
from ots.audit import api as audit
from ots.core import events
from ots.core.clock import now
from ots.core.errors import Conflict, NotFound, ValidationFailed
from ots.core.permissions import require

from . import ids
from .models import (
    Category,
    FileIdDocument,
    FileIdMember,
    FileIdWorkspace,
    Role,
    WorkspaceStatus,
)

MAX_TAGS = 10


def _tags(raw: Any) -> list[str]:
    items = raw if isinstance(raw, list) else str(raw or "").split(",")
    out: list[str] = []
    for t in items:
        t = " ".join(str(t).split())[:40]
        if t and t.lower() not in {x.lower() for x in out}:
            out.append(t)
    return out[:MAX_TAGS]


def _clean(name: str, description: str, category: str) -> tuple[str, str, str]:
    name = " ".join((name or "").split())
    if not name or len(name) > 200:
        raise ValidationFailed("Give it a name of up to 200 characters.", fields={"name": ["Invalid."]})
    if category not in Category.values:
        raise ValidationFailed("Choose a category.", fields={"category": ["Invalid."]})
    return name, (description or "").strip()[:2000], category


@transaction.atomic
def create_workspace(
    actor: User, *, name: str, file_id: str = "", description: str = "", category: str = Category.CLIENT,
    tags: Any = None, members: list[tuple[User, str]] | None = None, request: Any = None,
    strict_format: bool = False,
) -> FileIdWorkspace:  # fmt: skip
    """Make a File ID. Blank `file_id` generates one; a typed one is normalised and must be free. The creator
    becomes the Owner. Two people typing the same id at the same moment: exactly one wins."""
    require(actor, "fileid.create")
    name, description, category = _clean(name, description, category)
    typed = bool(file_id.strip())
    for _attempt in range(8):
        shown = ids.normalise(file_id) if typed else ids.generate(name)
        if typed and strict_format and not ids.is_standard(shown):
            raise ValidationFailed(
                "A File ID looks like FTS-AZE-483921: FTS, 2 to 4 capital letters, then 6 digits.",
                fields={"file_id": ["Invalid."]},
            )
        if not ids.is_valid(shown):
            raise ValidationFailed(
                "A File ID is 4 to 40 letters, digits or hyphens, starting with a letter or digit.",
                fields={"file_id": ["Invalid."]},
            )
        try:
            with transaction.atomic():
                ws = FileIdWorkspace.objects.create(
                    file_id=shown, file_id_norm=shown, name=name, description=description, category=category,
                    tags=_tags(tags), owner=actor, created_by=actor,
                )  # fmt: skip
            break
        except IntegrityError:
            if typed:
                raise ValidationFailed(
                    "That File ID is already in use.", fields={"file_id": ["Already in use."]}
                ) from None
    else:  # pragma: no cover  (a generated id colliding eight times in a row)
        raise Conflict("Could not make a unique File ID. Try again.")
    FileIdMember.objects.create(workspace=ws, user=actor, role=Role.OWNER, added_by=actor)
    audit.record(
        "fileid.created", actor, ws, {"category": category}, request
    )  # the id is not copied into the log
    events.publish("fileid.created", ("fileid", str(ws.pk)), {"workspace_id": str(ws.pk)})
    for user, role in members or []:
        if user.pk != actor.pk:
            add_member(actor, ws, user, role, request=request)
    return ws


def _lock(ws: FileIdWorkspace) -> FileIdWorkspace:
    return FileIdWorkspace.objects.select_for_update().get(pk=ws.pk)


def _eligible(user: User) -> bool:
    """Who may be a member: an active Super Admin, Sub-Admin or Employee; never HR or break-glass."""
    return user.is_active and user.role != UserRole.HR and not user.is_break_glass


@transaction.atomic
def add_member(
    actor: User, ws: FileIdWorkspace, user: User, role: str, *, request: Any = None
) -> FileIdMember:
    ws = _lock(ws)
    require(actor, "fileid.manage_members", ws)
    if role not in Role.values:
        raise ValidationFailed("Choose a role.", fields={"role": ["Invalid."]})
    if not _eligible(user):
        raise ValidationFailed("That person cannot be added.", fields={"user": ["Not allowed."]})
    if FileIdMember.objects.filter(workspace=ws, user=user, revoked_at__isnull=True).exists():
        raise ValidationFailed("That person is already a member.", fields={"user": ["Already a member."]})
    m = FileIdMember.objects.create(workspace=ws, user=user, role=role, added_by=actor)
    audit.record("fileid.member_added", actor, ws, {"user": str(user.pk), "role": role}, request)
    return m


def _member(ws: FileIdWorkspace, user: User) -> FileIdMember:
    m = (
        FileIdMember.objects.select_for_update()
        .filter(workspace=ws, user=user, revoked_at__isnull=True)
        .first()
    )
    if m is None:
        raise NotFound("That person is not a member.")
    return m


def _owners(ws: FileIdWorkspace) -> int:
    return FileIdMember.objects.filter(workspace=ws, role=Role.OWNER, revoked_at__isnull=True).count()


@transaction.atomic
def change_role(
    actor: User, ws: FileIdWorkspace, user: User, role: str, *, request: Any = None
) -> FileIdMember:
    ws = _lock(ws)
    require(actor, "fileid.manage_members", ws)
    if role not in Role.values:
        raise ValidationFailed("Choose a role.", fields={"role": ["Invalid."]})
    m = _member(ws, user)
    if m.role == role:
        return m
    if m.role == Role.OWNER and _owners(ws) <= 1:
        raise ValidationFailed("A File ID needs an owner. Hand ownership to someone else first.")
    old, m.role = m.role, role
    m.save(update_fields=["role"])
    if role == Role.OWNER:
        FileIdWorkspace.objects.filter(
            pk=ws.pk, owner=ws.owner_id
        ).update()  # the owner column is the first owner
    audit.record("fileid.role_changed", actor, ws, {"user": str(user.pk), "from": old, "to": role}, request)
    return m


@transaction.atomic
def remove_member(actor: User, ws: FileIdWorkspace, user: User, *, request: Any = None) -> FileIdMember:
    """Access ends on the next request; everything the person did stays. The last Owner cannot be removed."""
    ws = _lock(ws)
    require(actor, "fileid.manage_members", ws)
    m = _member(ws, user)
    if m.role == Role.OWNER and _owners(ws) <= 1:
        raise ValidationFailed("The last owner cannot be removed. Hand ownership to someone else first.")
    m.revoked_at, m.revoked_by = now(), actor
    m.save(update_fields=["revoked_at", "revoked_by"])
    audit.record("fileid.member_removed", actor, ws, {"user": str(user.pk), "role": m.role}, request)
    return m


@transaction.atomic
def transfer_ownership(actor: User, ws: FileIdWorkspace, to: User, *, request: Any = None) -> FileIdWorkspace:
    """The Owner or the Super Admin hands the workspace to a member (or adds them as Owner). The previous
    owner stays a Contributor."""
    ws = _lock(ws)
    require(actor, "fileid.manage_members", ws)
    if not _eligible(to):
        raise ValidationFailed("That person cannot own a File ID.", fields={"user": ["Not allowed."]})
    prev = ws.owner_id
    mine = (
        FileIdMember.objects.select_for_update()
        .filter(workspace=ws, user=to, revoked_at__isnull=True)
        .first()
    )
    if mine is None:
        FileIdMember.objects.create(workspace=ws, user=to, role=Role.OWNER, added_by=actor)
    else:
        mine.role = Role.OWNER
        mine.save(update_fields=["role"])
    if prev != to.pk:
        FileIdMember.objects.filter(workspace=ws, user_id=prev, revoked_at__isnull=True).exclude(
            user=to
        ).update(role=Role.CONTRIBUTOR)
    ws.owner = to
    ws.save(update_fields=["owner"])
    audit.record("fileid.ownership_transferred", actor, ws, {"from": str(prev), "to": str(to.pk)}, request)
    return ws


@transaction.atomic
def link_document(actor: User, ws: FileIdWorkspace, file: Any, *, request: Any = None) -> FileIdDocument:
    """Link an existing File. The actor must be able to open it, and be a Contributor or Owner. A private file
    of somebody else cannot be linked by anyone but the Super Admin, and linking widens nothing elsewhere."""
    from ots.files.models import FileVisibility
    from ots.files.selectors import versions_visible_to

    ws = _lock(ws)
    require(actor, "fileid.contribute", ws)
    can_open = versions_visible_to(actor).filter(file=file).exists()
    if not can_open or (file.visibility == FileVisibility.PRIVATE and file.owner_id != actor.pk and
                        actor.role != UserRole.SUPER_ADMIN):  # fmt: skip
        raise NotFound("File not found.")
    return _link(actor, ws, file, request)


def _link(actor: User, ws: FileIdWorkspace, file: Any, request: Any, folder_id: Any = None) -> FileIdDocument:
    existing = FileIdDocument.objects.filter(workspace=ws, file=file, removed_at__isnull=True).first()
    if existing is not None:
        return existing  # linking twice is the same as linking once
    doc = FileIdDocument.objects.create(workspace=ws, file=file, added_by=actor, folder_id=folder_id)
    audit.record(
        "fileid.document_linked", actor, ws, {"file": str(file.pk), "name": file.display_name}, request
    )
    events.publish("fileid.document_linked", ("fileid", str(ws.pk)), {"file_id": str(file.pk)})
    return doc


def link_new_file(
    actor: User, ws: FileIdWorkspace, file: Any, request: Any = None, folder_id: Any = None
) -> FileIdDocument:
    """A file just uploaded into a workspace (called in the upload's commit, which checked the rights)."""
    return _link(actor, ws, file, request, folder_id)


@transaction.atomic
def unlink_document(actor: User, ws: FileIdWorkspace, file: Any, *, request: Any = None) -> FileIdDocument:
    ws = _lock(ws)
    require(actor, "fileid.contribute", ws)
    doc = (
        FileIdDocument.objects.select_for_update()
        .filter(workspace=ws, file=file, removed_at__isnull=True)
        .first()
    )
    if doc is None:
        raise NotFound("That document is not in this File ID.")
    doc.removed_at, doc.removed_by = now(), actor
    doc.save(update_fields=["removed_at", "removed_by"])
    audit.record(
        "fileid.document_unlinked", actor, ws, {"file": str(file.pk), "name": file.display_name}, request
    )
    return doc


@transaction.atomic
def edit_details(
    actor: User,
    ws: FileIdWorkspace,
    *,
    name: str,
    description: str,
    category: str,
    tags: Any,
    request: Any = None,
) -> FileIdWorkspace:
    ws = _lock(ws)
    require(actor, "fileid.manage_members", ws)
    ws.name, ws.description, ws.category = _clean(name, description, category)
    ws.tags = _tags(tags)
    ws.save(update_fields=["name", "description", "category", "tags"])
    audit.record("fileid.edited", actor, ws, {}, request)
    return ws


@transaction.atomic
def set_status(actor: User, ws: FileIdWorkspace, status: str, *, request: Any = None) -> FileIdWorkspace:
    ws = _lock(ws)
    require(actor, "fileid.archive", ws)
    if ws.status == status:
        return ws
    ws.status = status
    ws.save(update_fields=["status"])
    audit.record(
        "fileid.archived" if status == WorkspaceStatus.ARCHIVED else "fileid.restored", actor, ws, {}, request
    )
    return ws
