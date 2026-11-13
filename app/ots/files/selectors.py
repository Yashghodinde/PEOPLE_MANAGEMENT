"""File reads (M08). Every query starts from what the viewer may see."""

from __future__ import annotations

from typing import Any

from ots.files.models import TaskFile
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
