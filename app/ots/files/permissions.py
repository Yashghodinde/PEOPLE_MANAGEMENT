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


@policy("file.download")
def can_download(user: Any, version: Any) -> bool:
    """Download and preview share one rule: see `selectors.versions_visible_to`."""
    from . import selectors

    if not _active(user) or version is None:
        return False
    return selectors.versions_visible_to(user).filter(pk=version.pk).exists()


@policy("file.handover")
def can_handover(user: Any, version: Any) -> bool:
    """Anyone who may open the file may send it on; whom it may go to is `selectors.handover_targets`."""
    return can_download(user, version)


@policy("file.attach_reference")
def can_attach(user: Any, task: Task | None) -> bool:
    """Super Admin: any task. Sub-Admin: visible tasks. Employees cannot attach existing files."""
    if not _active(user) or task is None:
        return False
    if user.role == Role.SUPER_ADMIN:
        return True
    if user.role == Role.SUB_ADMIN:
        return Task.objects.visible_to(user).filter(pk=task.pk).exists()
    return False


@policy("library.manage")
def can_manage_library(user: Any, obj: Any = None) -> bool:
    return _active(user) and bool(user.role == Role.SUPER_ADMIN)


@policy("storage.retry_failed")
def can_retry_failed(user: Any, obj: Any = None) -> bool:
    return _active(user) and bool(user.role == Role.SUPER_ADMIN)
