"""File reads (M08). Every query starts from what the viewer may see."""

from __future__ import annotations

from typing import Any

from django.db.models import Exists, OuterRef, Q, QuerySet

from ots.files.models import FileVersion, TaskFile
from ots.tasks.models import Task

PANEL_LIMIT = 200  # files shown on one task page (CLAUDE.md: never an unbounded list)

PENDING_STATES = ("STAGED", "ARCHIVING", "RETRY_WAIT", "VERIFYING", "BLOCKED_QUOTA", "BLOCKED_AUTH")


def task_file_rows(viewer: Any, task: Task) -> list[TaskFile]:
    """Newest first, one row per upload linked to this task. The task must already be visible to `viewer`."""
    if not Task.objects.visible_to(viewer).filter(pk=task.pk).exists():
        return []
    return list(
        TaskFile.objects.filter(task=task)
        .select_related("file", "version", "version__uploaded_by", "assignment__user")
        .order_by("-linked_at", "-id")[:PANEL_LIMIT]
    )


def anything_pending(rows: list[TaskFile]) -> bool:
    return any(r.version.state in PENDING_STATES for r in rows)


def versions_visible_to(viewer: Any) -> QuerySet[FileVersion]:
    """Versions `viewer` may download or preview (M08 section 8).

    Super Admin: all. Everyone else: their own uploads, files on tasks they can see, files handed to them, and
    the library. A Sub-Admin also sees uploads by anyone in their team.
    """
    from ots.accounts import selectors as accounts
    from ots.accounts.models import Role
    from ots.files.models import FileHandoverRecipient, FileVisibility

    if viewer is None or not getattr(viewer, "is_authenticated", False) or not viewer.is_active:
        return FileVersion.objects.none()
    qs = FileVersion.objects.all()
    if viewer.role == Role.SUPER_ADMIN:
        return qs
    visible_tasks = Task.objects.visible_to(viewer).values("pk")
    wanted = (
        Q(uploaded_by_id=viewer.pk)
        | Q(file__visibility=FileVisibility.LIBRARY)
        | Exists(TaskFile.objects.filter(version=OuterRef("pk"), task__in=visible_tasks))
        | Exists(FileHandoverRecipient.objects.filter(handover__version=OuterRef("pk"), to_user_id=viewer.pk))
    )
    if viewer.role == Role.SUB_ADMIN:
        wanted |= Q(uploaded_by_id__in=accounts.subtree_ids(viewer.pk))
    return qs.filter(wanted)
