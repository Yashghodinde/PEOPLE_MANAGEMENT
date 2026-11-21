"""The single door to workspaces (IMP-04 section 2.2 rule 2). Every view, search, count and autocomplete that
touches a workspace starts from `workspaces_visible_to`."""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Q, QuerySet

from ots.accounts.models import Role as UserRole

from .ids import normalise
from .models import FileIdDocument, FileIdMember, FileIdWorkspace, WorkspaceStatus


def workspaces_visible_to(user: Any) -> QuerySet[FileIdWorkspace]:
    """Super Admin: all (archived too). Anyone else: ACTIVE workspaces where they are a current member.
    Unknown roles: none. Being a manager, a task creator or knowing the File ID gives nothing."""
    if user is None or not getattr(user, "is_active", False) or not getattr(user, "is_authenticated", True):
        return FileIdWorkspace.objects.none()
    if user.role == UserRole.SUPER_ADMIN:
        return FileIdWorkspace.objects.all()
    if user.role not in (UserRole.SUB_ADMIN, UserRole.HR, UserRole.EMPLOYEE):
        return FileIdWorkspace.objects.none()
    mine = FileIdMember.objects.filter(user=user, revoked_at__isnull=True).values("workspace_id")
    return FileIdWorkspace.objects.filter(pk__in=mine, status=WorkspaceStatus.ACTIVE)


def find(user: Any, file_id: str) -> FileIdWorkspace | None:
    """The workspace with this File ID if the user may open it. Missing and forbidden look the same."""
    return workspaces_visible_to(user).filter(file_id_norm=normalise(file_id)).first()


def cards(user: Any) -> list[FileIdWorkspace]:
    return list(
        workspaces_visible_to(user)
        .select_related("owner")
        .annotate(
            member_count=Count("members", filter=Q(members__revoked_at__isnull=True), distinct=True),
            document_count=Count("documents", filter=Q(documents__removed_at__isnull=True), distinct=True),
        )
        .order_by("-created_at", "pk")[:200]
    )


def members(ws: FileIdWorkspace) -> list[FileIdMember]:
    return list(
        ws.members.filter(revoked_at__isnull=True)
        .select_related("user", "added_by")
        .order_by("added_at", "pk")
    )


def active_documents(ws: FileIdWorkspace) -> QuerySet[FileIdDocument]:
    return ws.documents.filter(removed_at__isnull=True).select_related("file", "added_by")
