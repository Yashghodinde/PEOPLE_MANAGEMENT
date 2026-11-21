"""File ID policies (IMP-04 section 2.2). Fail closed; membership is looked up per request, never cached."""

from __future__ import annotations

from typing import Any

from ots.accounts.models import Role as UserRole
from ots.core.permissions import policy

from .models import FileIdMember, FileIdWorkspace, Role, WorkspaceStatus

CREATORS = (UserRole.SUPER_ADMIN, UserRole.SUB_ADMIN)


def _active(user: Any) -> bool:
    return bool(user) and getattr(user, "is_active", False) and user.role in UserRole.values


def member_role(user: Any, ws: FileIdWorkspace) -> str | None:
    """The person's role in this workspace today, or None (the Super Admin has no row: `effective_role`)."""
    if not _active(user):
        return None
    return (
        FileIdMember.objects.filter(workspace=ws, user=user, revoked_at__isnull=True)
        .values_list("role", flat=True)
        .first()
    )


def effective_role(user: Any, ws: FileIdWorkspace) -> str | None:
    """OWNER-level for the Super Admin (even if archived); a member's role otherwise."""
    if not _active(user):
        return None
    if user.role == UserRole.SUPER_ADMIN:
        return Role.OWNER
    if ws.status != WorkspaceStatus.ACTIVE:
        return None  # an archived workspace is for the Super Admin only
    return member_role(user, ws)


@policy("fileid.create")
def can_create(user: Any, obj: Any = None) -> bool:
    return _active(user) and user.role in CREATORS


@policy("fileid.view")
def can_view(user: Any, ws: Any = None) -> bool:
    return ws is not None and effective_role(user, ws) is not None


@policy("fileid.contribute")
def can_contribute(user: Any, ws: Any = None) -> bool:
    return ws is not None and effective_role(user, ws) in (Role.OWNER, Role.CONTRIBUTOR)


@policy("fileid.manage_members")
def can_manage_members(user: Any, ws: Any = None) -> bool:
    return ws is not None and effective_role(user, ws) == Role.OWNER


@policy("fileid.archive")
def can_archive(user: Any, ws: Any = None) -> bool:
    return _active(user) and user.role == UserRole.SUPER_ADMIN
