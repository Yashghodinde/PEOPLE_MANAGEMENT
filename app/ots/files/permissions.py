"""File policies (M08 section 8). Registered at import; the registry fails closed.

M08.2 registers upload. Download, attach and handover arrive with M08.4 and M08.5.
"""

from __future__ import annotations

from typing import Any

from ots.accounts.models import Role
from ots.core.permissions import policy
from ots.tasks.models import OPEN_STATUSES, Task, TaskAssignment


def _active(user: Any) -> bool:
    return bool(user) and getattr(user, "is_active", False) and user.role in Role.values


@policy("file.upload")
def can_upload(user: Any, task: Task | None) -> bool:
    """Super Admin: any task. Sub-Admin: visible tasks. Employee: tasks with their own open assignment.

    Without a task the file goes to the library, which only the Super Admin manages.
    """
    if not _active(user):
        return False
    if task is None:
        return bool(user.role == Role.SUPER_ADMIN)
    if user.role == Role.SUPER_ADMIN:
        return True
    if user.role == Role.SUB_ADMIN:
        return Task.objects.visible_to(user).filter(pk=task.pk).exists()
    return TaskAssignment.objects.filter(task=task, user=user, status__in=OPEN_STATUSES).exists()
