"""Personal storage (IMP-02): reads for "My storage" and "Shared with me", and the writes (rename, hide, restore).

"Today" and the date folders come from `FileVersion.uploaded_on`, the Indian calendar date stored at upload, and
"today" is worked out for each request. There is no job at midnight, so nothing can move, duplicate or vanish
when the date changes. Hiding a file only takes it out of its owner's view: no version, blob or SharePoint
object is touched, and people it was already handed to keep their access.
"""

from __future__ import annotations

import datetime
from typing import Any

from django.db import transaction
from django.db.models import Count, QuerySet
from django.http import HttpRequest

from ots.accounts.models import Role
from ots.audit import api as audit
from ots.core import events
from ots.core.clock import now, today_local
from ots.core.errors import NotFound, PermissionDenied, ValidationFailed

from . import naming
from .models import File, FileHandoverRecipient, FileStatus, FileVersion, FileVisibility

DAYS_PER_PAGE = 30


def _mine(user: Any) -> QuerySet[FileVersion]:
    """Every version of the user's own, visible personal files."""
    return FileVersion.objects.filter(
        uploaded_by=user, file__visibility=FileVisibility.PRIVATE, file__status=FileStatus.ACTIVE, is_voice=False
    ).select_related("file", "blob", "uploaded_by")


def todays_versions(user: Any) -> list[FileVersion]:
    return list(_mine(user).filter(uploaded_on=today_local()).order_by("-uploaded_at", "-pk")[:200])


def versions_on(user: Any, day: datetime.date) -> list[FileVersion]:
    return list(_mine(user).filter(uploaded_on=day).order_by("-uploaded_at", "-pk")[:500])


def date_folders(user: Any, page: int = 0) -> tuple[list[tuple[datetime.date, int]], bool]:
    """(date, number of files) for the days before today, newest first, DAYS_PER_PAGE at a time."""
    rows = list(
        _mine(user)
        .filter(uploaded_on__lt=today_local())
        .order_by()
        .values_list("uploaded_on")
        .annotate(n=Count("pk"))
        .order_by("-uploaded_on")[page * DAYS_PER_PAGE : (page + 1) * DAYS_PER_PAGE + 1]
    )
    return rows[:DAYS_PER_PAGE], len(rows) > DAYS_PER_PAGE


def shared_with_me(user: Any, limit: int = 100) -> list[FileVersion]:
    """Versions other people sent to this person, newest hand-over first (each once)."""
    seen: dict[Any, FileVersion] = {}
    qs = (
        FileHandoverRecipient.objects.filter(to_user=user)
        .select_related("handover__version__file", "handover__version__blob", "handover__from_user")
        .order_by("-handover__sent_at", "-pk")[: limit * 3]
    )
    for r in qs:
        v = r.handover.version
        if v.is_voice or v.pk in seen:
            continue
        v.sent_by, v.sent_at = r.handover.from_user, r.handover.sent_at  # type: ignore[attr-defined]
        seen[v.pk] = v
    return list(seen.values())[:limit]


def hidden_files() -> list[File]:
    """What owners have hidden, for the Super Admin's "Deleted" view."""
    return list(
        File.objects.filter(visibility=FileVisibility.PRIVATE, status=FileStatus.DELETED)
        .select_related("owner", "current_version", "deleted_by")
        .order_by("-deleted_at", "-pk")[:200]
    )


# ---------------------------------------------------------------- writes


def _own_file(user: Any, file_id: Any, *, want: str = FileStatus.ACTIVE) -> File:
    f = File.objects.select_for_update().filter(pk=file_id, visibility=FileVisibility.PRIVATE).first()
    if f is None or f.owner_id != user.pk or f.status != want:
        raise NotFound("File not found.")  # someone else's file and a missing one look the same
    return f


@transaction.atomic
def rename(user: Any, file_id: Any, name: str, request: HttpRequest | None = None) -> File:
    f = _own_file(user, file_id)
    name = " ".join((name or "").split())[:255]
    if not name:
        raise ValidationFailed("Type a name.", fields={"name": ["Required."]})
    verdict = naming.judge(name)
    if not verdict.ok:
        raise ValidationFailed(verdict.message, fields={"name": [verdict.message]}, suggestion=verdict.suggestion)
    if name == f.display_name:
        return f
    old, f.display_name = f.display_name, name
    f.save(update_fields=["display_name"])
    audit.record("file.renamed", user, f, {"from": old, "to": name}, request)
    return f


@transaction.atomic
def hide(user: Any, file_id: Any, request: HttpRequest | None = None) -> File:
    """"Delete" in the owner's words: it leaves the owner's view; nothing is removed anywhere."""
    f = File.objects.select_for_update().filter(pk=file_id, visibility=FileVisibility.PRIVATE).first()
    if f is None or f.owner_id != user.pk:
        raise NotFound("File not found.")
    if f.status == FileStatus.DELETED:
        return f  # a second press does nothing
    f.status, f.deleted_at, f.deleted_by = FileStatus.DELETED, now(), user
    f.save(update_fields=["status", "deleted_at", "deleted_by"])
    audit.record("file.hidden", user, f, {"name": f.display_name}, request)
    events.publish("file.hidden", ("file", str(f.pk)), {"file_id": str(f.pk)})
    return f


@transaction.atomic
def restore(user: Any, file_id: Any, request: HttpRequest | None = None) -> File:
    if user.role != Role.SUPER_ADMIN:
        raise PermissionDenied("Only the Super Admin can restore a hidden file.", action="file.restore")
    f = File.objects.select_for_update().filter(pk=file_id, visibility=FileVisibility.PRIVATE).first()
    if f is None:
        raise NotFound("File not found.")
    if f.status == FileStatus.ACTIVE:
        return f
    f.status, f.deleted_at, f.deleted_by = FileStatus.ACTIVE, None, None
    f.save(update_fields=["status", "deleted_at", "deleted_by"])
    audit.record("file.restored", user, f, {"name": f.display_name}, request)
    events.publish("file.restored", ("file", str(f.pk)), {"file_id": str(f.pk)})
    return f
