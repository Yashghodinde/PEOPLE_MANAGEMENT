"""Task policies (M07 section 5). Registered at import; the registry in ots.core.permissions fails closed.

M07.2 registers the assignment actions the engine needs. M07.3 adds the task-level ones.
"""

from __future__ import annotations

from typing import Any

from ots.accounts.models import Role
from ots.core.permissions import policy

from .models import TaskAssignment


def _active(user: Any) -> bool:
    return bool(user) and getattr(user, "is_active", False) and user.role in Role.values


def _own(user: Any, a: TaskAssignment) -> bool:
    return a.user_id == user.pk


def _created(user: Any, a: TaskAssignment) -> bool:
    return a.task.created_by_id == user.pk


def _is_reviewer(user: Any, a: TaskAssignment) -> bool:
    """Task creator, Super Admin or the assignee's CURRENT manager; never the assignee (F-TK-6)."""
    if not _active(user) or user.role == Role.EMPLOYEE or _own(user, a):
        return False
    return user.role == Role.SUPER_ADMIN or _created(user, a) or a.user.reports_to_id == user.pk


def _own_policy(user: Any, a: Any = None) -> bool:
    return _active(user) and a is not None and _own(user, a)


for _action in ("start", "pause_eod", "resume", "block", "submit"):
    policy(f"assignment.{_action}")(_own_policy)


@policy("assignment.unblock")
def _unblock(user: Any, a: Any = None) -> bool:
    if not _active(user) or a is None:
        return False
    return (
        _own(user, a) or user.role == Role.SUPER_ADMIN or (user.role == Role.SUB_ADMIN and _created(user, a))
    )


policy("assignment.approve")(_is_reviewer)
policy("assignment.request_changes")(_is_reviewer)


def _remover(user: Any, a: Any = None) -> bool:
    """Remove or cancel: the task creator or the Super Admin (an Employee is never a creator)."""
    if not _active(user) or a is None or user.role == Role.EMPLOYEE:
        return False
    return user.role == Role.SUPER_ADMIN or _created(user, a)


policy("assignment.remove")(_remover)
policy("assignment.cancel")(_remover)


# ---- task-level actions ----
def _visible(user: Any, task: Any) -> bool:
    """Super Admin: all. Sub-Admin: tasks they created, or with any assignee in their team (or themselves).
    Employee: tasks where they hold or held an assignment."""
    from ots.accounts import selectors

    if not _active(user):
        return False
    if task is None or user.role == Role.SUPER_ADMIN:
        return True  # None = "may open the tasks area"; rows are scoped by Task.objects.visible_to
    if user.role == Role.SUB_ADMIN:
        team = selectors.subtree_ids(user.pk) | {user.pk}
        return task.created_by_id == user.pk or task.assignments.filter(user_id__in=team).exists()
    return task.assignments.filter(user_id=user.pk).exists()


policy("task.view")(_visible)


@policy("task.create")
def _create(user: Any, obj: Any = None) -> bool:
    return _active(user) and user.role in (Role.SUPER_ADMIN, Role.SUB_ADMIN)


def _manage(user: Any, task: Any = None) -> bool:
    """Update, cancel, add people: the Super Admin for any task, a Sub-Admin for tasks they created."""
    if not _active(user) or task is None:
        return False
    return user.role == Role.SUPER_ADMIN or (user.role == Role.SUB_ADMIN and task.created_by_id == user.pk)


for _a in ("task.update", "task.cancel", "assignment.add"):
    policy(_a)(_manage)


@policy("task.comment")
def _comment(user: Any, task: Any = None) -> bool:
    if not _active(user) or task is None:
        return False
    if user.role == Role.SUPER_ADMIN:
        return True
    if user.role == Role.SUB_ADMIN:
        return _visible(user, task)
    return task.assignments.filter(user_id=user.pk, removed_at__isnull=True).exists()  # own assigned tasks
